"""夜间无人值守驱动的机器判据（ADR-0023、09 §十）。

为什么测它：交接棒契约（六节、≤150 行、≤8KB）和 Q1 滤网是无人值守的护栏，
只写在文档里等于没有护栏；驱动自身的循环也要能离线跑通（假 claude，不打 API）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import night_driver as nd  # noqa: E402

FAKE_CLAUDE = REPO_ROOT / "tools" / "night" / "fake_claude.sh"

BOARD_FIXTURE = """\
#33 [docs] R011 容差要不要按 493 天全量重标定（改 tolerance_pct）｜Step 4b｜开发｜进行中｜打回 0
#57 [data] 停牌登记补全：区间并集合并后进主数据快照｜Step 3｜开发｜进行中｜打回 0
#58 [feat] 参考表黄金样本更新与上线部署｜Step 5｜功能验收｜进行中｜打回 0
#1 [feat] Step 0d 任务流水线与回测台账｜Step 0d｜结论｜已结｜打回 0
"""


@pytest.fixture
def zx_task_stub(tmp_path: Path) -> Path:
    stub = tmp_path / "zx-task"
    stub.write_text(f'#!/usr/bin/env bash\ncat << "EOF"\n{BOARD_FIXTURE}EOF\n', encoding="utf-8")
    stub.chmod(0o755)
    return stub


@pytest.fixture
def night_env(tmp_path: Path, zx_task_stub: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_root = tmp_path / "data"
    monkeypatch.setenv("ZX_DATA_ROOT", str(data_root))
    monkeypatch.setenv("NIGHT_ZX_TASK", str(zx_task_stub))
    monkeypatch.setenv("NIGHT_CLAUDE_BIN", str(FAKE_CLAUDE))
    monkeypatch.delenv("FAKE_MODE", raising=False)
    return data_root


def test_parse_board() -> None:
    tasks = nd.parse_board(BOARD_FIXTURE)
    assert [t.task_id for t in tasks] == ["33", "57", "58", "1"]
    assert tasks[1].title.startswith("停牌登记补全")
    assert tasks[0].phase == "开发"


def test_q1_gate_keywords_and_phase() -> None:
    gated = nd.Task(task_id="58", title="参考表黄金样本更新与上线部署", phase="功能验收")
    reason = nd.is_q1_gated(gated)
    assert reason and any(word in reason for word in ("验收", "黄金样本", "上线"))
    open_task = nd.Task(task_id="57", title="停牌登记补全", phase="开发")
    assert nd.is_q1_gated(open_task) == ""


def test_pick_tasks_parks_q1_but_follows_user_list() -> None:
    board = nd.parse_board(BOARD_FIXTURE)
    chosen, parked = nd.pick_tasks(board, [], max_tasks=5)
    assert [t.task_id for t in chosen] == ["33", "57"]
    assert [t.task_id for t in parked] == ["58"]

    chosen_user, parked_user = nd.pick_tasks(board, ["58", "57"], max_tasks=5)
    assert [t.task_id for t in chosen_user] == ["58", "57"]
    assert parked_user == []


def test_validate_handoff_contract(tmp_path: Path) -> None:
    tmp = tmp_path
    good = tmp / "handoff-good.md"
    good.write_text(
        "# 交接棒 · #57 · 完成\n\n" + "\n".join(f"{s}\n\n- 内容\n" for s in nd.HANDOFF_SECTIONS),
        encoding="utf-8",
    )
    assert nd.validate_handoff(good) == []

    missing = tmp / "handoff-missing.md"
    missing.write_text("# 交接棒 · #57 · 完成\n" + nd.HANDOFF_SECTIONS[0] + "\n", encoding="utf-8")
    problems = nd.validate_handoff(missing)
    assert len(problems) == len(nd.HANDOFF_SECTIONS) - 1

    fat = tmp / "handoff-fat.md"
    fat.write_text(
        "# 交接棒\n" + "\n".join(f"{s}\n" + "日志灌水\n" * 30 for s in nd.HANDOFF_SECTIONS),
        encoding="utf-8",
    )
    assert any("行" in p for p in nd.validate_handoff(fat))
    assert not (tmp / "handoff-none.md").exists()
    assert nd.validate_handoff(tmp / "handoff-none.md") != []


def test_stub_handoff_is_always_valid(tmp_path: Path) -> None:
    path = tmp_path / "handoff-001-57.md"
    nd.write_stub_handoff(path, nd.Task(task_id="57", title="停牌登记补全"), "会话超时")
    assert nd.validate_handoff(path) == []
    text = path.read_text(encoding="utf-8")
    assert "挂起" in text and "会话超时" in text


def test_render_brief_is_pointer_only_and_bounded(tmp_path: Path) -> None:
    prev = tmp_path / "handoff-000-1.md"
    prev.write_text("stub", encoding="utf-8")
    brief = nd.render_brief(
        nd.Task(task_id="57", title="停牌登记补全", step="3"),
        tmp_path / "handoff-001-57.md",
        prev,
        "abc1234",
        "zx-task show 57",
    )
    encoded = brief.encode("utf-8")
    assert len(encoded) <= nd.MAX_BRIEF_BYTES
    assert "#57" in brief and "停牌登记补全" in brief and "abc1234" in brief
    assert str(prev) in brief
    assert "docs/01-路线图.md" not in brief or "README §5" in brief  # 只指路，不内嵌全文


def test_parse_handoff_sections(tmp_path: Path) -> None:
    path = tmp_path / "handoff.md"
    path.write_text(
        "# 交接棒 · #57 · 部分完成\n"
        f"{nd.HANDOFF_SECTIONS[1]}\n- 选了 A / 理由 B / 代价 C\n"
        f"{nd.HANDOFF_SECTIONS[3]}\n- commit abc1234\n"
        f"{nd.HANDOFF_SECTIONS[4]}\n- 一件待裁决（Q1 测试权限节点）\n"
        f"{nd.HANDOFF_SECTIONS[5]}\n- 下一个\n",
        encoding="utf-8",
    )
    decisions, open_items, evidence = nd.parse_handoff(path)
    assert decisions == ["- 选了 A / 理由 B / 代价 C"]
    assert open_items == ["- 一件待裁决（Q1 测试权限节点）"]
    assert evidence == ["- commit abc1234"]


def test_extract_bash_commands_ignores_brief_text(tmp_path: Path) -> None:
    log = tmp_path / "session.jsonl"
    log.write_text(
        '{"type":"user","message":{"content":"禁止 git reset --hard"}}\n'
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash",'
        '"input":{"command":"git commit -m x"}}]}}\n'
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash",'
        '"input":{"command":"git reset --hard HEAD~1"}}]}}\n',
        encoding="utf-8",
    )
    commands = nd.extract_bash_commands(log)
    assert commands == ["git commit -m x", "git reset --hard HEAD~1"]


def test_dry_run_lists_plan(night_env: Path) -> None:
    assert nd.main(["--dry-run", "--max-tasks", "2"]) == 0
    assert (night_env / "reports" / "night").is_dir()  # 目录会建，但不起会话


def test_full_loop_success_archives_handoff_and_report(
    night_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "success")
    code = nd.main(["--tasks", "57", "--max-tasks", "1"])
    assert code == 0
    night_dir = night_env / "reports" / "night"
    (date_dir,) = [p for p in night_dir.iterdir() if p.is_dir()]
    handoffs = sorted(date_dir.glob("handoff-*.md"))
    assert [h.name for h in handoffs] == ["handoff-001-57.md"]
    assert nd.validate_handoff(handoffs[0]) == []
    shift = night_env / "reports" / f"night-shift-{date_dir.name}.md"
    assert shift.exists() and "今晚上线了什么" in shift.read_text(encoding="utf-8")


def test_hangup_writes_stub_and_stops_after_consecutive_failures(
    night_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "hangup")
    code = nd.main(["--tasks", "57", "33", "--max-tasks", "2", "--max-consecutive-failures", "1"])
    assert code == 1
    (date_dir,) = [p for p in (night_env / "reports" / "night").iterdir() if p.is_dir()]
    handoffs = sorted(date_dir.glob("handoff-*.md"))
    assert len(handoffs) == 1  # 第一次挂起即停，没跑第二个任务
    assert "挂起" in handoffs[0].read_text(encoding="utf-8")


def test_destructive_command_trips_hard_stop(
    night_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "destructive")
    code = nd.main(["--tasks", "57", "--max-tasks", "1"])
    assert code == 1
    shift = next((night_env / "reports").glob("night-shift-*.md"))
    assert "破坏性命令" in shift.read_text(encoding="utf-8")


def test_oversize_handoff_is_flagged_in_report(
    night_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "fat")
    code = nd.main(["--tasks", "57", "--max-tasks", "1"])
    assert code == 1
    shift = next((night_env / "reports").glob("night-shift-*.md"))
    assert "行" in shift.read_text(encoding="utf-8")


def test_partial_and_q1_parked_feed_decision_pack(
    night_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MODE", "partial")
    code = nd.main(["--max-tasks", "1"])  # 自主模式：#57 跑、#58 挂起
    assert code == 0
    pack = next((night_env / "reports").glob("decision-pack-*.md"))
    text = pack.read_text(encoding="utf-8")
    assert "待用户裁决" in text and "R011 容差" in text and "黄金样本" in text


def test_real_templates_pass_own_validator() -> None:
    """模板本身就是合格交接棒骨架——契约与模板漂移在 CI 里现形。"""
    assert nd.validate_handoff(nd.HANDOFF_TMPL) == []
    brief = nd.render_brief(
        nd.Task(task_id="1", title="模板自检", step="0"),
        Path("/tmp/handoff-tmpl.md"),
        None,
        "sha",
        "zx-task show 1",
    )
    assert len(brief.encode("utf-8")) <= nd.MAX_BRIEF_BYTES


def test_fake_claude_timeout_path(night_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "slow")
    code = nd.main(["--tasks", "57", "--max-tasks", "1", "--max-task-minutes", "0.02"])
    assert code == 1
    (date_dir,) = [p for p in (night_env / "reports" / "night").iterdir() if p.is_dir()]
    assert "挂起" in (date_dir / "handoff-001-57.md").read_text(encoding="utf-8")


def test_driver_help_runs() -> None:
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "night_driver.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0
    assert "一任务一全新会话" in out.stdout
