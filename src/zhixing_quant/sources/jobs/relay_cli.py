"""`zx-relay`：转接源参考表的采集入口（ADR-0015 决定 3、5）。

stk_limit 一天一次返回全市场（Qoute 实测），分页 limit/offset 拉取。**2026-09-22 实测**：
rds 单次查询在 5000 行处静默截断（响应声称 count=5644、has_more=True，但 offset≥5000
一律空页），**全市场拉取在本源不可用**——截断由 `has_more` 硬响，不会无声丢行。日常用
`--symbols` 逐票拉取；`--symbols` 缺省 = 不过滤（受上述截断约束，报告会印缺口）。

锚点对账（决定 3）：涨跌幅 = 涨停价 ÷ 干净区日线昨收（不复权对不复权），越过 R004 档位
+1pp 的行即整批拒——整页不可信，一行都不写。锚不上（主数据没这票、日线缺昨收）的行
**剔除并计数**：那不是"对不上"的证据，是"没得对"，与超阈是两回事，报告里分开说。

退出码：0 跑完（含"那天无行"）；1 锚点对账整批拒；2 没开始（参数/网络/key）。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from zhixing_quant import config
from zhixing_quant.backtest.cli import check_range
from zhixing_quant.storage import tables
from zhixing_quant.storage.tables import TableWriteReport

FetchFn = Callable[..., tuple[str, dict[str, Any]]]
PrevCloseFn = Callable[[str, date], float | None]
LimitPctFn = Callable[[str], float]

ANCHOR_TOLERANCE_PP = 1.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zx-relay", description="转接源参考表采集（ADR-0015）：拉一天、锚点对账、入干净区"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    pull = sub.add_parser("pull", help="拉某个交易日的整张表")
    pull.add_argument("--table", default="stk_limit", help="参考表名，默认 stk_limit")
    pull.add_argument("--day", required=True, metavar="YYYY-MM-DD", help="要哪个交易日")
    pull.add_argument("--symbols", default=None, help="逗号分隔的票池过滤；缺省全市场入库")
    pull.add_argument("--page-size", type=int, default=5000, help="分页大小，缺省 5000")
    pull.add_argument(
        "--out", type=Path, default=None, help="报告目录，默认 <数据根>/reports/relay/<今天>"
    )
    return parser


def fetch_all_pages(
    table: str,
    day: date,
    *,
    fetch: FetchFn,
    page_size: int = 5000,
) -> tuple[str, list[list[str]], list[str], int | None]:
    """分页拉完整天。返回 (供数源, 原始 items, fields, 源声称的总行数或 None)。

    **截断必须硬响**（2026-09-22 实测）：rds 在 offset≥5000 处一律返回空，而响应里
    `count=5644, has_more=True` 照给——"下一页空"在该源有两种含义（拉完了 / 源截断了），
    只有 `has_more` 能分辨。把它当"拉完了"就是无声丢 11% 的行，那种丢失在任何报告里
    都不会自己出现。
    """
    items: list[list[str]] = []
    fields: list[str] = []
    source = ""
    offset = 0
    total: int | None = None
    while True:
        source, body = fetch(
            table, {"trade_date": day.strftime("%Y%m%d"), "limit": page_size, "offset": offset}
        )
        data = body.get("data") or {}
        page_fields = [str(f) for f in data.get("fields") or []]
        page_items = [[str(v) for v in row] for row in data.get("items") or []]
        if not fields:
            fields = page_fields
        elif page_fields and page_fields != fields:
            raise ValueError(f"offset={offset} 页的 fields 变了：{page_fields} != {fields}")
        raw_count = data.get("count")
        if isinstance(raw_count, int):
            total = raw_count
        items.extend(page_items)
        if len(page_items) < page_size:
            if data.get("has_more") and total is not None and len(items) < total:
                raise ValueError(
                    f"{source} {table}@{day} 在 offset={offset} 处截断：已取 {len(items)} 行、"
                    f"源声称共 {total} 行、下一页为空——本源单次查询有行数上限，"
                    "全市场拉取不可用，改用 --symbols 逐票拉取"
                )
            return source, items, fields, total
        offset += page_size


def anchor_check(
    rows: list[Any],
    *,
    prev_close_of: PrevCloseFn,
    limit_pct_of: LimitPctFn,
) -> tuple[list[str], int, list[Any]]:
    """锚点对账。返回 (超阈话术, 锚不上剔除数, 有锚且未超阈的行)。

    超阈记录的是**全部**行（报告里截断展示），只要非空调用方就整批拒；剔除在拒之前
    不发生——整批拒的时候剔除数没有意义，只有全过时"剔除多少"才值得说。
    """
    problems: list[str] = []
    unanchored = 0
    anchored: list[Any] = []
    for row in rows:
        prev = prev_close_of(row.symbol, row.trade_date)
        if prev is None or prev <= 0:
            unanchored += 1
            continue
        limit = limit_pct_of(row.symbol)
        for name, price in (("涨停", row.up_limit), ("跌停", row.down_limit)):
            pct = (price / prev - 1.0) * 100.0
            if abs(pct) > limit + ANCHOR_TOLERANCE_PP:
                problems.append(
                    f"{row.symbol}@{row.trade_date} {name}价 {price} 对昨收 {prev} 偏差 "
                    f"{pct:.2f}%，超出档位 {limit:.1f}%+{ANCHOR_TOLERANCE_PP:.0f}pp"
                )
        anchored.append(row)
    return problems, unanchored, anchored


def _prev_close_of(symbol: str, day: date) -> float | None:
    """干净区日线上的昨收（不复权）：day 往前找最近的那个有成交的收盘。锚不上给 None。"""
    from zhixing_quant.storage.query import read_bars

    bars = read_bars(symbol, day - timedelta(days=30), day, dataset="daily")
    earlier = [bar for bar in bars if bar.trade_date < day and bar.volume > 0]
    return earlier[-1].close if earlier else None


def _limit_pct_of(symbol: str) -> float:
    """R004 的档位表 + 主数据的板块/ST 档。查不出按主板 10% 处理——锚点容差 1pp 兜住
    四舍五入；错档（20% 板当 10% 锚）会真的报出来，那正是它该响的时候。"""
    from zhixing_quant.quality import gate_config
    from zhixing_quant.sources.akshare import master as master_source

    params = gate_config.load(config.gate_config_file()).rule("R004").params
    limits: Mapping[str, float] = params["limits_pct"]
    try:
        state = master_source.read_master().to_master().state_on(symbol, date.today())
        from zhixing_quant.domain.limits import limit_pct

        return limit_pct(state.board, is_st=state.is_st, limits_pct=limits)
    except KeyError:
        return limits["main"]


def _pull(
    args: argparse.Namespace,
    *,
    fetch: FetchFn,
    prev_close_of: PrevCloseFn,
    limit_pct_of: LimitPctFn,
    root: Path | None = None,
) -> tuple[str, int, TableWriteReport | None]:
    """拉一天 → 解析 → 过滤 → 锚点对账 → 落盘。返回 (报告, 退出码, 落盘账)。"""
    from zhixing_quant.sources.relay.tables import PARSERS

    source, items, fields, total = fetch_all_pages(
        args.table, args.day_parsed, fetch=fetch, page_size=args.page_size
    )
    rows = PARSERS[args.table](source, fields, items)
    scope = "全市场"
    if args.symbols:
        wanted = {c.strip() for c in args.symbols.split(",") if c.strip()}
        rows = [row for row in rows if row.symbol in wanted]
        scope = f"过滤 {len(wanted)} 票"
    head = [
        f"# 转接源参考表采集 · {args.table} · {args.day_parsed.isoformat()}",
        "",
        f"- 供数源：`{source}`（{scope}），原始 {len(items)} 行 → 解析 {len(rows)} 行",
    ]
    if total is not None and args.symbols is None and total > len(items):
        head.append(
            f"- **源声称共 {total} 行、只取到 {len(items)} 行**——按 --symbols 逐票拉取才取得全"
        )
    if not rows:
        empty = "\n".join([*head, "", "- 那天没有行（非交易日或源无数据），什么都不写。", ""])
        return empty, 0, None

    problems, unanchored, kept = anchor_check(
        rows, prev_close_of=prev_close_of, limit_pct_of=limit_pct_of
    )
    if problems:
        body = [
            *head,
            "",
            f"**锚点对账整批拒**：{len(problems)} 条超阈（容差 {ANCHOR_TOLERANCE_PP:.0f}pp），"
            "整页不可信，一行都不写（ADR-0015 决定 3）。",
            "",
        ]
        body += [f"- {p}" for p in problems[:10]]
        if len(problems) > 10:
            body.append(f"- …共 {len(problems)} 条，全量在上面的对账明细里")
        return "\n".join(body), 1, None

    ledger = tables.write_table(kept, table=args.table, root=root)
    tail = [
        "",
        f"- 锚点对账：{len(kept)} 行有锚全过；{unanchored} 行锚不上被剔除"
        "（主数据/日线缺——不是对不上，是没得对，别读成全数入库）",
        f"- 落盘：{ledger.partitions} 个分区，新增 {ledger.added}、修复 {ledger.repaired}、"
        f"重写 {ledger.rewritten} 个文件",
    ]
    return "\n".join([*head, *tail]), 0, ledger


def main(argv: list[str] | None = None) -> int:
    from zhixing_quant.sources.relay.client import fetch as relay_fetch

    args = build_parser().parse_args(argv)
    try:
        args.day_parsed = date.fromisoformat(args.day)
        check_range(date(2000, 1, 1), args.day_parsed)
        report, code, _ = _pull(
            args,
            fetch=relay_fetch,
            prev_close_of=_prev_close_of,
            limit_pct_of=_limit_pct_of,
        )
        out = args.out or config.reports_dir() / "relay" / date.today().isoformat()
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{args.table}-{args.day}.md").write_text(report + "\n", encoding="utf-8")
        print(report)
        print(f"报告落 {out / f'{args.table}-{args.day}.md'}")
        return code
    except (ValueError, OSError) as exc:
        print(f"转接采集没开始：{exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # RelayUnavailable 等"网络/源全灭"：没开始，不是数据坏
        print(f"转接采集没开始：{type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
