#!/usr/bin/env python3
"""夜间无人值守驱动：一任务一全新会话，靠最小交接棒接力。

为什么是外置驱动而不是同会话长跑：2026-09-24 夜班 17 小时单会话烧掉 5.5 亿
cache-read token——上下文只涨不掉（p50 246k、峰值 962k），每轮请求都重读整窗。
一任务一全新会话把跨任务冗余归零，历史只经
`${ZX_DATA_ROOT}/reports/night/<date>/handoff-NNN-<taskid>.md` 传递（契约见
docs/09 §十，形态见 ADR-0023）。

用法（睡前一条命令，何时开跑由用户定——本驱动只交付不自跑）：

    uv run python tools/night_driver.py --dry-run              # 只挑任务+渲染简报
    uv run python tools/night_driver.py --tasks 57 53          # 按清单（zx-task 号）
    uv run python tools/night_driver.py                        # 自主从 zx-task board 挑

晨间看 `${ZX_DATA_ROOT}/reports/night-shift-<date>.md` 与 `decision-pack-<date>.md`。
离线测试：`NIGHT_CLAUDE_BIN=tools/night/fake_claude.sh` 注入假会话，不打真实 API。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date as date_cls
from pathlib import Path

from zhixing_quant import config

REPO_ROOT = Path(__file__).resolve().parent.parent
NIGHT_DIR = REPO_ROOT / "tools" / "night"
BRIEF_TMPL = NIGHT_DIR / "task_brief.tmpl.md"
HANDOFF_TMPL = NIGHT_DIR / "handoff.tmpl.md"
NIGHT_SETTINGS = NIGHT_DIR / "night-settings.json"

#: 交接棒契约（docs/09 §十）：机器只校验节头与容量，内容真伪归晨间人审。
MAX_HANDOFF_LINES = 150
MAX_HANDOFF_BYTES = 8192
MAX_BRIEF_BYTES = 3500

HANDOFF_SECTIONS: tuple[str, ...] = (
    "## 一、结论与产出物",
    "## 二、关键决策",
    "## 三、触碰的文件",
    "## 四、证据指针",
    "## 五、开着的口子",
    "## 六、建议的下一任务",
)

#: Q1 四节点（05 §四）相关词：命中即挂起进决策包，夜间永不执行。
Q1_KEYWORDS: tuple[str, ...] = (
    "验收",
    "采纳",
    "黄金样本",
    "合入",
    "上线",
    "部署",
    "策略启用",
    "用户签字",
    "待人工",
)

CONVENTIONAL_RE = re.compile(
    r"^(feat|fix|docs|test|refactor|chore|perf|build|ci|style)(\([^)]*\))?!?: .+"
)
BOARD_RE = re.compile(
    r"^#(?P<id>\d+) \[(?P<type>\w+)\] (?P<title>.+)｜(?P<step>[^｜]*)｜"
    r"(?P<phase>[^｜]*)｜(?P<status>[^｜]*)｜打回 (?P<rejects>\d+)\s*$"
)
#: 只扫会话里的 Bash 命令，不扫简报正文——简报本来就含「禁止 git reset --hard」字样。
DESTRUCTIVE_RE = re.compile(
    r"git push (--force|-f)|git reset --hard|git clean|git branch -D|"
    r"git push --delete|publish_site\.sh"
)


class SessionTimeout(Exception):
    """任务会话墙钟超时。"""


@dataclass(frozen=True)
class Task:
    task_id: str
    title: str
    step: str = "-"
    phase: str = "-"
    status: str = "-"
    from_user: bool = False

    @property
    def label(self) -> str:
        return f"#{self.task_id} {self.title}"


@dataclass
class TaskResult:
    task: Task
    status: str  # 完成 / 部分完成 / 挂起 / 跳过
    handoff_path: Path | None = None
    violations: list[str] = field(default_factory=list)
    open_items: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    note: str = ""


def git(repo: Path, *args: str) -> str:
    """只读 git 调用；失败返回空串而不是抛——驱动不该因为 git 抖动整晚停摆。"""
    try:
        out = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def parse_board(text: str) -> list[Task]:
    """解析 `zx-task board` 输出行：`#id [type] title｜step｜phase｜status｜打回 n`。"""
    tasks: list[Task] = []
    for line in text.splitlines():
        match = BOARD_RE.match(line.strip())
        if not match:
            continue
        tasks.append(
            Task(
                task_id=match.group("id"),
                title=match.group("title"),
                step=match.group("step"),
                phase=match.group("phase"),
                status=match.group("status"),
            )
        )
    return tasks


def is_q1_gated(task: Task) -> str:
    """命中人工门禁返回原因，空串 = 可以夜间跑。"""
    for word in Q1_KEYWORDS:
        if word in task.title or word in task.phase:
            return f"命中人工门禁关键词「{word}」"
    if task.phase in {"功能验收", "结论"}:
        return f"阶段已是「{task.phase}」（Q1 Step 验收/结论属用户裁决位）"
    return ""


def pick_tasks(
    board: list[Task], user_ids: list[str], max_tasks: int
) -> tuple[list[Task], list[Task]]:
    """返回 (今晚要跑的, 挂起进决策包的)。

    用户点名 = 用户已拍板，照单全跑不过 Q1 滤网；自主模式才走滤网——
    Q1 四节点是「用户没点头就不许动」，不是「用户点头了也不许动」。
    """
    if user_ids:
        by_id = {t.task_id: t for t in board}
        chosen: list[Task] = []
        for raw in user_ids[:max_tasks]:
            found = by_id.get(raw)
            if found is None:
                chosen.append(Task(task_id=raw, title=raw, from_user=True))
            else:
                chosen.append(
                    Task(
                        task_id=found.task_id,
                        title=found.title,
                        step=found.step,
                        phase=found.phase,
                        status=found.status,
                        from_user=True,
                    )
                )
        return chosen, []

    chosen = []
    parked = []
    for task in board:
        if "已结" in task.status or "作废" in task.status:
            continue  # 完结任务既不跑也不挂起，不进晨间报告
        reason = is_q1_gated(task)
        if reason:
            parked.append(task)  # 挂起不占任务数上限——列出来是给晨间看的，不是跑它
            continue
        if len(chosen) < max_tasks:
            chosen.append(task)
    return chosen, parked


def render_brief(
    task: Task, handoff_path: Path, prev_handoff: Path | None, head_sha: str, refs: str
) -> str:
    """渲染任务简报：指针不带内容，全文 ≤MAX_BRIEF_BYTES。"""
    template = BRIEF_TMPL.read_text(encoding="utf-8")
    if prev_handoff is not None:
        prev_line = f"上一份交接棒 `{prev_handoff}`——先读它再开工"
    else:
        prev_line = "无（本晚第一个任务，从 01 路线图定位当前 Step 开始）"
    brief = (
        template.replace("{prev_handoff_line}", prev_line)
        .replace("{task_id}", f"#{task.task_id}")
        .replace("{title}", task.title)
        .replace("{step}", task.step)
        .replace("{refs}", refs)
        .replace("{git_sha}", head_sha or "unknown")
        .replace("{handoff_path}", str(handoff_path))
    )
    encoded = brief.encode("utf-8")
    if len(encoded) > MAX_BRIEF_BYTES:
        raise ValueError(f"简报 {len(encoded)}B 超上限 {MAX_BRIEF_BYTES}B：{task.label}")
    return brief


def validate_handoff(path: Path) -> list[str]:
    """交接棒机器校验：节头齐、≤150 行、≤8KB。返回违规清单，空 = 合格。"""
    if not path.exists():
        return [f"交接棒不存在：{path}"]
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []
    lines = text.splitlines()
    if len(lines) > MAX_HANDOFF_LINES:
        problems.append(f"交接棒 {len(lines)} 行，超 {MAX_HANDOFF_LINES} 行上限")
    if len(text.encode("utf-8")) > MAX_HANDOFF_BYTES:
        problems.append(f"交接棒超 {MAX_HANDOFF_BYTES} 字节上限")
    for section in HANDOFF_SECTIONS:
        if section not in text:
            problems.append(f"缺节头：{section}")
    return problems


def write_stub_handoff(path: Path, task: Task, reason: str) -> None:
    """会话没留下合格交接棒时，驱动代写挂起桩——格式永远合格，把原因留给晨间。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""# 交接棒 · #{task.task_id} · 挂起

## 一、结论与产出物（≤10 行）

- 未产出。会话未留下合格交接棒，驱动代写挂起桩：{reason}

## 二、关键决策（逐条：选了什么 / 为什么 / 代价是什么）

- 无（会话未走到收工交接）

## 三、触碰的文件（仅路径）

- 无

## 四、证据指针（pytest 尾行、coverage 数字、backtest_id、commit sha、CI run id）

- 无。诊断指针：`{path.parent}/session-*.jsonl`

## 五、开着的口子 / 待用户裁决（标注对应 Q1 哪个节点）

- 本任务整件挂起（无 Q1 节点归属）：{reason}

## 六、建议的下一任务（一句话 + 路线图 Step 依据）

- 复跑本任务（{task.label}），或先看诊断再定
""",
        encoding="utf-8",
    )


def extract_bash_commands(log_path: Path) -> list[str]:
    """从 stream-json 会话日志抽取 Bash 命令（只认 tool_use，不误伤简报正文）。"""
    commands: list[str] = []
    if not log_path.exists():
        return commands
    for line in log_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") or {}
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    command = tool_input.get("command")
                    if isinstance(command, str):
                        commands.append(command)
    return commands


def post_checks(repo: Path, sha_before: str, log_path: Path) -> list[str]:
    """司机侧硬复查：提交规范、ADR 只增不改、冲突残留、破坏性命令痕迹。"""
    problems: list[str] = []
    sha_after = git(repo, "rev-parse", "HEAD")
    if sha_before and sha_after and sha_after != sha_before:
        subjects = git(repo, "log", "--pretty=%s", f"{sha_before}..HEAD")
        for subject in filter(None, subjects.splitlines()):
            if not CONVENTIONAL_RE.match(subject):
                problems.append(f"非 Conventional Commit：{subject}")
        adr_changes = git(repo, "diff", "--name-status", f"{sha_before}..HEAD", "--", "docs/adr/")
        for line in filter(None, adr_changes.splitlines()):
            if line[:1] in {"M", "D", "R"}:
                problems.append(f"改动了 ADR 旧文（只增不改）：{line}")
    unmerged = git(repo, "diff", "--name-only", "--diff-filter=U")
    if unmerged:
        problems.append(f"git 残留未合并冲突：{unmerged.replace(chr(10), ' ')}")
    for command in extract_bash_commands(log_path):
        if DESTRUCTIVE_RE.search(command):
            problems.append(f"会话执行了破坏性命令：{command[:120]}")
    return problems


def parse_handoff(path: Path) -> tuple[list[str], list[str], list[str]]:
    """抽取交接棒三段：关键决策、开着的口子、证据指针。"""
    if not path.exists():
        return [], [], []
    text = path.read_text(encoding="utf-8")
    decisions = _section_lines(text, "## 二、关键决策", "## 三、")
    open_items = _section_lines(text, "## 五、开着的口子", "## 六、")
    evidence = _section_lines(text, "## 四、证据指针", "## 五、")
    return decisions, open_items, evidence


def _section_lines(text: str, start: str, end: str) -> list[str]:
    """抽取一节的正文行；缺尾标记时退化为「碰到下一个 ## 标题就停」。"""
    if start not in text:
        return []
    body = text.split(start, 1)[1]
    if end in body:
        body = body.split(end, 1)[0]
    else:
        kept: list[str] = []
        for ln in body.splitlines():
            if ln.startswith("## "):
                break
            kept.append(ln)
        body = "\n".join(kept)
    return [ln.strip() for ln in body.splitlines() if ln.strip() and ln.strip() != "-"]


def _kill_group(proc: subprocess.Popen[str], hard: bool = False) -> None:
    """杀整个进程组（start_new_session 后 pgid == pid）。SIGTERM 起步，hard 用 SIGKILL。"""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL if hard else signal.SIGTERM)


def run_session(
    claude_bin: str,
    brief: str,
    handoff_path: Path,
    task_id: str,
    log_path: Path,
    timeout_s: float,
    repo: Path,
    model: str,
) -> int:
    """起一个全新无头会话。返回退出码；超时杀进程后抛 SessionTimeout。"""
    settings_path = NIGHT_SETTINGS if NIGHT_SETTINGS.exists() else None
    cmd = [
        claude_bin,
        "-p",
        brief,
        "--output-format",
        "stream-json",
        "--permission-mode",
        "acceptEdits",
    ]
    if settings_path is not None:
        cmd += ["--settings", str(settings_path)]
    if model:
        cmd += ["--model", model]
    env = dict(os.environ)
    env["NIGHT_HANDOFF_PATH"] = str(handoff_path)
    env["NIGHT_TASK_ID"] = task_id
    proc = subprocess.Popen(
        cmd,
        cwd=repo,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # 独立进程组：超时时连子孙一起杀。否则孤儿子进程占着 stdout 管道，
        # communicate() 等 EOF 能等到天荒地老（真踩过：sleep 300 的替身拖死测试）
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_group(proc, hard=True)
            out, _ = proc.communicate()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(out or "", encoding="utf-8")
        raise SessionTimeout(f"任务墙钟超时（{timeout_s:.0f}s）") from None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(out or "", encoding="utf-8")
    return proc.returncode


def write_night_shift(path: Path, date_str: str, results: list[TaskResult], mode: str) -> None:
    """晨间总报告，三节沿用 night-shift-2026-09-24.md 的既有格式。"""
    done = [r for r in results if r.status in {"完成", "部分完成"}]
    hung = [r for r in results if r.status in {"挂起", "跳过"}]
    lines = [
        f"# 夜班交接 · {date_str}（{mode}）",
        "",
        "> 产出物，归档 `${ZX_DATA_ROOT}/reports/`（README §6.3 ④）。同目录"
        f" `decision-pack-{date_str}.md` 有待你拍板的条目。",
        "",
        "## 一、今晚上线了什么",
        "",
        "| 任务 | 结果 | 交接棒 |",
        "|---|---|---|",
    ]
    for r in done + hung:
        name = r.handoff_path.name if r.handoff_path else "—"
        note = r.note.replace("|", "／")[:60]
        lines.append(f"| {r.task.label[:40]} | {r.status}（{note or '见交接棒'}） | {name} |")
    lines += ["", "## 二、证据（可重放）", ""]
    for r in results:
        if r.evidence:
            lines.append(f"- **{r.task.label[:40]}**：" + "；".join(r.evidence[:4]))
        elif r.note:
            lines.append(f"- **{r.task.label[:40]}**：{r.note}")
    lines += ["", "## 三、还开着的口子", ""]
    any_open = False
    for r in results:
        for item in r.open_items:
            lines.append(f"- **{r.task.label[:40]}**：{item}")
            any_open = True
    if not any_open:
        lines.append("- 无（或见 decision-pack）")
    for r in results:
        for v in r.violations:
            lines.append(f"- ⚠️ **{r.task.label[:40]}**：{v}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_decision_pack(path: Path, date_str: str, results: list[TaskResult]) -> bool:
    """待用户裁决包：驱动只搬运+编号，不改写交接棒原话。返回是否有条目。"""
    numbered = 0
    lines = [
        f"# 待用户裁决 · 决策包（{date_str} 夜班整理）",
        "",
    ]
    for r in results:
        if not r.open_items:
            continue
        for item in r.open_items:
            numbered += 1
            lines += [f"## {numbered}. {r.task.label[:40]}", "", item, ""]
    if numbered == 0:
        return False
    lines += ["## 一句话总结", "", f"共 {numbered} 件等你拍板；四五行回复能清一批。", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return True


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False：否则 `--tasks 57` 会被缩写解析成 `--tasks-file 57`（真踩过）
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="夜间无人值守驱动：一任务一全新会话 + 最小交接棒（ADR-0023）",
        epilog="晨间产物：reports/night-shift-<date>.md 与 decision-pack-<date>.md",
    )
    parser.add_argument(
        "--tasks", nargs="+", default=[], help="zx-task 号清单（点名模式，照单全跑）"
    )
    parser.add_argument("--tasks-file", type=Path, help="任务清单文件，每行一个 zx-task 号")
    parser.add_argument("--max-tasks", type=int, default=6)
    parser.add_argument("--max-hours", type=float, default=8.0)
    parser.add_argument("--max-task-minutes", type=float, default=75.0)
    parser.add_argument("--max-consecutive-failures", type=int, default=2)
    parser.add_argument("--model", default="", help="透传给 claude --model")
    parser.add_argument("--claude-bin", default=os.environ.get("NIGHT_CLAUDE_BIN", "claude"))
    parser.add_argument("--date", default=date_cls.today().isoformat())
    parser.add_argument("--dry-run", action="store_true", help="只挑任务+渲染简报，不跑会话")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    user_ids = list(args.tasks)
    if args.tasks_file:
        user_ids += [
            ln.strip()
            for ln in args.tasks_file.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]

    for required in (BRIEF_TMPL, HANDOFF_TMPL):
        if not required.exists():
            print(f"fatal：缺模板 {required}", file=sys.stderr)
            return 2

    reports = config.reports_dir()
    night_dir = reports / "night" / args.date
    night_dir.mkdir(parents=True, exist_ok=True)

    zx_task = os.environ.get("NIGHT_ZX_TASK", str(REPO_ROOT / ".venv" / "bin" / "zx-task"))
    board = parse_board(
        subprocess.run(
            [zx_task, "board"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    )
    to_run, parked = pick_tasks(board, user_ids, args.max_tasks)
    mode = "按用户清单" if user_ids else "agent 自主推进"

    if not to_run:
        print("没有可跑的任务（全被 Q1 滤网拦下或看板为空）", file=sys.stderr)
        for task in parked:
            print(f"  挂起：{task.label}（{is_q1_gated(task)}）", file=sys.stderr)
        return 1

    if args.dry_run:
        head_sha = git(REPO_ROOT, "rev-parse", "HEAD")
        for task in to_run:
            handoff_path = night_dir / f"handoff-001-{task.task_id}.md"
            brief = render_brief(task, handoff_path, None, head_sha, f"zx-task show {task.task_id}")
            print(f"=== 将跑：{task.label}（Step {task.step} / {task.phase}）===")
            print(brief)
            print(f"[brief {len(brief.encode('utf-8'))}B ≤ {MAX_BRIEF_BYTES}B]")
        for task in parked:
            print(f"=== 挂起：{task.label}（{is_q1_gated(task)}）===")
        return 0

    results: list[TaskResult] = []
    started = time.monotonic()
    consecutive_failures = 0
    prev_handoff: Path | None = None
    hard_stop_reason = ""

    for index, task in enumerate(to_run, start=1):
        if index > args.max_tasks:
            break
        elapsed_h = (time.monotonic() - started) / 3600
        if elapsed_h >= args.max_hours:
            hard_stop_reason = f"总时长超 {args.max_hours}h"
            break
        if consecutive_failures >= args.max_consecutive_failures:
            hard_stop_reason = f"连续失败 {consecutive_failures} 次"
            break

        handoff_path = night_dir / f"handoff-{index:03d}-{task.task_id}.md"
        log_path = night_dir / f"session-{index:03d}.jsonl"
        head_sha = git(REPO_ROOT, "rev-parse", "HEAD")
        brief = render_brief(
            task, handoff_path, prev_handoff, head_sha, f"zx-task show {task.task_id}"
        )
        print(f"=== [{index}/{len(to_run)}] {task.label} ===", flush=True)
        task_started = time.monotonic()
        result = TaskResult(task=task, status="挂起", handoff_path=handoff_path)
        try:
            rc = run_session(
                claude_bin=args.claude_bin,
                brief=brief,
                handoff_path=handoff_path,
                task_id=task.task_id,
                log_path=log_path,
                timeout_s=args.max_task_minutes * 60,
                repo=REPO_ROOT,
                model=args.model,
            )
            if rc != 0:
                result.note = f"会话退出码 {rc}"
        except SessionTimeout as exc:
            result.note = str(exc)
            result.violations.append(str(exc))  # 墙钟超时是硬停条件，不只是失败

        problems = validate_handoff(handoff_path)
        if not handoff_path.exists():
            write_stub_handoff(handoff_path, task, result.note or "会话未留下交接棒")
            consecutive_failures += 1
        elif problems:
            # 超限/缺节的交接棒**保留现场**（代写桩会湮灭会话证据），照记违规
            consecutive_failures += 1
        else:
            consecutive_failures = 0
            result.status = "部分完成" if result.note else "完成"

        result.violations = (
            result.violations + problems + post_checks(REPO_ROOT, head_sha, log_path)
        )
        decisions, open_items, evidence = parse_handoff(handoff_path)
        result.decisions, result.open_items, result.evidence = decisions, open_items, evidence
        result.duration_s = time.monotonic() - task_started
        results.append(result)
        prev_handoff = handoff_path
        print(f"    → {result.status}（{result.duration_s:.0f}s）", flush=True)

        if result.violations:
            hard_stop_reason = "硬复查违规：" + result.violations[0]
            break

    for task in parked:
        results.append(
            TaskResult(
                task=task,
                status="跳过",
                note=is_q1_gated(task) or "用户裁决位",
                open_items=[f"夜间跳过：{is_q1_gated(task) or '用户裁决位'}，要跑请白天点名"],
            )
        )

    night_shift = reports / f"night-shift-{args.date}.md"
    write_night_shift(night_shift, args.date, results, mode)
    pack_path = reports / f"decision-pack-{args.date}.md"
    has_pack = write_decision_pack(pack_path, args.date, results)

    print(f"夜班报告：{night_shift}")
    if has_pack:
        print(f"决策包：{pack_path}")
    if hard_stop_reason:
        print(f"⚠️ 提前终止：{hard_stop_reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
