"""参考表的通用读写（ADR-0015 决定 2）：非行情 dataset 的落盘与读出。

与 `store_bars` 的分工：幂等语义**逐条相同**（同键同值不写、同键异值修复、跨批冲突抛、
相等就不重写文件），共享的是 `layout` 的分区形状与 `partition` 的原子替换原语；不共享的
是行——这里收各表自己的 NamedTuple，不认 `Bar`。`store_bars` 因此一字不动。

读出走 `read_table`：`query.read_bars` 对参考表会因列名对不上而炸，类型混淆在入口就响——
这是 ADR-0015 后果第二条的机器形态。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from operator import attrgetter
from pathlib import Path
from typing import Any, NamedTuple

import duckdb

from zhixing_quant.storage import layout, partition
from zhixing_quant.storage.write import BarConflict


class StkLimitRow(NamedTuple):
    """与 `sources.relay.tables.StkLimitRow` 同形同序——storage 层不 import sources 层，
    形状一致由两侧的 TableSpec/列名投影保证，`tests/test_storage_tables.py` 钉互认。"""

    source: str
    symbol: str
    trade_date: date
    up_limit: float
    down_limit: float


@dataclass(frozen=True)
class TableSpec:
    """一张参考表的形状：列、行标识、排序键、行类型。与 `layout.DatasetSpec` 同一三件套。"""

    name: str
    columns: tuple[tuple[str, str], ...]
    key: tuple[str, ...]
    order: tuple[str, ...]
    row: type[Any]  # 各表自己的 NamedTuple；type[Any] 防 mypy 把 type(row) is not 收窄成 NamedTuple
    #: 分区年月取自哪一列：行情表是 trade_date，公告类表是 ann_date（ADR-0015）。
    date_field: str = "trade_date"

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns)


class DailyBasicRow(NamedTuple):
    """与 `sources.relay.tables.DailyBasicRow` 同形同序（结构互认见模块说明）。"""

    source: str
    symbol: str
    trade_date: date
    close: float
    turnover_rate: float | None
    turnover_rate_f: float | None
    volume_ratio: float | None
    pe: float | None
    pe_ttm: float | None
    pb: float | None
    ps: float | None
    ps_ttm: float | None
    dv_ratio: float | None
    dv_ttm: float | None
    total_share: float | None
    float_share: float | None
    free_share: float | None
    total_mv: float | None
    circ_mv: float | None


class ForecastRow(NamedTuple):
    """与 `sources.relay.tables.ForecastRow` 同形同序（结构互认见模块说明）。"""

    source: str
    symbol: str
    ann_date: date
    end_date: date
    type: str
    p_change_min: float | None
    p_change_max: float | None
    net_profit_min: float | None
    net_profit_max: float | None
    summary: str


SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        name="stk_limit",
        columns=(
            ("source", "VARCHAR"),
            ("symbol", "VARCHAR"),
            ("trade_date", "DATE"),
            ("up_limit", "DOUBLE"),
            ("down_limit", "DOUBLE"),
        ),
        key=("trade_date",),
        order=("trade_date",),
        row=StkLimitRow,
    ),
    TableSpec(
        name="forecast",
        columns=(
            ("source", "VARCHAR"),
            ("symbol", "VARCHAR"),
            ("ann_date", "DATE"),
            ("end_date", "DATE"),
            ("type", "VARCHAR"),
            ("p_change_min", "DOUBLE"),
            ("p_change_max", "DOUBLE"),
            ("net_profit_min", "DOUBLE"),
            ("net_profit_max", "DOUBLE"),
            ("summary", "VARCHAR"),
        ),
        key=("ann_date", "end_date"),
        order=("ann_date", "end_date"),
        row=ForecastRow,
        date_field="ann_date",
    ),
    TableSpec(
        name="daily_basic",
        columns=(
            ("source", "VARCHAR"),
            ("symbol", "VARCHAR"),
            ("trade_date", "DATE"),
            ("close", "DOUBLE"),
            ("turnover_rate", "DOUBLE"),
            ("turnover_rate_f", "DOUBLE"),
            ("volume_ratio", "DOUBLE"),
            ("pe", "DOUBLE"),
            ("pe_ttm", "DOUBLE"),
            ("pb", "DOUBLE"),
            ("ps", "DOUBLE"),
            ("ps_ttm", "DOUBLE"),
            ("dv_ratio", "DOUBLE"),
            ("dv_ttm", "DOUBLE"),
            ("total_share", "DOUBLE"),
            ("float_share", "DOUBLE"),
            ("free_share", "DOUBLE"),
            ("total_mv", "DOUBLE"),
            ("circ_mv", "DOUBLE"),
        ),
        key=("trade_date",),
        order=("trade_date",),
        row=DailyBasicRow,
    ),
)
_BY_NAME = {spec.name: spec for spec in SPECS}


def table_spec(table: str) -> TableSpec:
    try:
        return _BY_NAME[table]
    except KeyError:
        raise ValueError(f"未知参考表 {table!r}，可选 {sorted(_BY_NAME)}") from None


@dataclass(frozen=True)
class TableWriteReport:
    """与 `write.WriteReport` 同款账本：`rewritten=0` 就是这次重跑什么都没改。"""

    partitions: int
    added: int
    repaired: int
    rewritten: int


def write_table(rows: Sequence[Any], *, table: str, root: Path | None = None) -> TableWriteReport:
    """`rows` 的静态类型给 `Any` 是有意的：每张表有自己的 NamedTuple，而 `spec.row` 的
    运行时类型检查（下面第一个 if）是这里真正的守门——静态上把每张表各写一个重载，
    换来的是投影错位照样编译通过，不如运行时那一条响得可靠。"""
    """落一批参考表行。空批次什么都不做；行类型与表不符当场响（投影错位不留到读回）。"""
    spec = table_spec(table)
    groups: dict[Path, dict[tuple[object, ...], Any]] = {}
    for row in rows:
        # 结构检查而非类型身份：storage 层不 import sources 层，两边的行模型是同形不同名
        # 的两个类——`_fields` 与列名逐一对齐就接受，投影错位在这里响，不留到读回。
        if getattr(row, "_fields", None) != spec.names:
            raise ValueError(
                f"表 {spec.name} 收到了 {type(row).__name__}"
                f"（fields={getattr(row, '_fields', None)}）：行形状与列定义不符，"
                "存储层不替它投影"
            )
        symbol = row.symbol
        year = getattr(row, spec.date_field).year
        path = layout.partition_path(symbol, year, dataset=spec.name, root=root)
        key = tuple(getattr(row, name) for name in spec.key)
        if any(part is None for part in key):
            raise ValueError(f"{symbol} 的主键列为空（{','.join(spec.key)}）：没有身份的行不落盘")
        slot = groups.setdefault(path, {})
        previous = slot.get(key)
        if previous is not None and previous != row:
            raise BarConflict(
                f"{symbol}@{getattr(row, spec.date_field)} 在这一批里给了两条不同的行"
                f"（{previous.source} 与 {row.source}）：存储层不替它们裁决谁对"
            )
        slot[key] = row

    added = repaired = rewritten = 0
    if groups:
        with duckdb.connect() as con:
            for path in sorted(groups):
                existing = _read(con, path, spec)
                merged, gained, changed = _merge(existing, groups[path], spec)
                added += gained
                repaired += changed
                if merged == existing:
                    continue
                partition.rewrite(con, path, spec.columns, [_values(row, spec) for row in merged])
                rewritten += 1
    return TableWriteReport(
        partitions=len(groups), added=added, repaired=repaired, rewritten=rewritten
    )


def read_table(
    table: str,
    symbol: str,
    start: date,
    end: date,
    *,
    root: Path | None = None,
) -> list[Any]:
    """一只票一段区间的参考表行，按 `spec.order` 升序。没落过盘就是空，不是错。

    返回类型给 `list[Any]`：每张表的具体行类型由表名决定，调用方按名取用；静态上
    收窄到具体 NamedTuple 需要按表重载，值不过这条查询的体量。
    """
    spec = table_spec(table)
    out: list[Any] = []
    with duckdb.connect() as con:
        for path in layout.partitions(symbol, start, end, dataset=spec.name, root=root):
            out.extend(partition.read(con, path, spec.names, spec.row))
    in_range = [r for r in out if start <= r.trade_date <= end]
    return sorted(in_range, key=attrgetter(*spec.order))


def _read(con: Any, path: Path, spec: TableSpec) -> list[Any]:
    return sorted(partition.read(con, path, spec.names, spec.row), key=attrgetter(*spec.order))


def _merge(
    existing: Sequence[Any],
    incoming: dict[tuple[object, ...], Any],
    spec: TableSpec,
) -> tuple[list[Any], int, int]:
    by_key: dict[tuple[object, ...], NamedTuple] = {
        tuple(getattr(row, name) for name in spec.key): row for row in existing
    }
    added = repaired = 0
    for key, row in incoming.items():
        current = by_key.get(key)
        if current is None:
            added += 1
        elif current != row:
            repaired += 1
        by_key[key] = row
    return sorted(by_key.values(), key=attrgetter(*spec.order)), added, repaired


def _values(row: Any, spec: TableSpec) -> tuple[object, ...]:
    return tuple(getattr(row, name) for name in spec.names)
