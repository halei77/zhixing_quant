"""`zx-daily`：每日盘后采集的命令行入口（01 路线图 Step 2 第 4 项，验收 4）。

只做装配，不做判定：主数据与日历从快照读，日线从源抓，抓与判与落盘与报都在 `daily.py`，
路径由这里给（`storage.layout` 的默认根就是 `config.parquet_dir()`）。所以这个文件里
没有一条业务规则——规则住在有测试的地方，这里只负责"怎么被叫起来"。

不调度自己：定时器（cron / systemd）装在用户机器上，属于"先问"（05 Q1）。这个入口的
存在就是为了让那一步只是一行命令，而不是一次改造。

主数据与日历不在这里刷新：它们变化以周计，靠 `tools/capture_golden.py` 手动重抓。任务
只检查日历覆盖不覆盖今天，判不出就报错（`last_trading_day`）——"静默用三个月前的日历
跑完今天"是这里最不该发生的事。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from zhixing_quant.domain.security import SecurityMaster
from zhixing_quant.sources.akshare import fetch as akshare_fetch
from zhixing_quant.sources.akshare.calendar import load_calendar
from zhixing_quant.sources.akshare.master import read_master
from zhixing_quant.sources.jobs import daily as job
from zhixing_quant.sources.rows import SourceSchemaError
from zhixing_quant.storage.quarantine import store_quarantined
from zhixing_quant.storage.write import store_bars


def _days(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期要写成 2024-01-31：{text!r}") from exc


def _symbols(text: str) -> list[str]:
    return [code.strip() for code in text.split(",") if code.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zx-daily", description="每日盘后采集：抓取 → 门禁 → 日报"
    )
    parser.add_argument(
        "--day", type=_days, default=None, help="报告日，默认取今天（非交易日则回溯）"
    )
    parser.add_argument(
        "--symbols",
        type=_symbols,
        default=None,
        metavar="代码[,代码…]",
        help="只跑这几只票，默认跑主数据当天的在册名单",
    )
    parser.add_argument("--limit", type=int, default=None, help="股票池只取前 N 只（试跑用）")
    parser.add_argument(
        "--previous-days",
        type=int,
        default=1,
        help="往回多带几个交易日（R007 至少要 1；默认值就是它）",
    )
    parser.add_argument("--attempts", type=int, default=3, help="单只票的抓取次数上限")
    parser.add_argument(
        "--breaker",
        type=int,
        default=20,
        help="连败这么多只就收工（源整体宕掉时不去撞三千万次请求）",
    )
    return parser


def main(argv: list[str] | None = None, *, fetcher: job.Fetcher | None = None) -> int:
    """跑一次盘后采集，把日报打到标准输出。退出码：判成 0，一只都没判成 1。

    `fetcher` 是测试与"换源"的注入点，命令行上不暴露：这个入口存在的意义就是"用 akshare
    抓 akshare 的日线"，能随手换成假抓取的那就不再是采集任务，而是又一条测试通路。
    """
    args = build_parser().parse_args(argv)
    grab = akshare_fetch.fetch_daily if fetcher is None else fetcher
    try:
        calendar = load_calendar(until=args.day)
        result = job.run(
            args.day,
            fetch=grab,
            store=store_bars,
            quarantine=store_quarantined,
            master=SecurityMaster(read_master().listings),
            calendar=calendar,
            symbols=args.symbols,
            limit=args.limit,
            previous_days=args.previous_days,
            attempts=args.attempts,
            breaker=args.breaker,
        )
    except SourceSchemaError as exc:
        # 快照缺失/过期是"今天根本没开始"，和"开始了但一只票都没抓到"是两条诊断。
        print(f"采集中止：{exc}", file=sys.stderr)
        return 2
    print(result.markdown)
    return 0 if result.ok else 1
