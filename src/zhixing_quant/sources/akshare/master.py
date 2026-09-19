"""akshare 证券主数据：交易所上市列表 → `Listing`（04 §五 接入清单第 3 项）。

一个函数同时服务沪市（`stock_info_sh_name_code`）与深市（`stock_info_sz_name_code`）：
两个源的列名不同而形状相同，别名表交给 `pick` 处理，比开两个适配器少一份会漂的重复逻辑。

不取源自带的"板块"列：板块一律由代码前缀经 `domain.symbol.board_of` 现判。落库就有两份
事实，而两份不一致时（源的板块列写的是历史归属）R004 按哪一份判都没有依据。

退市名单（`stock_info_sh_delist` 等）不在本层：Step 2a 只要"在册 + 上市日"，退市区间是
Step 3 股票池 PIT 还原的活，届时它和 `Listing.delisted_on` 一起接。
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from zhixing_quant import config
from zhixing_quant.domain.security import Listing
from zhixing_quant.domain.symbol import UnknownCode, board_of, normalize_code
from zhixing_quant.sources.rows import SourceSchemaError, pick, to_date

CODE_ALIASES = ("证券代码", "A股代码", "code")
NAME_ALIASES = ("证券简称", "A股简称", "name")
LISTED_ALIASES = ("上市日期", "A股上市日期", "list_date")
#: 两份交易所名单的文件名，与 `tools/capture_golden.py` 的 key 一致：两处不同名就是两份主数据。
SNAPSHOT_NAMES = ("stock_info_sh_name_code__主板A股", "stock_info_sz_name_code__A股列表")


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


def read_master(directory: Path | None = None) -> MasterLoad:
    """离线读主数据快照：两份名单合起来读，跳过的行照原样带原因返回。

    合起来读而不是各读各的：`SecurityMaster` 要的是"那天在册的全市场"，两个入口迟早被
    用成一个（日报上就是少一半票）。这里不刷新、不联网——快照旧不旧是抓取边界的事。
    """
    rows: list[Mapping[str, object]] = []
    for path in snapshot_paths(directory):
        if not path.is_file():
            raise SourceSchemaError(
                f"没有主数据快照 {path}：先跑 tools/capture_golden.py"
                "（少一份名单等于少一个交易所，股票池凭空缩小一半）"
            )
        with path.open(encoding="utf-8", newline="") as fh:
            rows += list(csv.DictReader(fh))
    return listings_from_rows(rows)
