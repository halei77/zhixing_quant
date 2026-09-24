"""`zx-minute`：分钟线采集的命令行入口（01 Step 4；ADR-0009 决定 8）。

只做装配，和 `zx-daily` 一样：判定、重试、落盘、日报都在 `jobs.minute`，周期 → dataset 的
映射在 `storage.layout`，**周期 → 源**（1 → ngw `ngw_minute_1`，5/30/60 → akshare/sina
`akshare_minute_*`）也住在 `jobs.minute`（`live_fetcher` 抓取分派 + `drafts_for` 行解析分派，
同一个 `NGW_PERIOD` 键）。这个文件里没有一条业务规则。

它**拒绝猜范围**：`--symbols` 与 `--limit` 都不给时不启动（退出码 2）。全市场三周期约 13290
请求、2–3 小时/日，而"采几个周期、票池多大"是 ADR-0009 决定 8 里等用户裁决的那一条——给一个
默认值等于替人裁决，那是这条管道唯一不该自己决定的事。

定时器不在这里装（05 Q1）。它与 `zx-daily` 串在同一个盘后窗口里跑，日线在前：分钟线的对账
要比的就是"今天的日线在不在"，顺序倒了会每天白报一次断档。
"""

from __future__ import annotations

import argparse
import sys
from functools import partial

from zhixing_quant import config
from zhixing_quant.sources.akshare.calendar import load_calendar
from zhixing_quant.sources.akshare.master import read_master
from zhixing_quant.sources.jobs import minute as job
from zhixing_quant.sources.jobs.cli import day_arg, symbols_arg
from zhixing_quant.sources.rows import SourceSchemaError
from zhixing_quant.storage import layout
from zhixing_quant.storage.quarantine import store_quarantined
from zhixing_quant.storage.write import store_bars

#: 默认周期 = ADR-0009 决定 7 的三周期（ADR-0021 决定 2 管的是"每日必采范围 = 60"，不改这里
#: 的 CLI 默认）。**1 分钟不进默认**：它的源是 ngw、按 `--periods 1`（或含 1 的列表）显式开
#: （ADR-0022 决定 3）。票池仍然要显式给，见模块说明。
DEFAULT_PERIODS = ("5", "30", "60")


def _periods(text: str) -> tuple[str, ...]:
    """`--periods 5,30` → 周期元组。认不出的周期在这里就报，不等第一个请求打完再发现没处落。"""
    periods = tuple(p.strip() for p in text.split(",") if p.strip())
    if not periods:
        raise argparse.ArgumentTypeError("--periods 至少要写一个周期（1/5/30/60）")
    for period in periods:
        try:
            layout.minute_dataset(period)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(str(exc)) from exc
    return periods


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zx-minute", description="分钟线采集：抓取 → 门禁 → minute_* dataset → 日报"
    )
    parser.add_argument(
        "--day", type=day_arg, default=None, help="报告日，默认取今天（非交易日回溯）"
    )
    parser.add_argument(
        "--periods",
        type=_periods,
        default=DEFAULT_PERIODS,
        metavar="1[,5…]",
        help=(
            f"采哪几个周期，默认 {','.join(DEFAULT_PERIODS)}；"
            f"{job.NGW_PERIOD} 走 ngw（源标识 ngw_minute_1），5/30/60 走 sina（akshare_minute_*）"
        ),
    )
    parser.add_argument(
        "--symbols",
        type=symbols_arg,
        default=None,
        metavar="代码[,代码…]",
        help="只跑这几只票（不给就必须给 --limit）",
    )
    parser.add_argument("--limit", type=int, default=None, help="股票池只取前 N 只")
    parser.add_argument("--attempts", type=int, default=3, help="单个请求的抓取次数上限")
    parser.add_argument(
        "--breaker",
        type=int,
        default=20,
        help="连败这么多**请求**就收工（分钟线一只票有多个请求，所以数的是请求）",
    )
    return parser


def main(argv: list[str] | None = None, *, fetcher: job.MinuteFetcher | None = None) -> int:
    """跑一次分钟线采集，把分钟日报打到标准输出。退出码同 `zx-daily`：判成 0，没判成 1，没开始 2。

    `fetcher` 是抓取边界的注入点（测试与"换源"用它），命令行上不暴露——理由与 `zx-daily` 同一条。
    """
    args = build_parser().parse_args(argv)
    # 周期 → 源的抓取分派在 job.live_fetcher 里（与行解析分派同一个 NGW_PERIOD 键）：
    # 这里再写一遍 "1 走 ngw" 就是第二份周期表，而它迟早和 drafts_for 漂开。
    grab = job.live_fetcher(args.periods) if fetcher is None else fetcher
    try:
        calendar = load_calendar(until=args.day)
        result = job.run(
            args.day,
            fetch=grab,
            # 一个周期一个 store：dataset 名在这里由 `layout` 说一次，任务层不再猜
            stores={
                period: partial(store_bars, dataset=layout.minute_dataset(period))
                for period in args.periods
            },
            quarantine=store_quarantined,
            master=read_master().to_master(),
            calendar=calendar,
            directory=config.reports_dir() / job.MINUTE_REPORTS,
            # 对账读的是干净区那一层，与 `stores` 里那些 `store_bars` 同一个 root（默认都是
            # `config.parquet_dir()`）：写成数据根的话两边都读到空目录，"账对上了"就成了假话。
            reconcile=partial(job.reconcile_pool, root=config.parquet_dir()),
            symbols=args.symbols,
            limit=args.limit,
            attempts=args.attempts,
            breaker=args.breaker,
        )
    except (SourceSchemaError, job.ScopeNotConfigured) as exc:
        # 快照读不出、与"没人告诉我要跑多大"都是"今天根本没开始"，和"开始了但一只没判成"
        # 是两条诊断（后者是退出码 1）。
        print(f"采集中止：{exc}", file=sys.stderr)
        return 2
    print(result.markdown)
    return 0 if result.ok else 1
