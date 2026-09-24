"""`zx-relay backfill` 的任务件：可续跑、可钉源、可重试的参考表回填（任务 #53）。

## 为什么长这样（2026-09-24 实测）

之前的回填是"按交易日全市场"逐日循环，进度记在 /tmp 运行件里——WSL2 一重启就没了
（661 个交易日只做了 231 个，全是 2024 年），且整市场单日拉取在 rds 的 5000 行静默截断下
必然硬响（ADR-0015）。盘上因此满是洞：daily_basic 4472/5565 只有分区，600519 只有 648 行
（2024-01-02..2026-09-18，对干净区日线在册的 663 个有量交易日缺 15 天）、300308 只有
154 行停在 2024-09-13；forecast 只有 5 只票；stk_limit 只有 5 只票。

本任务把方向倒过来：**逐票区间拉取**。一只票一个窗口最多几百行（远够不着 5000 上限），
rds 实测（2026-09-24）认 `ts_code+start_date+end_date`——600519.SH 20240102..20240110
精确回 7 行；同日 promax 503 `upstream_pool_exhausted`，所以要 `--relay` 能钉源、坏了能
换源重跑。截断真撞上了由 `relay.pages.fetch_pages` 的 has_more 守卫硬响（ADR-0015）。

## 三条硬性质

1. **续跑判据只读盘，不靠运行件**（#53 的直接起因）。按日表（daily_basic/stk_limit）每票
   先读盘算 `应有集 − 已有集`：应有集 = 干净区日线该票**有量**交易日（volume>0，与锚点
   口径同源），已有集 = 参考表分区里已落的日期。差集为空 → 整票跳过，一个请求都不发；
   不为空 → 只有缺口要补。公告类表（forecast）没有"应有日全集"可推——一只票哪几个季度
   发预告是它自己的事——只能逐票拉全史、靠主键经 `write_table` 幂等跳过已存在的行，
   报告里两种跳过分开计数，不混成一个数。
2. **落盘幂等按 `storage/tables.py` 既有契约**：同键同值不写（重跑 rewritten=0、文件
   mtime 不动）、同键异值修复、批内冲突抛。中断续跑的对偶保证（tests/sources/jobs/
   test_backfill.py）：跑一半被杀 → 重跑 → 与一次跑完**逐行相同**。
3. **快速失败**：`--attempts`/`--breaker` 形态对齐 `zx-minute`——单票抓取重试 N 次、
   连败 N 票熔断收工（连败按**票**数：转接一票就是一条请求链，client 内部已按 ADR-0014
   走 1→5→30 分钟阶梯，任务层再数请求没有意义）。`RelayUnavailable` 可重试；
   `ValueError`（截断、fields 漂移、解析拒收）是确定性失败，重试只会同样结果——当场记账
   不烧 attempts。

退出码：0 跑完且无失败票；1 跑完但有失败票（锚拒/解析/网络重试尽）；2 没跑完（熔断收工）
或没开始（参数/票池/网络，经 CLI 的异常路径）。
"""

from __future__ import annotations

import time
from bisect import bisect_left
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import duckdb

from zhixing_quant.domain.symbol import normalize_code
from zhixing_quant.sources.akshare.fetch import market_of
from zhixing_quant.sources.jobs.daily import REASON_BREAKER
from zhixing_quant.sources.relay.client import RelayUnavailable
from zhixing_quant.sources.relay.pages import (
    SHORT_PAGE_TABLES,
    FetchFn,
    fetch_pages,
    fetch_pages_short,
    page_limit_of,
)
from zhixing_quant.sources.relay.tables import PARSERS
from zhixing_quant.storage import layout, tables
from zhixing_quant.storage.query import read_bars

#: 回填认这几张表（#53 圈定三张 + #59 C 刀的 report_rc + 本刀的 fina_indicator）。
#: 别的参考表要先配锚、实测过窗口参数才许进来——表名打错或没实测就开跑，在这里是退出码 2
#: 的 ValueError，不是"先跑了再说"。
BACKFILL_TABLES = ("daily_basic", "forecast", "stk_limit", "report_rc", "fina_indicator")

#: 按日序列表：续跑判据 = 日线有量交易日 − 盘上已有。公告类（forecast）没有应有集，
#: 走"拉全史 + 主键幂等跳过"，见模块说明第 2 条。
RANGE_TABLES = frozenset({"daily_basic", "stk_limit"})

#: 回填窗口起点。实测口径（2026-09-24 盘上数）：干净区 daily 的有量交易日从 2023-11-06 起
#: 共 703 个（2023 年 40 个、2024-01-02 起 663 个），daily_basic 盘上最早就是 2024-01-02——
#: 对齐任务 #53 的"2024-01-02 .. 至今"。2023 年那 40 天不在本窗口（要改窗口是口径变更）。
RANGE_START = date(2024, 1, 2)

#: 日线预载往回多带的自然日：2024-01-02 的 stk_limit 锚要 2023-12-29 昨收，跨年 + 双节
#: 最长假期内 21 天足够罩住（与 `read_bars` 的区间筛法同理，宁多勿少）。
REF_PAD_DAYS = 21

#: 锚点调用器：整票的行 + 预载日线索引 → (超阈问题, 锚不上行数, 放行行数, 除权待核话术)。
#: 比 `relay_cli.ANCHORS` 的三元组多一元：回填按票循环，话术要随票进报告（见 make_anchor）。
AnchorFn = Callable[[list[Any], "BarRefs"], tuple[list[str], int, list[Any], list[str]]]

Status = Literal["landed", "no_rows", "skipped", "failed", "breaker"]

#: 读盘跳过的两种话术——"覆盖齐了"与"锚不上所以不拉"是两件事，报告里分开数。
SKIP_COMPLETE = "覆盖已齐（读盘判据：日线有量交易日全部在册）"
SKIP_NO_REFERENCE = "日线无参考行（锚不上，拉了也会被剔）"


@dataclass(frozen=True)
class BarRefs:
    """一只票预载的日线索引：锚点的三个参考（当日收盘/昨收/复权因子）都从这里查。

    预载而不是逐行 `read_bars`：回填窗口一张票几百个行锚，原来 `_same_ref_of` 是每行一次
    DuckDB 查询（开连接 + glob + 读分区），一票就要几百次；整票读一次压成查表后，锚点是
    O(log n)/O(1)。`same`/`prev`/`factor` 的签名就是 `relay_cli` 锚函数要的 RefFn/FactorFn。

    昨收用排序日序列 + bisect 而不是线性扫：全市场 stk_limit 回填是几千票 × 几百行，
    O(n²) 会把 CPU 烧在找昨收上。`ordered_days` 含窗口回带（REF_PAD_DAYS）里那天——
    窗口第一天的昨收在窗口外，这正是回带存在的理由。
    """

    symbol: str
    #: 窗口内的有量交易日——按日表"应有集"就是它。
    traded: frozenset[date]
    closes: dict[date, float]
    ordered_days: tuple[date, ...]
    factors: dict[date, float | None]

    @classmethod
    def none(cls, symbol: str) -> BarRefs:
        """空索引：公告类表的锚不看日线，不为它做任何读盘。"""
        return cls(symbol, frozenset(), {}, (), {})

    @classmethod
    def load(cls, symbol: str, start: date, end: date, *, root: Path) -> BarRefs:
        bars = [
            bar
            for bar in read_bars(
                symbol, start - timedelta(days=REF_PAD_DAYS), end, dataset="daily", root=root
            )
            if bar.volume > 0
        ]
        closes = {bar.trade_date: bar.close for bar in bars}
        factors = {bar.trade_date: bar.adj_factor for bar in bars}
        traded = frozenset(day for day in closes if start <= day <= end)
        return cls(symbol, traded, closes, tuple(sorted(closes)), factors)

    def same(self, symbol: str, day: date) -> float | None:
        """当日收盘（== day，缺就是缺）：daily_basic 锚的参考。绝不往前顶旧收盘。"""
        return self.closes.get(day) if symbol == self.symbol else None

    def prev(self, symbol: str, day: date) -> float | None:
        """昨收（严格早于 day 的最近有量日收盘）：stk_limit 锚的参考。"""
        if symbol != self.symbol:
            return None
        index = bisect_left(self.ordered_days, day)
        if index == 0:
            return None
        return self.closes[self.ordered_days[index - 1]]

    def factor(self, symbol: str, day: date) -> float | None:
        """当日复权因子（除权待核的存在性检查）。"""
        return self.factors.get(day) if symbol == self.symbol else None


@dataclass(frozen=True)
class SymbolOutcome:
    """一只票在这次回填里的账。报告的每一行都从这些数加出来，没有第二本账。"""

    symbol: str
    status: Status
    reason: str = ""
    source: str = ""
    #: 窗口过滤后解析出的行数。
    fetched: int = 0
    #: 锚点放行、交给落盘的行数。
    kept: int = 0
    added: int = 0
    repaired: int = 0
    rewritten: int = 0
    unanchored: int = 0
    exdiv_notes: tuple[str, ...] = ()

    @property
    def existing_skipped(self) -> int:
        """拉回来了、但盘上已有同键同值所以一行没写的行（write_table 的幂等账面）。"""
        return max(0, self.kept - self.added - self.repaired)


@dataclass(frozen=True)
class BackfillResult:
    """一次回填的总账 + 日报正文 + 报告路径。退出码也在这里判（见 exit_code）。"""

    table: str
    start: date
    end: date
    ranged: bool
    pool: int
    scope: str
    attempts: int
    breaker: int
    page_size: int
    outcomes: tuple[SymbolOutcome, ...]
    sources: tuple[tuple[str, int], ...]
    coverage_symbols: int
    coverage_days: int
    coverage_from: date | None
    coverage_to: date | None
    markdown: str
    report: Path
    breaker_tripped: bool = False

    @property
    def added(self) -> int:
        return sum(outcome.added for outcome in self.outcomes)

    @property
    def repaired(self) -> int:
        return sum(outcome.repaired for outcome in self.outcomes)

    @property
    def rewritten(self) -> int:
        return sum(outcome.rewritten for outcome in self.outcomes)

    @property
    def existing_skipped(self) -> int:
        return sum(outcome.existing_skipped for outcome in self.outcomes)

    @property
    def unanchored(self) -> int:
        return sum(outcome.unanchored for outcome in self.outcomes)

    @property
    def failed(self) -> tuple[SymbolOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.status == "failed")

    @property
    def landed(self) -> tuple[SymbolOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.status == "landed")

    @property
    def exdiv_notes(self) -> tuple[str, ...]:
        return tuple(note for outcome in self.outcomes for note in outcome.exdiv_notes)

    def exit_code(self) -> int:
        """0 跑完无失败；1 跑完有失败票；2 熔断收工（没跑完）。"""
        if self.breaker_tripped:
            return 2
        return 1 if self.failed else 0


def ts_code_of(symbol: str) -> str:
    """6 位代码 → Tushare 谱系的 `ts_code`（600519.SH）。与 pull 逐票模式同一条式子。"""
    return f"{normalize_code(symbol)}.{market_of(symbol).upper()}"


def fetch_range(
    table: str,
    symbol: str,
    start: date | None,
    end: date | None,
    *,
    fetch: FetchFn,
    page_size: int = 5000,
) -> tuple[str, list[list[str]], list[str], int | None]:
    """一只票一个窗口的全部行（`ts_code` + `start_date/end_date`），分页带回。

    参数实测（2026-09-24，rds）：daily_basic 认 start_date/end_date——600519.SH
    20240102..20240110 精确回 7 行、has_more=False。逐票 + 窗口把单查询压到几百行，rds 的
    5000 行静默截断碰不到；真撞上了 `pages.fetch_pages` 会硬响（ADR-0015 决定 3）。

    `start`/`end` 给 None = 公告类全史拉取（forecast/report_rc/fina_indicator 没有按日窗口，
    主键去重交给 `write_table` 的幂等）。promax 即便不认这两个参数，本地还有窗口过滤兜底
    （见 run）。

    **两种分页形状各走各的守卫**（2026-09-25 实测）：has_more 族（daily_basic/forecast/
    stk_limit）进 `fetch_pages`；短页族（report_rc/fina_indicator：has_more 恒 False、
    单查询静默顶——5000 / 100 行、offset 语义不可用）进 `fetch_pages_short`——页 < 顶即
    到底，满页按日期二分缩窗（顶与全史左界按表登记）。
    """
    params: dict[str, object] = {"ts_code": ts_code_of(symbol)}
    if start is not None and end is not None:
        params["start_date"] = start.strftime("%Y%m%d")
        params["end_date"] = end.strftime("%Y%m%d")
    if table in SHORT_PAGE_TABLES:
        return fetch_pages_short(table, params, fetch=fetch, page_size=page_size)
    return fetch_pages(table, params, fetch=fetch, page_size=page_size)


def existing_dates(table: str, symbol: str, start: date, end: date, *, root: Path) -> set[date]:
    """盘上这只票在这张表、这个窗口里已有哪些日期（续跑判据的"已有集"）。"""
    spec = tables.table_spec(table)
    rows = tables.read_table(table, symbol, start, end, root=root)
    return {getattr(row, spec.date_field) for row in rows}


def coverage(table: str, *, root: Path) -> tuple[int, int, date | None, date | None]:
    """盘上这张表最终覆盖：几只票 × 几个日期（+起讫）。读的是干净区 Parquet 本体。

    报告要回答的是"盘上现在有多少"，不是"这次跑了有多少"——后者 outcomes 里有，前者只能
    读盘。`hive_partitioning=false`（坑 #13）：symbol 本来就是行里的列，再从路径推一遍会
    撞出两列同名。
    """
    spec = tables.table_spec(table)
    paths = [str(path) for path in layout.dataset_partitions(spec.name, root=root)]
    if not paths:
        return 0, 0, None, None
    column = spec.date_field
    with duckdb.connect() as con:
        row = con.execute(
            f"SELECT count(DISTINCT symbol), count(DISTINCT {column}), "
            f"min({column}), max({column}) FROM read_parquet(?, hive_partitioning=false)",
            [paths],
        ).fetchone()
    if row is None:  # 上面 paths 非空，SELECT 至少回一行——防御只为过 mypy 的 Any 收窄
        return 0, 0, None, None
    return int(row[0]), int(row[1]), row[2], row[3]


def run(
    table: str,
    *,
    symbols: Sequence[str],
    fetch: FetchFn,
    anchor: AnchorFn,
    root: Path,
    directory: Path,
    scope: str = "逐票",
    start: date = RANGE_START,
    end: date | None = None,
    now: datetime | None = None,
    attempts: int = 3,
    breaker: int = 5,
    page_size: int | None = None,
    backoff: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = None,
) -> BackfillResult:
    """跑一池票：读盘判缺口 → 逐票抓取重试 → 解析 → 锚点 → 幂等落盘 → 日报归档。

    `root` 与 `directory` 都没有默认值：读盘根必须是干净区那一层（坑 #24：传成数据根会
    静默读空、判据全绿），报告目录默认了就会覆盖别人的文件——两者都由 CLI 用
    `config.parquet_dir()` / `config.reports_dir()` 说清楚。

    `page_size` 缺省 = **按表取上限**（`pages.page_limit_of`）：forecast 这类高基数接口实测
    `max_limit=1000`，全局 5000 会整池 HTTP 400（2026-09-25 实测 5568 连败）。显式传超上限
    的值不在这里兜底——`pages.fetch_pages` 发前拒（fail-closed，transport 零调用），错参数
    要响在调用方眼前，不许静默改成另一个尺寸继续跑。

    `end` 缺省 = 今天（本地钟，与 pull 的 `date.today()` 同一个"今天"，不走 UTC——坑 #32
    的 UTC/本地差一天在这里同样会咬人）；`now` 是给测试的注入点。
    """
    if table not in BACKFILL_TABLES:
        raise ValueError(
            f"表 {table!r} 不在回填支持清单 {list(BACKFILL_TABLES)} 里："
            "每张表要先配锚、实测窗口参数才许回填（ADR-0015 决定 3，没有批量豁免）"
        )
    if table not in PARSERS:  # 防 BACKFILL_TABLES 与 PARSERS 漂移；上面已限死，理论不达
        raise ValueError(f"表 {table!r} 没有登记解析器")
    if attempts < 1 or breaker < 1:
        raise ValueError(
            f"attempts/breaker 至少为 1（收到 attempts={attempts}、breaker={breaker}）："
            "0 次重试等于写死失败，不是快速失败"
        )
    if not symbols:
        raise ValueError("票池是空的：--symbols 没解析出代码，或主数据在册名单为空")
    resolved_page_size = page_size if page_size is not None else page_limit_of(table)
    moment = now if now is not None else datetime.now()
    resolved_end = end if end is not None else moment.date()
    if resolved_end < start:
        raise ValueError(f"区间颠倒了：{start} 晚于 {resolved_end}")

    ranged = table in RANGE_TABLES
    outcomes: list[SymbolOutcome] = []
    source_counts: Counter[str] = Counter()
    consecutive = 0
    breaker_tripped = False

    for position, symbol in enumerate(symbols):
        if consecutive >= breaker:
            outcomes.extend(
                SymbolOutcome(left, "breaker", reason=REASON_BREAKER) for left in symbols[position:]
            )
            breaker_tripped = True
            break
        outcome = _process_symbol(
            table,
            symbol,
            ranged=ranged,
            start=start,
            end=resolved_end,
            fetch=fetch,
            anchor=anchor,
            root=root,
            attempts=attempts,
            page_size=resolved_page_size,
            backoff=backoff,
            sleep=sleep,
        )
        outcomes.append(outcome)
        if outcome.source:
            source_counts[outcome.source] += 1
        # 熔断数的是"连续没拿到可用的东西"：网络/截断/解析/锚拒都算败；读盘跳过中立
        # （没联网不证明源活着，也不该攒败绩）；拿到行（哪怕 0 行）说明源在答话，清零。
        if outcome.status == "failed":
            consecutive += 1
        elif outcome.status in ("landed", "no_rows"):
            consecutive = 0
        if progress is not None:
            progress(_progress_line(position + 1, len(symbols), outcome))

    cov_symbols, cov_days, cov_from, cov_to = coverage(table, root=root)
    window = (
        f"{start.isoformat()} .. {resolved_end.isoformat()}"
        if ranged
        else "全史（公告类表没有按日窗口）"
    )
    result = BackfillResult(
        table=table,
        start=start,
        end=resolved_end,
        ranged=ranged,
        pool=len(symbols),
        scope=scope,
        attempts=attempts,
        breaker=breaker,
        page_size=resolved_page_size,
        outcomes=tuple(outcomes),
        sources=tuple(sorted(source_counts.items())),
        coverage_symbols=cov_symbols,
        coverage_days=cov_days,
        coverage_from=cov_from,
        coverage_to=cov_to,
        markdown="",  # 先占位再 replace：渲染要读聚合属性，归档要读渲染结果，一次构造装不下
        report=directory,
        breaker_tripped=breaker_tripped,
    )
    markdown = _render(window, result)
    report = _archive(markdown, table=table, directory=directory, moment=moment)
    return replace(result, markdown=markdown, report=report)


def _process_symbol(
    table: str,
    symbol: str,
    *,
    ranged: bool,
    start: date,
    end: date,
    fetch: FetchFn,
    anchor: AnchorFn,
    root: Path,
    attempts: int,
    page_size: int,
    backoff: float,
    sleep: Callable[[float], None],
) -> SymbolOutcome:
    """一只票的完整处理。失败都收成 outcome（不抛），熔断与退出码由 run 汇总。"""
    refs = BarRefs.load(symbol, start, end, root=root) if ranged else BarRefs.none(symbol)
    if ranged:
        if not refs.traded:
            return SymbolOutcome(symbol, "skipped", reason=SKIP_NO_REFERENCE)
        missing = set(refs.traded) - existing_dates(table, symbol, start, end, root=root)
        if not missing:
            # 读盘判据的核心：应有集全在册，整票一个请求都不发（#53 的"不重复劳动"）。
            return SymbolOutcome(symbol, "skipped", reason=SKIP_COMPLETE)

    source = ""
    items: list[list[str]] = []
    fields: list[str] = []
    last = ""
    for attempt in range(attempts):
        try:
            source, items, fields, _total = fetch_range(
                table,
                symbol,
                start if ranged else None,
                end,
                fetch=fetch,
                page_size=page_size,
            )
            last = ""
            break
        except RelayUnavailable as exc:
            # client 内已按 ADR-0014 走完 1→5→30 分钟阶梯与切源；任务层再试是"换个时辰
            # 再问一次"，中间小睡（zx-minute 同款 backoff×2ⁿ）而不是立刻撞墙。
            last = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < attempts:
                sleep(backoff * 2**attempt)
        except ValueError as exc:
            # 截断 / fields 漂移：同一个请求重发一万次也是同一个答案，不烧 attempts。
            return SymbolOutcome(
                symbol, "failed", reason=f"{type(exc).__name__}: {exc}", source=source
            )
    else:
        return SymbolOutcome(
            symbol, "failed", reason=f"重试 {attempts} 次仍失败：{last}", source=source
        )

    try:
        rows = PARSERS[table](source, fields, items)
    except ValueError as exc:
        # 形状/值域拒收是源给错了东西，不是网络抖动——当场记败，不重发同一个请求。
        return SymbolOutcome(symbol, "failed", reason=f"解析拒收：{exc}", source=source)

    spec = tables.table_spec(table)
    date_field = spec.date_field
    if ranged:
        foreign = sorted({row.symbol for row in rows} - {symbol})
        if foreign:
            return SymbolOutcome(
                symbol,
                "failed",
                reason=f"响应混入其它代码 {','.join(foreign[:5])}：源没按 ts_code 过滤，不可信",
                source=source,
            )
        rows = [row for row in rows if start <= getattr(row, date_field) <= end]
    fetched = len(rows)
    if not rows:
        return SymbolOutcome(
            symbol,
            "no_rows",
            reason="源无行（非交易日/停牌，或源没有这段数据）",
            source=source,
        )

    problems, unanchored, kept, notes = anchor(rows, refs)
    if problems:
        tail = f" 等 {len(problems)} 条" if len(problems) > 1 else ""
        return SymbolOutcome(
            symbol,
            "failed",
            reason="锚点对账整批拒（ADR-0015 决定 3，一行不写）：" + problems[0] + tail,
            source=source,
            fetched=fetched,
            unanchored=unanchored,
            exdiv_notes=tuple(notes),
        )
    if not kept:
        return SymbolOutcome(
            symbol,
            "no_rows",
            reason=f"{fetched} 行全部锚不上被剔除（主数据/日线缺——没得对，不是对不上）",
            source=source,
            fetched=fetched,
            unanchored=unanchored,
            exdiv_notes=tuple(notes),
        )
    ledger = tables.write_table(kept, table=table, root=root)
    return SymbolOutcome(
        symbol,
        "landed",
        source=source,
        fetched=fetched,
        kept=len(kept),
        added=ledger.added,
        repaired=ledger.repaired,
        rewritten=ledger.rewritten,
        unanchored=unanchored,
        exdiv_notes=tuple(notes),
    )


def _progress_line(done: int, total: int, outcome: SymbolOutcome) -> str:
    """逐票进度一行：长跑（全市场几千票）要看得见走到哪、这票什么下场。"""
    line = f"[{done}/{total}] {outcome.symbol} {outcome.status}"
    if outcome.added:
        line += f" 新增 {outcome.added}"
    if outcome.existing_skipped:
        line += f" 跳过已有 {outcome.existing_skipped}"
    if outcome.reason and outcome.status in ("failed", "skipped", "no_rows"):
        line += f"：{outcome.reason}"
    return line


def _render(window: str, result: BackfillResult) -> str:
    """日报正文。报告要回答任务 #53 点名的四件事：新增/改写、跳过、失败、盘上最终覆盖。"""
    source_desc = (
        "、".join(f"{name} {count} 票" for name, count in result.sources) or "（无供数记录）"
    )
    skipped = [outcome for outcome in result.outcomes if outcome.status == "skipped"]
    complete = sum(1 for outcome in skipped if outcome.reason == SKIP_COMPLETE)
    no_reference = sum(1 for outcome in skipped if outcome.reason == SKIP_NO_REFERENCE)
    no_rows = sum(1 for outcome in result.outcomes if outcome.status == "no_rows")
    breaker_kept = sum(1 for outcome in result.outcomes if outcome.status == "breaker")
    lines = [
        f"# 转接源参考表回填 · {result.table} · {window}",
        "",
        f"- 池子 {result.pool} 只（{result.scope}），供数源 {source_desc}；"
        f"attempts={result.attempts}、breaker={result.breaker}、page_size={result.page_size}",
        f"- 落盘：新增 {result.added} 行、修复 {result.repaired} 行、"
        f"重写 {result.rewritten} 个分区文件；同键同值跳过 {result.existing_skipped} 行"
        "（读盘/幂等判据，一行没动）",
        f"- 锚不上剔除 {result.unanchored} 行（主数据/日线缺——没得对，不是对不上，别读成全数入库）",
        f"- 逐票结果：落盘 {len(result.landed)}、无行 {no_rows}、读盘跳过 {len(skipped)}"
        f"（覆盖已齐 {complete} / 日线无参考 {no_reference}）、"
        f"失败 {len(result.failed)}、熔断未碰 {breaker_kept}",
    ]
    notes = result.exdiv_notes
    if notes:
        lines.append(f"- 参考价待核 {len(notes)} 条（除权日两界自洽，放行）：")
        lines += [f"  - {note}" for note in notes[:5]]
        if len(notes) > 5:
            lines[-1] = f"  - …共 {len(notes)} 条"
    if result.failed:
        lines.append("")
        lines.append(f"- 失败 {len(result.failed)} 只（原因）：")
        lines += [f"  - {outcome.symbol}：{outcome.reason}" for outcome in result.failed[:20]]
        if len(result.failed) > 20:
            lines.append(f"  - …共 {len(result.failed)} 只")
    if result.breaker_tripped:
        lines.append(
            f"- **熔断收工**：连败已达 breaker={result.breaker}，剩下 "
            f"{breaker_kept} 只没碰——收工不是掩盖缺数，换源（--relay）后重跑会从盘上接着"
        )
    if result.coverage_symbols:
        lines.append(
            f"- 盘上最终覆盖：{result.table} 共 {result.coverage_symbols} 只 × "
            f"{result.coverage_days} 个日期"
            f"（{result.coverage_from} .. {result.coverage_to}）"
        )
    else:
        lines.append(f"- 盘上最终覆盖：{result.table} 还没有任何行")
    return "\n".join(lines) + "\n"


def _archive(markdown: str, *, table: str, directory: Path, moment: datetime) -> Path:
    """归档到 `<目录>/<今天>/backfill-<表>-<时分秒>.md`（对齐 pull 的日期分目录）。

    文件名带时分秒而不是只有日期：同一天会跑很多次（中断重跑、验证幂等），覆盖掉上一份
    等于把"那次跑发生了什么"的证据抹掉——报告是留痕，不是看板。
    """
    day_dir = directory / moment.date().isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"backfill-{table}-{moment.strftime('%H%M%S')}.md"
    # 同一秒同表的两次跑（测试里的 now 注入）会撞名：序号后缀兜底，不覆盖已留痕的那份。
    counter = 1
    while path.exists():
        path = day_dir / f"backfill-{table}-{moment.strftime('%H%M%S')}-{counter}.md"
        counter += 1
    path.write_text(markdown, encoding="utf-8")
    return path
