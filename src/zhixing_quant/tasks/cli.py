"""任务流水线 CLI（09 §七）。

CLI 是 agent 迁移状态的唯一入口（09 §二"只能通过 CLI 迁移状态"），所以退出码
本身就是契约的一部分：0 已写入，2 状态机拒绝，3 目标不存在。写库失败不会留下
半个状态——校验全在 Store 里先跑完。
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from zhixing_quant import config
from zhixing_quant.tasks import db, models
from zhixing_quant.tasks.store import Backtest, Event, Store, Task, TaskNotFound

EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_NOT_FOUND = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zx-task", description="任务流水线与回测台账（09）")
    parser.add_argument("--db", type=Path, default=None, help="任务库路径，默认取数据根")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="登记任务")
    p.add_argument("title")
    p.add_argument("--type", required=True, choices=list(models.TASK_TYPE_LABELS))
    p.add_argument("--step", required=True, help="关联 Step，如 0d / 1")
    p.add_argument("--refs", default="", help="关联验收文档，逗号分隔")

    p = sub.add_parser("board", help="任务看板")
    p.add_argument("--status", choices=list(models.STATUS_LABELS))

    p = sub.add_parser("show", help="单任务时间线")
    p.add_argument("id", type=int)

    p = sub.add_parser("advance", help="当前阶段通过 / 标为不适用，进入下一阶段")
    p.add_argument("id", type=int)
    p.add_argument("--evidence", default="", help="通过证据（CI 记录 / 报告路径 / 对照表）")
    p.add_argument("--na-reason", default="", help="本阶段不适用的理由")
    p.add_argument("--actor", default="agent")

    p = sub.add_parser("reject", help="打回到更早的阶段")
    p.add_argument("id", type=int)
    p.add_argument("--to", required=True, help="回退到的阶段")
    p.add_argument("--reason", required=True, help="结构化原因")
    p.add_argument("--note", required=True, help="补充说明")
    p.add_argument("--actor", default="agent")

    p = sub.add_parser("conclude", help="功能验收通过并下结论")
    p.add_argument("id", type=int)
    p.add_argument("--verdict", required=True, help="通过 / 不通过")
    p.add_argument("--evidence", required=True, help="验收对照证据")
    p.add_argument("--actor", default="agent")

    for name, hint in (("resume", "解除挂起"), ("abandon", "作废并关闭")):
        p = sub.add_parser(name, help=f"用户裁决：{hint}")
        p.add_argument("id", type=int)
        p.add_argument("--note", required=True, help="裁决理由")
        p.add_argument("--actor", default="user")

    pitfall = sub.add_parser("pitfall", help="坑表")
    psub = pitfall.add_subparsers(dest="pitfall_cmd", required=True)
    p = psub.add_parser("add", help="登记坑")
    p.add_argument("--symptom", required=True)
    p.add_argument("--root-cause", required=True)
    p.add_argument("--workaround", required=True)
    p.add_argument("--related", default="", help="关联任务 id，逗号分隔")
    p = psub.add_parser("search", help="检索坑表（开工前必查，09 §八-1）")
    p.add_argument("keyword", nargs="?", default="")

    backtest = sub.add_parser("backtest", help="回测台账")
    bsub = backtest.add_subparsers(dest="backtest_cmd", required=True)
    p = bsub.add_parser("add", help="登记一次回测")
    p.add_argument("--strategy", required=True)
    p.add_argument("--version", required=True)
    p.add_argument("--params-hash", required=True)
    p.add_argument("--start", required=True, help="数据区间起")
    p.add_argument("--end", required=True, help="数据区间止")
    p.add_argument("--cost", required=True, help="成本假设")
    p.add_argument("--report", required=True, help="报告路径")
    p.add_argument("--status", required=True, help="通过 / 失败 / 作废")
    p.add_argument("--task", type=int, default=None)
    p.add_argument("--metrics-in", default="")
    p.add_argument("--metrics-out", default="")
    p = bsub.add_parser("list", help="台账查询")
    p.add_argument("--strategy", default=None)
    p = bsub.add_parser("show", help="单条回测")
    p.add_argument("id", type=int)
    return parser


def _print_task(task: Task) -> None:
    print(
        f"#{task.id} [{task.type}] {task.title}｜Step {task.step}｜"
        f"{task.stage_label}｜{task.status_label}｜打回 {task.reject_count}"
    )


def _print_event(event: Event) -> None:
    move = f"{models.stage_label(event.from_stage)}→{models.stage_label(event.to_stage)}"
    detail = " ".join(x for x in (event.reason, event.evidence) if x)
    print(f"  {event.at:%Y-%m-%d %H:%M:%S} [{event.actor}] {event.action} {move} {detail}".rstrip())


def _print_backtest(bt: Backtest) -> None:
    print(
        f"#{bt.id} {bt.strategy} {bt.version} 第{bt.run_no}次 [{bt.status_label}] "
        f"{bt.data_start}~{bt.data_end} 成本={bt.cost_assumption} → {bt.report_path}"
    )


def _print_backtest_detail(bt: Backtest) -> None:
    """list 只给一行摘要，show 要能看见 09 §六 列的样本内外指标——否则这两列只能靠 sqlite 手查。"""
    _print_backtest(bt)
    task = f"#{bt.task_id}" if bt.task_id is not None else "无"
    print(f"  关联任务：{task}｜参数哈希 {bt.params_hash}")
    print(f"  样本内：{bt.metrics_in or '未填'}")
    print(f"  样本外：{bt.metrics_out or '未填'}")


def _run(store: Store, args: argparse.Namespace) -> int:
    if args.cmd == "new":
        _print_task(store.create_task(args.title, args.type, args.step, args.refs))
    elif args.cmd == "board":
        for task in store.board(args.status):
            _print_task(task)
    elif args.cmd == "show":
        task = store.get_task(args.id)
        _print_task(task)
        if task.summary:
            print(f"  结论：{task.summary}")
        for event in store.timeline(args.id):
            _print_event(event)
    elif args.cmd == "advance":
        _print_task(store.advance(args.id, args.evidence, args.na_reason, args.actor))
    elif args.cmd == "reject":
        _print_task(store.reject(args.id, args.to, args.reason, args.note, args.actor))
    elif args.cmd == "conclude":
        _print_task(store.conclude(args.id, args.verdict, args.evidence, args.actor))
    elif args.cmd in ("resume", "abandon"):
        decide = store.resume if args.cmd == "resume" else store.abandon
        _print_task(decide(args.id, args.note, args.actor))
    elif args.cmd == "pitfall":
        return _run_pitfall(store, args)
    elif args.cmd == "backtest":
        return _run_backtest(store, args)
    else:  # pragma: no cover - argparse 已拦住未知子命令
        raise _unreachable(args.cmd)
    return EXIT_OK


def _run_pitfall(store: Store, args: argparse.Namespace) -> int:
    if args.pitfall_cmd == "add":
        pid = store.add_pitfall(args.symptom, args.root_cause, args.workaround, args.related)
        print(f"坑 #{pid} 已登记")
    else:
        hits = store.search_pitfalls(args.keyword)
        if not hits:
            print("坑表无匹配")
        for p in hits:
            print(f"#{p.id} 现象：{p.symptom}\n   根因：{p.root_cause}\n   规避：{p.workaround}")
    return EXIT_OK


def _run_backtest(store: Store, args: argparse.Namespace) -> int:
    if args.backtest_cmd == "add":
        _print_backtest(
            store.add_backtest(
                strategy=args.strategy,
                version=args.version,
                params_hash=args.params_hash,
                data_start=args.start,
                data_end=args.end,
                cost_assumption=args.cost,
                report_path=args.report,
                status=args.status,
                task_id=args.task,
                metrics_in=args.metrics_in,
                metrics_out=args.metrics_out,
            )
        )
    elif args.backtest_cmd == "list":
        for bt in store.list_backtests(args.strategy):
            _print_backtest(bt)
    else:
        _print_backtest_detail(store.get_backtest(args.id))
    return EXIT_OK


def _unreachable(value: object) -> Never:  # pragma: no cover - argparse 已拦住未知子命令
    raise AssertionError(f"未处理的子命令：{value}")


def main(argv: Sequence[str] | None = None, db_path: Path | None = None) -> int:
    """db_path 供测试与程序内调用使用，优先级高于 --db（后者是给命令行用户的）。"""
    args = build_parser().parse_args(argv)
    path = db_path if db_path is not None else (args.db or config.taskdb_file())
    con = db.connect(path)
    try:
        return _run(Store(con), args)
    except models.StageError as exc:
        print(f"拒绝：{exc}", file=sys.stderr)
        return EXIT_REJECTED
    except TaskNotFound as exc:
        print(f"不存在：#{exc.args[0]}", file=sys.stderr)
        return EXIT_NOT_FOUND
    finally:
        con.close()
