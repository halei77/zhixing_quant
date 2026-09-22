"""akshare 证券主数据：交易所上市列表 → `Listing`（04 §五 接入清单第 3 项）。

一个函数同时服务沪市（`stock_info_sh_name_code`）与深市（`stock_info_sz_name_code`）：
两个源的列名不同而形状相同，别名表交给 `pick` 处理，比开两个适配器少一份会漂的重复逻辑。

不取源自带的"板块"列：板块一律由代码前缀经 `domain.symbol.board_of` 现判。落库就有两份
事实，而两份不一致时（源的板块列写的是历史归属）R004 按哪一份判都没有依据。

退市名单（`stock_info_sh_delist` 等）不在本层：Step 2a 只要"在册 + 上市日"，退市区间是
Step 3 股票池 PIT 还原的活，届时它和 `Listing.delisted_on` 一起接。

ST 帽是从证券简称的**前缀**推出来的，而前缀只是"抓取那天看到的样子"：所以那一段区间的起点
是 `manifest.csv` 里的抓取日，快照日之前一律不判（帽可能是那之后才戴上的）。真正的 ST 历史
要等一个能给出起止日期的源（01 §四 主数据条目里的"ST 状态历史"）。
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from zhixing_quant import config
from zhixing_quant.domain.security import Interval, Listing, SecurityMaster
from zhixing_quant.domain.symbol import UnknownCode, board_of, normalize_code
from zhixing_quant.sources.rows import SourceSchemaError, pick, to_date

# ts_code：relay stock_basic 那份 BSE 名单（akshare 没有 BSE 名单接口）。
CODE_ALIASES = ("证券代码", "A股代码", "ts_code", "code")
NAME_ALIASES = ("证券简称", "A股简称", "name")
LISTED_ALIASES = ("上市日期", "A股上市日期", "list_date")
#: 两份交易所名单的文件名，与 `tools/capture_golden.py` 的 key 一致：两处不同名就是两份主数据。
#: 四份名单（2026-09-22 用户裁决扩板：主板/创业板/科创板/北交所一个不能少——ADR-0013
#: 补充决定三的待裁决就此了结）。北交所那份来自 relay 的 stock_basic（akshare 没有 BSE
#: 名单接口），列名 ts_code/name/list_date 落在 ALIASES 的英文兜底里，不用特判。
SNAPSHOT_NAMES = (
    "stock_info_sh_name_code__主板A股",
    "stock_info_sz_name_code__A股列表",
    "stock_info_sh_name_code__科创板",
    "relay_stock_basic__BJ",
)
#: 抓取清单的文件名，同样与 `tools/capture_golden.py` 一致。ST 判定要读它：没有抓取日期，
#: "这名字挂着帽"就是一条不知道从哪天起生效的断言，而 PIT 模型不接受没有时点的状态。
MANIFEST_NAME = "manifest.csv"
#: 快照里真实存在的两种写法（`ST人福`、`*ST九鼎`）。`SST`/`S*ST` 是股权分置改革年代的老帽子，
#: 源早就不产出了，但认出来不花钱——漏认等于拿 10% 的上限去判一只只许涨 5% 的票。
ST_PREFIXES = ("ST", "*ST", "SST", "S*ST")


def is_st_name(name: str) -> bool:
    """证券简称是否挂着风险警示帽。只认前缀：帽子写在名字里任何别的位置都不是帽子。"""
    text = name.strip().upper()
    return any(text.startswith(prefix) for prefix in ST_PREFIXES)


@dataclass(frozen=True)
class SkippedRow:
    """一行没进主数据的位置、原文与原因。计数要有出处，否则"这次少 200 只"查不回去。"""

    position: int
    raw_code: str
    reason: str


@dataclass(frozen=True)
class MasterLoad:
    listings: tuple[Listing, ...]
    skipped: tuple[SkippedRow, ...]
    #: 只由 `read_master` 填：行级入口（`listings_from_rows`）拿不到抓取日期。
    st_periods: tuple[Interval, ...] = ()

    def to_master(self) -> SecurityMaster:
        """装配成可查询的主数据。

        装配放在这里而不是让调用方自己拼两个参数：漏掉 `st_periods` 的那份主数据在日报上
        与"这个市场今天没有 ST 票"一模一样，而它真正的意思是 R004 的 5% 档整条失效。
        """
        return SecurityMaster(self.listings, self.st_periods)


def _parse_row(row: Mapping[str, object]) -> Listing | str:
    """一行 → 要么是一条登记，要么是它不能入库的原因。"""
    code = str(pick(row, *CODE_ALIASES) or "").strip()
    name = str(pick(row, *NAME_ALIASES) or "").strip()
    listed_on = to_date(pick(row, *LISTED_ALIASES))
    try:
        # 沪市列表里混着 B 股（900xxx），深市混着债券：代码本身合法，但不属于本项目的
        # 四档板块表，R004 对它们没有判据。
        board_of(normalize_code(code))
    except UnknownCode:
        return f"代码 {code!r} 不属于四档板块（B股/债券/表外代码）"
    if not name:
        return "缺证券简称"
    if listed_on is None:
        return "上市日期解析不出来"
    return Listing(code=code, name=name, listed_on=listed_on)


def listings_from_rows(rows: Sequence[Mapping[str, object]]) -> MasterLoad:
    """源行 → 上市登记。跳过的行一律带原因返回，不静默丢。

    为什么这里不像日历那样整批拒收：交易所列表**本来**就含 B 股与债券，那些不是数据错误
    而是范围之外，拒收会让这个源永远装不进来。但"跳了几行、为什么跳"必须是返回值的一部分
    ——真出事时（列名变了、某板块整批前缀没进表）无声的 `continue` 会把股票池缩小 20%
    而没有任何一处代码报错。全空才拒收，那才是列名变了的样子。

    重复代码不在这里去重：交给 `SecurityMaster` 抛 `MasterConflict`。两个源各说一遍
    "什么算重复"，迟早判得不一致。
    """
    listings: list[Listing] = []
    skipped: list[SkippedRow] = []
    for position, row in enumerate(rows):
        parsed = _parse_row(row)
        if isinstance(parsed, str):
            raw_code = str(pick(row, *CODE_ALIASES) or "").strip()
            skipped.append(SkippedRow(position=position, raw_code=raw_code, reason=parsed))
            continue
        listings.append(parsed)
    if not listings:
        raise SourceSchemaError(
            f"{len(rows)} 行主数据一行都没解析出来，多半是列名变了："
            f"{sorted({s.reason for s in skipped})}"
        )
    return MasterLoad(listings=tuple(listings), skipped=tuple(skipped))


def snapshot_paths(directory: Path | None = None) -> tuple[Path, ...]:
    """两份交易所名单的快照位置（数据根 `golden/`，与日历同一处）。"""
    root = directory if directory is not None else config.golden_dir()
    return tuple(root / f"{name}.csv" for name in SNAPSHOT_NAMES)


def captured_on(directory: Path) -> date:
    """名单是哪一天抓的——ST 那段区间唯一的起点候选。

    两份名单各有一条 `captured_at` 时取**最晚**的那个：09-01 抓的名单里的 ST 票，从 09-19
    起算才不越界（我们只见过 09-19 那天的名字）。宁可少判几天，不多判没有证据的日子。
    """
    path = directory / MANIFEST_NAME
    if not path.is_file():
        raise SourceSchemaError(
            f"没有抓取清单 {path}：ST 帽的生效日期只能从快照的抓取时间拿，"
            "先跑 tools/capture_golden.py"
        )
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    days = [to_date(row.get("captured_at")) for row in rows if row.get("key") in SNAPSHOT_NAMES]
    if len(days) != len(SNAPSHOT_NAMES) or any(day is None for day in days):
        raise SourceSchemaError(
            f"{path} 里没有两份名单完整的抓取时间（要 {sorted(SNAPSHOT_NAMES)}）："
            "ST 帽从哪天起算判不出来，而判不出来在日报上与「今天没有 ST 票」长得一模一样"
        )
    return max(day for day in days if day is not None)


def read_master(directory: Path | None = None) -> MasterLoad:
    """离线读主数据快照：两份名单合起来读，跳过的行照原样带原因返回。

    合起来读而不是各读各的：`SecurityMaster` 要的是"那天在册的全市场"，两个入口迟早被
    用成一个（日报上就是少一半票）。这里不刷新、不联网——快照旧不旧是抓取边界的事。

    ST 区间在这里填：简称挂着帽的票，从抓取日起算一段"持续中"的 ST。往前不填——帽可能是
    快照那天之后才戴上的；往后填到下一次抓取为止，因为摘帽同样没有源可查，而帽一摘，下一次
    重抓就把这段截断了。这就是"主数据要定期重抓"这条运维口径在代码里的落点。
    """
    root = directory if directory is not None else config.golden_dir()
    observed_on = captured_on(root)
    rows: list[Mapping[str, object]] = []
    for path in snapshot_paths(root):
        if not path.is_file():
            raise SourceSchemaError(
                f"没有主数据快照 {path}：先跑 tools/capture_golden.py"
                "（少一份名单等于少一个交易所，股票池凭空缩小一半）"
            )
        with path.open(encoding="utf-8", newline="") as fh:
            rows += list(csv.DictReader(fh))
    load = listings_from_rows(rows)
    return replace(
        load,
        st_periods=tuple(
            Interval(item.code, observed_on) for item in load.listings if is_st_name(item.name)
        ),
    )
