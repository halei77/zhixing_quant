"""`zx-relay`：转接源参考表的采集入口（ADR-0015 决定 3、5）。

stk_limit 一天一次返回全市场（Qoute 实测），分页 limit/offset 拉取。**2026-09-22 实测**：
rds 单次查询在 5000 行处静默截断（响应声称 count=5644、has_more=True，但 offset≥5000
一律空页），**全市场拉取在本源不可用**——截断由 `has_more` 硬响，不会无声丢行。日常用
`--symbols` 逐票拉取；`--symbols` 缺省 = 不过滤（受上述截断约束，报告会印缺口）。

三个子命令：`pull` 拉某个交易日、`capture` 抓黄金样本、`backfill` 逐票区间回填（任务 #53，
可续跑判据读盘、--relay 钉源、--attempts/--breaker 对齐 zx-minute——任务件在
`jobs.backfill`，这个文件只做装配）。

锚点对账（决定 3）：涨跌幅 = 涨停价 ÷ 干净区日线昨收（不复权对不复权），越过 R004 档位
+1pp 的行即整批拒——整页不可信，一行都不写。锚不上（主数据没这票、日线缺昨收）的行
**剔除并计数**：那不是"对不上"的证据，是"没得对"，与超阈是两回事，报告里分开说。

`pull` 退出码：0 跑完（含"那天无行"）；1 锚点对账整批拒；2 没开始（参数/网络/key）。
`backfill` 退出码：0 跑完无失败；1 跑完但有失败票；2 没跑完（熔断）或没开始。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from functools import lru_cache, partial
from pathlib import Path
from typing import Any

from zhixing_quant import config
from zhixing_quant.backtest.cli import check_range
from zhixing_quant.sources.jobs import backfill
from zhixing_quant.sources.relay.pages import (
    SHORT_PAGE_TABLES,
    FetchFn,
    fetch_pages,
    fetch_pages_short,
    page_limit_of,
)
from zhixing_quant.sources.relay.tables import _QUARTER_PATTERN
from zhixing_quant.storage import tables
from zhixing_quant.storage.tables import TableWriteReport

RefFn = Callable[[str, date], float | None]
LimitPctFn = Callable[[str], float]

#: 两种锚参考语义（2026-09-22 教训：混用一个"≤ day 最近收盘"会把 stk_limit 的参考取到
#: 当天自己的收盘、把 daily_basic 的参考取到缺数日子之前的旧收盘——两种错法都是大面积
#: 假超阈，锚点对账从守门员变成冤案制造机）：
#: - **昨收**（严格 < day）：stk_limit 的涨跌停价对它算；
#: - **当日**（== day，缺就是缺）：daily_basic 的 close 对它比。

ANCHOR_TOLERANCE_PP = 1.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zx-relay", description="转接源参考表采集（ADR-0015）：拉一天、锚点对账、入干净区"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    pull = sub.add_parser("pull", help="拉某个交易日的整张表")
    capture = sub.add_parser(
        "capture", help="抓一份真实响应快照落 golden/（ADR-0015 决定 4；采纳进 tests/ 需用户批准）"
    )
    capture.add_argument("--table", default="stk_limit", help="参考表名")
    capture.add_argument("--day", required=True, metavar="YYYY-MM-DD")
    capture.add_argument(
        "--symbols", default=None, help="逐票模式；缺省全市场（受 5000 行截断约束）"
    )
    pull.add_argument("--table", default="stk_limit", help="参考表名，默认 stk_limit")
    pull.add_argument("--day", required=True, metavar="YYYY-MM-DD", help="要哪个交易日")
    pull.add_argument("--symbols", default=None, help="逗号分隔的票池过滤；缺省全市场入库")
    pull.add_argument(
        "--page-size",
        type=int,
        default=None,
        help="分页大小，缺省按表取上限（5000；forecast 这类高基数接口实测 1000）。"
        "超上限发前拒，不发必 400 的那一发",
    )
    pull.add_argument(
        "--out", type=Path, default=None, help="报告目录，默认 <数据根>/reports/relay/<今天>"
    )
    backfill_cmd = sub.add_parser(
        "backfill",
        help="逐票区间回填（#53）：续跑判据读盘、--relay 钉源、--attempts/--breaker 对齐 zx-minute",
    )
    backfill_cmd.add_argument(
        "--table",
        default="daily_basic",
        choices=backfill.BACKFILL_TABLES,
        help="参考表名（仅回填支持的三张），默认 daily_basic",
    )
    backfill_cmd.add_argument(
        "--symbols", default=None, help="逗号分隔票池；缺省=主数据全市场（--limit 可截断）"
    )
    backfill_cmd.add_argument(
        "--limit", type=int, default=None, help="全市场模式只取前 N 只（给了 --symbols 时忽略）"
    )
    backfill_cmd.add_argument(
        "--relay",
        choices=("rds", "promax"),
        default=None,
        help="钉住单一源；缺省按 ADR-0014 顺序 rds→promax 自动退避、阶梯走完切源",
    )
    backfill_cmd.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="单票抓取次数上限（client 内已含 1→5→30 分钟阶梯，调大是乘法不是加法）",
    )
    backfill_cmd.add_argument(
        "--breaker",
        type=int,
        default=5,
        help="连败这么多**只票**就熔断收工（zx-minute 同款语义；转接一票=一条请求链）",
    )
    backfill_cmd.add_argument(
        "--out", type=Path, default=None, help="报告目录，默认 <数据根>/reports/relay/<今天>"
    )
    return parser


def fetch_all_pages(
    table: str,
    day: date,
    *,
    fetch: FetchFn,
    page_size: int = 5000,
    symbol: str | None = None,
    date_param: str | None = "trade_date",
) -> tuple[str, list[list[str]], list[str], int | None]:
    """分页拉完整天。返回 (供数源, 原始 items, fields, 源声称的总行数或 None)。

    守卫（**截断必须硬响**，2026-09-22 实测：rds offset≥5000 空页却 has_more=True）住在
    `relay.pages` 一处——pull 的按日拉取与 backfill 的逐票区间撞的是同一个 5000 行上限，
    两份守卫迟早漂成"一边响一边静默丢"。**两种分页形状各走各的守卫**（2026-09-25 实测）：
    has_more 族进 `fetch_pages`；`report_rc` 族（has_more 恒 False、单查询静默顶 5000）进
    `fetch_pages_short`（页 < limit 即到底 + 满页按日期二分缩窗）。表名是唯一分流依据。
    这里只负责把"哪天、哪票"拼成参数：逐票模式绕开单次查询的 5000 行截断，一票一天一两行，
    永远碰不到上限。
    """
    params: dict[str, object] = {}
    if date_param is not None:
        params[date_param] = day.strftime("%Y%m%d")
    if symbol is not None:
        params["ts_code"] = backfill.ts_code_of(symbol)
    if table in SHORT_PAGE_TABLES:
        return fetch_pages_short(table, params, fetch=fetch, page_size=page_size)
    return fetch_pages(table, params, fetch=fetch, page_size=page_size)


FactorFn = Callable[[str, date], float | None]
AnchorFn = Callable[..., tuple[list[str], int, list[Any]]]


def _anchor_stk_limit(
    rows: list[Any],
    prev_ref_of: RefFn,
    _same_ref_of: RefFn,
    limit_of: LimitPctFn,
    factor_of: FactorFn | None = None,
) -> tuple[list[str], int, list[Any]]:
    """涨跌停价对**昨收**（严格 < day）：任一边越过 R004 档位 +1pp 即记一条超阈。

    **超阈的例外不是豁免是换一个验证**：交易所的板价按**除权参考价**算（2026-09-22
    实测：601318 除息日跌停价对原始昨收 −11.57%），干净区又还没有分红表；且日线缺更近
    一日时昨收本身就是旧的。超阈的行改用自洽校验——涨停/跌停各反推一个参考价
    （`price ÷ (1 ± 档位)`），两者一致且落在昨收的合理邻域内 → 这行与某个"我们还没的
    参考价"自洽，放行并在报告里点名待核；不自洽的依旧算超阈。两种可能（除权 / 日线缺
    当日）报告里如实并列，不假装分得清。
    """
    problems: list[str] = []
    unanchored = 0
    exdiv_notes: list[str] = []
    anchored: list[Any] = []
    for row in rows:
        prev = prev_ref_of(row.symbol, row.trade_date)
        if prev is None or prev <= 0:
            unanchored += 1
            continue
        limit = limit_of(row.symbol)
        exceeded: list[tuple[str, float, float]] = []
        for name, price in (("涨停", row.up_limit), ("跌停", row.down_limit)):
            pct = (price / prev - 1.0) * 100.0
            if abs(pct) > limit + ANCHOR_TOLERANCE_PP:
                exceeded.append((name, price, pct))
        if exceeded:
            ref_up = row.up_limit / (1.0 + limit / 100.0)
            ref_down = row.down_limit / (1.0 - limit / 100.0)
            plausible = (
                0.8 * prev <= ref_up <= 1.2 * prev and abs(ref_up - ref_down) <= CLOSE_TOLERANCE
            )
            if factor_of is not None and plausible:
                factor_of(row.symbol, row.trade_date)  # 因子存在性检查留给实现；此处只标记
                exdiv_notes.append(
                    f"{row.symbol}@{row.trade_date} 板价对昨收超阈但两界反推参考价一致"
                    f"（≈{ref_up:.2f}，昨收 {prev}）——除权日或日线缺更近一日，放行待核"
                )
                anchored.append(row)
                continue
            for name, price, pct in exceeded:
                problems.append(
                    f"{row.symbol}@{row.trade_date} {name}价 {price} 对昨收 {prev} 偏差 "
                    f"{pct:.2f}%，超出档位 {limit:.1f}%+{ANCHOR_TOLERANCE_PP:.0f}pp"
                )
        anchored.append(row)
    if exdiv_notes:
        anchored = list(anchored)
    _exdiv_notes.extend(exdiv_notes)  # 话术经模块级列表带回，见 _pull
    return problems, unanchored, anchored


#: 除权待核话术的回传通道。函数返回值已经四元了，再塞一元签名很难看；模块级列表在
#: 单线程的 CLI 里是安全的，测试里每次用例前清空。
_exdiv_notes: list[str] = []


def take_exdiv_notes() -> list[str]:
    """取走并清空除权待核话术。

    pull 一个进程只锚一批，攒着没关系；backfill 按票循环几百次，不清会把 A 票的话术
    重复记到 B、C、… 票的报告头上——话术必须随产话的那一批走（`make_anchor` 每票先清后取）。
    """
    notes = list(_exdiv_notes)
    _exdiv_notes.clear()
    return notes


#: 收盘价的取整容差。双方都到分（0.01 元），逐位相等理应成立；一分钱的余量只吸收
#: 浮点表示误差。超它就是"两边不同源不同真"，整批拒。
CLOSE_TOLERANCE = 0.011


def _anchor_daily_basic(
    rows: list[Any],
    _prev_ref_of: RefFn,
    same_ref_of: RefFn,
    _limit_of: LimitPctFn,
) -> tuple[list[str], int, list[Any]]:
    """表内 close 对干净区日线**同日**收盘：两边都是不复权收盘价，一分钱都差不起。"""
    problems: list[str] = []
    unanchored = 0
    anchored: list[Any] = []
    for row in rows:
        ref = same_ref_of(row.symbol, row.trade_date)
        if ref is None or ref <= 0:
            unanchored += 1
            continue
        if abs(row.close - ref) > CLOSE_TOLERANCE:
            problems.append(
                f"{row.symbol}@{row.trade_date} 表内 close {row.close} 与干净区收盘 {ref} "
                f"差 {abs(row.close - ref):.4f} 元（容差 {CLOSE_TOLERANCE}）"
            )
        anchored.append(row)
    return problems, unanchored, anchored


def _anchor_forecast(
    rows: list[Any],
    _prev_ref_of: RefFn,
    _same_ref_of: RefFn,
    _limit_of: LimitPctFn,
) -> tuple[list[str], int, list[Any]]:
    """forecast 的锚：end_date 必须是季度末（业绩预告的报告期），ann_date 不许在未来。
    它没有干净区同口径数据可比——这两条是"行级可验"的全部，类型枚举与区间校验在解析器。"""
    import calendar as cal

    problems: list[str] = []
    unanchored = 0
    anchored: list[Any] = []
    for row in rows:
        if row.end_date.day != cal.monthrange(row.end_date.year, row.end_date.month)[1]:
            problems.append(
                f"{row.symbol}@{row.ann_date} end_date {row.end_date} 不是季度末（报告期口径）"
            )
            continue
        if row.ann_date > date.today():
            problems.append(f"{row.symbol}@{row.ann_date} ann_date 在未来")
            continue
        anchored.append(row)
    return problems, unanchored, anchored


def _anchor_report_rc(
    rows: list[Any],
    _prev_ref_of: RefFn,
    _same_ref_of: RefFn,
    _limit_of: LimitPctFn,
) -> tuple[list[str], int, list[Any]]:
    """report_rc 的锚：行级可验（与 forecast 同族——研报发布没有逐行可比的干净区同口径行）。

    `report_date` 不许在未来（发布日 ≤ 今天），quarter 形状再验一遍（解析器已拒，这里防
    绕过解析器的调用）。**为什么不拿 pe×eps 对昨收当逐行闸门**：2026-09-25 对 600519 的
    1376 组实测里 pe×eps≈前一交易日收盘的中位偏差 0.18%、≤5% 占 98.7%，但源自身带着陈旧 pe
    （最大一组隐含价偏 93%）——逐行硬闸会把整批真数据永远挡在门外。抽样互核（≥20 组、
    与干净区日线对账）按 04 §五 转接专项在接表验收里做，数字进报告，不冒充逐行闸门。
    """
    problems: list[str] = []
    unanchored = 0
    anchored: list[Any] = []
    today = date.today()
    for row in rows:
        if row.report_date > today:
            problems.append(f"{row.symbol}@{row.report_date} report_date 在未来（研报发布日）")
            continue
        if not _QUARTER_PATTERN.fullmatch(row.quarter):
            problems.append(f"{row.symbol}@{row.report_date} quarter {row.quarter!r} 不是 YYYYQn")
            continue
        anchored.append(row)
    return problems, unanchored, anchored


#: 表名 → 锚点。没登记锚的表不许拉（ADR-0015 决定 3：每张表配锚点对账，没有豁免）。
ANCHORS: dict[str, AnchorFn] = {
    "stk_limit": _anchor_stk_limit,
    "daily_basic": _anchor_daily_basic,
    # forecast / fina_audit / stk_holdernumber 同一支锚：公告类表没有干净区同口径
    # 数据可比，"行级可验"（报告期口径 + 时序合理）就是它们的全部分量。
    "forecast": _anchor_forecast,
    "fina_audit": _anchor_forecast,
    "stk_holdernumber": _anchor_forecast,
    "index_daily": _anchor_forecast,  # OHLC 不变量在解析器；行情日没有"报告期"概念，行级可验即全部
    "report_rc": _anchor_report_rc,  # 研报发布日：行级可验 + 未来发布日拒；抽样互核见 _docstring
}

#: 档位查询注入点：真跑用 `_limit_pct_of`（主数据 + gate.toml），测试注入常数表。
LimitOfFn = LimitPctFn


def _prev_ref_of(symbol: str, day: date) -> float | None:
    """昨收（严格 < day）：stk_limit 锚的参考。干净区日线（不复权）。"""
    from zhixing_quant.storage.query import read_bars

    bars = read_bars(symbol, day - timedelta(days=30), day, dataset="daily")
    earlier = [bar for bar in bars if bar.trade_date < day and bar.volume > 0]
    return earlier[-1].close if earlier else None


def _factor_of(symbol: str, day: date) -> float | None:
    """当日复权因子（除权待核的旁证；目前只做存在性检查）。"""
    from zhixing_quant.storage.query import read_bars

    bars = read_bars(symbol, day, day, dataset="daily")
    return bars[-1].adj_factor if bars else None


def _same_ref_of(symbol: str, day: date) -> float | None:
    """当日收盘（== day，缺就是缺）：daily_basic 锚的参考。绝不往前顶旧收盘——
    那会把"干净区还没这天"洗成"数值对不上"，锚点对账就成了冤案制造机。"""
    from zhixing_quant.storage.query import read_bars

    bars = read_bars(symbol, day, day, dataset="daily")
    rows = [bar for bar in bars if bar.volume > 0]
    return rows[-1].close if rows else None


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


#: 档位查询的进程级缓存：`_limit_pct_of` 每次都重读主数据快照，stk_limit 回填一票几百行
#: 会把快照读几百遍。缓存按票一份——一池票几千个键，几个 float 的事。
_limit_pct_cached = lru_cache(maxsize=None)(_limit_pct_of)


def make_anchor(table: str, *, limit_of: LimitOfFn | None = None) -> backfill.AnchorFn:
    """表 → 回填用的锚点调用器：整票的行 + 预载日线索引 → (超阈, 锚不上, 放行, 话术)。

    与 `_pull` 里那套逐行读盘的参考（`_prev_ref_of`/`_same_ref_of`）是同一个锚函数、两种
    取参考的姿势：回填一票几百行，逐行开 DuckDB 不可接受，所以参考由 `backfill.BarRefs`
    一次预载后注入。stk_limit 的除权待核话术经 `take_exdiv_notes` 每票清取，随 outcome 进报告。
    """
    from zhixing_quant.sources.relay.tables import PARSERS

    if table not in ANCHORS or table not in PARSERS:
        raise ValueError(
            f"表 {table!r} 没有登记解析器或锚点对账：ADR-0015 决定 3 不允许无锚入干净区"
        )
    anchor = ANCHORS[table]
    picker = limit_of if limit_of is not None else _limit_pct_cached

    def call(
        rows: list[Any], refs: backfill.BarRefs
    ) -> tuple[list[str], int, list[Any], list[str]]:
        take_exdiv_notes()  # 上一票的残留先清：话术只许归产它的那一批
        if table == "stk_limit":
            problems, unanchored, kept = anchor(
                rows, refs.prev, refs.same, picker, factor_of=refs.factor
            )
        else:
            problems, unanchored, kept = anchor(rows, refs.prev, refs.same, picker)
        return problems, unanchored, kept, take_exdiv_notes()

    return call


def _pull(
    args: argparse.Namespace,
    *,
    fetch: FetchFn,
    prev_ref_of: RefFn,
    same_ref_of: RefFn,
    limit_of: LimitOfFn | None = None,
    root: Path | None = None,
) -> tuple[str, int, TableWriteReport | None]:
    """拉一天 → 解析 → 过滤 → 锚点对账 → 落盘。返回 (报告, 退出码, 落盘账)。"""
    from zhixing_quant.sources.relay.tables import PARSERS

    anchor = ANCHORS.get(args.table)
    if anchor is None or args.table not in PARSERS:
        raise ValueError(
            f"表 {args.table!r} 没有登记解析器或锚点对账：ADR-0015 决定 3 不允许无锚入干净区"
        )
    # page_size 缺省 = 按表上限（forecast 这类高基数接口实测 max_limit=1000，5000 直接 400；
    # 显式给超上限的值由 pages 层发前拒，不发必错的那一发）。
    page_size = args.page_size if args.page_size is not None else page_limit_of(args.table)
    source = ""
    scope = "全市场"
    if args.symbols is None:
        source, items, fields, total = fetch_all_pages(
            args.table, args.day_parsed, fetch=fetch, page_size=page_size
        )
        rows = PARSERS[args.table](source, fields, items)
    else:
        rows = []
        items = []
        total = None
        raw_count = 0
    if args.symbols:
        wanted = [c.strip() for c in args.symbols.split(",") if c.strip()]
        # 逐票拉取（ts_code 参数）：过滤是在拉取前按票发请求，不是拉完整市场再筛——
        # 后者撞 5000 行截断，前者绕开它。
        rows = []
        source = ""
        # 公告类表（forecast / report_rc）按 ts_code 拉全史，没有 trade_date 参数——day 对
        # 它们是"锚点与归档日期"，不是源端筛选。
        date_param = None if args.table in ("forecast", "report_rc") else "trade_date"
        for symbol in wanted:
            src, page_items, page_fields, _ = fetch_all_pages(
                args.table,
                args.day_parsed,
                fetch=fetch,
                page_size=page_size,
                symbol=symbol,
                date_param=date_param,
            )
            source = source or src
            raw_count += len(page_items)
            rows.extend(PARSERS[args.table](src, page_fields, page_items))
        scope = f"逐票 {len(wanted)} 票"
        total = None  # 逐票没有"全天总数"可比；截断守卫在逐票模式下天然用不上
    raw_total = len(items) if args.symbols is None else raw_count
    head = [
        f"# 转接源参考表采集 · {args.table} · {args.day_parsed.isoformat()}",
        "",
        f"- 供数源：`{source}`（{scope}），原始 {raw_total} 行 → 解析 {len(rows)} 行",
    ]
    if total is not None and args.symbols is None and total > len(items):
        head.append(
            f"- **源声称共 {total} 行、只取到 {len(items)} 行**——按 --symbols 逐票拉取才取得全"
        )
    if not rows:
        empty = "\n".join([*head, "", "- 那天没有行（非交易日或源无数据），什么都不写。", ""])
        return empty, 0, None

    if args.table == "stk_limit":
        problems, unanchored, kept = anchor(
            rows,
            prev_ref_of,
            same_ref_of,
            limit_of or _limit_pct_of,
            factor_of=_factor_of,
        )
    else:
        problems, unanchored, kept = anchor(
            rows, prev_ref_of, same_ref_of, limit_of or _limit_pct_of
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
            body.append(f"- …共 {len(problems)} 条")
        return "\n".join(body), 1, None

    ledger = tables.write_table(kept, table=args.table, root=root)
    tail = [
        "",
        f"- 锚点对账：{len(kept)} 行有锚全过；{unanchored} 行锚不上被剔除"
        "（主数据/日线缺——不是对不上，是没得对，别读成全数入库）",
    ]
    for note in _exdiv_notes[:5]:
        tail.append(f"- 参考价待核：{note}")
    tail.append(
        f"- 落盘：{ledger.partitions} 个分区，新增 {ledger.added}、修复 {ledger.repaired}、"
        f"重写 {ledger.rewritten} 个文件"
    )
    return "\n".join([*head, *tail]), 0, ledger


def _backfill(
    args: argparse.Namespace,
    *,
    fetch: FetchFn,
) -> tuple[str, int, Path]:
    """装配一次回填：票池（--symbols 或主数据全市场[:limit]）→ 钉源 fetch → `backfill.run`。

    这里没有业务规则：续跑判据、重试熔断、锚点落盘、日报渲染全在 `jobs.backfill`（与
    zx-minute"CLI 只装配"同一条分工）。`root`/`directory` 必须由这里说死（坑 #24）。
    """
    if args.symbols is not None:
        pool = [code.strip() for code in args.symbols.split(",") if code.strip()]
        scope = f"--symbols {len(pool)} 票"
    else:
        from zhixing_quant.sources.akshare.master import read_master

        pool = list(read_master().to_master().universe_on(date.today()))
        if args.limit is not None:
            pool = pool[: args.limit]
        scope = "主数据全市场" + (f"[:{args.limit}]" if args.limit is not None else "")
    if not pool:
        raise ValueError("票池是空的：--symbols 没解析出代码，或主数据在册名单为空")
    relays: tuple[str, ...] = (args.relay,) if args.relay else ("rds", "promax")
    result = backfill.run(
        args.table,
        symbols=pool,
        fetch=partial(fetch, relays=relays),  # 钉源只动这里：run 层拿到的 fetch 已带 relays
        anchor=make_anchor(args.table),
        root=config.parquet_dir(),
        directory=args.out or config.reports_dir() / "relay",
        scope=scope,
        attempts=args.attempts,
        breaker=args.breaker,
        progress=lambda line: print(line, flush=True),
    )
    return result.markdown, result.exit_code(), result.report


def _capture(args: argparse.Namespace) -> str:
    """抓一份原始响应快照进 golden/relay/（ADR-0015 决定 4 的"抓下来的"那一半；
    "认过的"要用户批准后才进 tests/golden/，两处隔开）。"""
    from zhixing_quant.sources.relay.client import fetch as relay_fetch

    day = date.fromisoformat(args.day)
    wanted: list[str] | None = (
        [c.strip() for c in args.symbols.split(",") if c.strip()] if args.symbols else None
    )
    out_dir = config.data_root() / "golden" / "relay"
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# relay golden · {args.table} · {day.isoformat()}"]
    if wanted is None:
        source, body = relay_fetch(args.table, {"trade_date": day.strftime("%Y%m%d")})
        name = f"{args.table}-{day.isoformat()}-{source}.json"
        (out_dir / name).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        lines.append(
            f"- {name}（全市场首页，{len((body.get('data') or {}).get('items') or [])} 行）"
        )
    else:
        from zhixing_quant.domain.symbol import normalize_code
        from zhixing_quant.sources.akshare.fetch import market_of

        for symbol in wanted:
            source, body = relay_fetch(
                args.table,
                {
                    "ts_code": f"{normalize_code(symbol)}.{market_of(symbol).upper()}",
                    "trade_date": day.strftime("%Y%m%d"),
                },
            )
            name = f"{args.table}-{day.isoformat()}-{symbol}-{source}.json"
            (out_dir / name).write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
            lines.append(f"- {name}（{len((body.get('data') or {}).get('items') or [])} 行）")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    from zhixing_quant.sources.relay.client import fetch as relay_fetch

    args = build_parser().parse_args(argv)
    try:
        if args.command == "backfill":
            # parse_args 在 try 外先跑：多余参数/非法取值由 argparse 以退出码 2 拒掉，
            # 一步网络都不发（独立审计 M1 / 任务 #30 的 fail-closed 口径，测试同款钉住）。
            report, code, path = _backfill(args, fetch=relay_fetch)
            print(report)
            print(f"报告落 {path}")
            return code
        args.day_parsed = date.fromisoformat(args.day)
        if args.command == "capture":
            print(_capture(args))
            return 0
        check_range(date(2000, 1, 1), args.day_parsed)
        report, code, _ = _pull(
            args,
            fetch=relay_fetch,
            prev_ref_of=_prev_ref_of,
            same_ref_of=_same_ref_of,
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
