"""`zx-backup`：备份与恢复演练的命令行入口（ADR-0005 决定 4、补充决定二与五）。

只做装配，跟 `zx-daily`、`zx-minute` 一样：账在 `state.py`，字节在 `run.py`，恢复与演练在
`restore.py`，这个文件里没有一条备份规则。

退出码沿用那三档：0 这次跑成了；1 跑成了但结论是"不行"（恢复出来的字节与清单对不上、演练没读出
东西——它们是一次**跑完了**的动作，只是备份被证明不可用）；2 根本没开始（没配备份根、两棵树套在
一起、还没有基线、账读坏了）。把"没判成"与"没开始"分成两档的理由与采集那两条任务同一条：告警要
知道的是今天有没有真跑。

它跑完就退出。定时器不在这儿装（补充决定五）：`cron` 与 `systemd timer` 各一份的样子写在
ADR-0005 补充决定五末尾，装与不装由人——那一步改的是机器上的服务。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

from zhixing_quant import config
from zhixing_quant.backup import restore as drill
from zhixing_quant.backup import run
from zhixing_quant.backup.state import FULL, load_state

#: 报告归档目录名（数据根下 `reports/backup/`，与分钟日报共用 `reports/` 那个容器）。
REPORTS = "backup"


@dataclass(frozen=True)
class Outcome:
    """一次动作的结果：给人看的那段话，与退出码。"""

    text: str
    code: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zx-backup",
        description=(
            "数据根备份（ADR-0005）：full 建立基线、incr 只拷变化、restore 拼回来、verify 演练。\n"
            f"备份根从 {config.BACKUP_ENV} 读，没有默认值——不配就直接退出（补充决定一）。"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        (FULL, "整棵数据根拷成一代基线，并裁掉超出保留名额的旧代"),
        ("incr", "只拷自基线以来变化的文件，落一代增量"),
        ("restore", "把某一天的树叠出来（要一个空目录）"),
        ("verify", "恢复演练：挑一个数据集恢复出去、逐文件对哈希、再用查询层真读一遍"),
    ):
        command = sub.add_parser(name, help=help_text)
        command.add_argument(
            "--day",
            type=_day_arg,
            default=None,
            metavar="YYYY-MM-DD",
            help="这一代备份的日期，默认今天",
        )
        if name in ("restore", "verify"):
            command.add_argument(
                "--into",
                type=Path,
                required=True,
                metavar="目录",
                help="恢复出去的目标：空目录或不存在的路径（跑完自己删，工具不删目录）",
            )
        if name == "verify":
            command.add_argument(
                "--dataset",
                default=None,
                help="验哪个 dataset；不挑就自动选那几代里覆盖文件最多的那个",
            )
    return parser


def _day_arg(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--day 要的是 YYYY-MM-DD，收到的是 {text!r}") from exc


def clean_zone() -> PurePosixPath:
    """干净区在数据根里的那一段（今天读出来是 `data`）。

    由 `config` 那两个派生路径相减得到，而不是在这里写那个字符串：备份层一旦自己拼"数据根下面
    那个目录叫 data"，`config.parquet_dir()` 改了名，这里就会静默挑不到数据集。
    """
    return PurePosixPath(config.parquet_dir().relative_to(config.data_root()).as_posix())


def _archive(when: date, name: str, markdown: str) -> None:
    """报告落数据根：跑过就得留下能查的东西（04 §四 那条在这里同样成立）。"""
    path = config.reports_dir() / REPORTS / f"{when.isoformat()}-{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """跑一次备份相关的动作。退出码三档见模块说明。"""
    args = build_parser().parse_args(argv)
    when: date = args.day if args.day is not None else date.today()
    try:
        backup = config.backup_dir()
        outcome = _dispatch(args, when=when, data=config.data_root(), backup=backup)
    except (ValueError, OSError) as exc:
        # 没配备份根、两棵树套在一起、账没有或读坏、那天没有基线——全是"今天没开始"。
        print(f"备份中止：{exc}", file=sys.stderr)
        return 2
    print(outcome.text)
    return outcome.code


def _dispatch(args: argparse.Namespace, *, when: date, data: Path, backup: Path) -> Outcome:
    """一条命令一段装配。分四支的理由是"报出来的东西"确实不同：计数报告 / 一棵树 / 演练结论。"""
    command = str(args.command)
    if command == FULL or command == "incr":
        made = (
            run.full(when, data=data, backup=backup)
            if command == FULL
            else run.incremental(when, data=data, backup=backup)
        )
        _archive(when, command, made.markdown)
        return Outcome(made.markdown, 0)
    into: Path = args.into
    if command == "restore":
        return _restore(backup, into=into, when=when)
    dataset: str | None = args.dataset
    return _verify(backup, into=into, when=when, dataset=dataset)


def _restore(backup: Path, *, into: Path, when: date) -> Outcome:
    what = drill.plan(backup, when)
    restored = drill.restore(backup, into, what)
    tail = (
        "字节全部与清单一致"
        if restored.intact
        else f"备份里有 {len(restored.bad)} 个文件读坏了：{'、'.join(restored.bad)}"
    )
    state = load_state(backup)
    return Outcome(
        f"恢复 {when.isoformat()} → `{into}`\n"
        f"- 全量 {what.base.isoformat()} 叠加 {len(what.deltas)} 代增量 → "
        f"{len(restored.files)} 个文件 / {restored.total_bytes / 1024 / 1024:.2f} MiB\n"
        f"- {tail}\n"
        f"- 账上的基线仍是 {state.base.isoformat()}：restore 只读，不写账\n",
        0 if restored.intact else 1,
    )


def _verify(backup: Path, *, into: Path, when: date, dataset: str | None) -> Outcome:
    done = drill.verify(backup, clean_zone=clean_zone(), into=into, dataset=dataset, as_of=when)
    _archive(when, "verify", done.markdown)
    return Outcome(done.markdown, 0 if done.ok else 1)
