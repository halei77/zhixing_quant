"""转接源参考表的行模型、解析与表级校验（ADR-0015 决定 1、3）。

每张表三样东西，缺一不接：行模型（NamedTuple，列名与 `storage.tables.TableSpec` 对齐）、
`tushare fields/items → 行` 的解析器（`zip(fields, row)`，字段顺序不固定是这族源的已知
陷阱）、值域校验（不合法当场响，不产出"看着像数据"的行）。

解析器的输入就是快照里的形状（fields + items 的裸列表），CI 里离线重放用的同一形状。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any, NamedTuple

from zhixing_quant.domain.symbol import normalize_code


class StkLimitRow(NamedTuple):
    """`stk_limit` 一行：某票某日的涨跌停价（不复权绝对价，ADR-0015 背景第二条）。"""

    source: str
    symbol: str
    trade_date: date
    up_limit: float
    down_limit: float


def _field(row: Sequence[object], fields: Sequence[str], name: str) -> str:
    """按名取列。字段顺序随参数集漂是这族源的已知陷阱，按下标取等于埋雷。"""
    try:
        index = fields.index(name)
    except ValueError:
        raise ValueError(f"响应缺字段 {name!r}（fields={'、'.join(fields)}）") from None
    return str(row[index])


def _as_date(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"trade_date {text!r} 不是 YYYYMMDD") from exc


def parse_stk_limit(
    source: str, fields: Sequence[str], items: Sequence[Sequence[object]]
) -> list[StkLimitRow]:
    """一页 stk_limit → 校验过的行。值域不合法整批响：涨跌停价错一个就不是"少一行"的事，
    是这一天整页不可信——锚点对账（装载器）会再拦一层，这里只挡形状级错误。
    """
    rows: list[StkLimitRow] = []
    seen: set[tuple[str, date]] = set()
    for raw in items:
        symbol = normalize_code(_field(raw, fields, "ts_code"))
        trade_date = _as_date(_field(raw, fields, "trade_date"))
        up = float(_field(raw, fields, "up_limit"))
        down = float(_field(raw, fields, "down_limit"))
        if not up > down > 0:
            raise ValueError(f"{symbol}@{trade_date} 涨跌停价不成立：up={up} down={down}")
        key = (symbol, trade_date)
        if key in seen:
            raise ValueError(f"{symbol}@{trade_date} 在同一页里出现两次（limit/offset 分页重叠）")
        seen.add(key)
        rows.append(StkLimitRow(source, symbol, trade_date, up, down))
    return rows


class DailyBasicRow(NamedTuple):
    """`daily_basic` 一行：某票某日的每日估值指标（不复权口径，Qoute §8 陷阱清单的
    null 纪律在这里生效——源给 null 的字段保持 None，**禁止 0 填充**参与任何计算）。"""

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


def _opt(row: Sequence[object], fields: Sequence[str], name: str) -> float | None:
    """可空数值列：源给 null（字符串 'None' 或空）就保持 None——0 填充会把"没这数"
    洗成"这数为 0"，银行/保险的估值科目大面积是 null，那是它们的常态不是异常。"""
    text = _field(row, fields, name)
    if text in ("", "None", "nan", "NULL", "null"):
        return None
    value = float(text)
    if value != value:  # NaN
        return None
    return value


def parse_daily_basic(
    source: str, fields: Sequence[str], items: Sequence[Sequence[object]]
) -> list[DailyBasicRow]:
    rows: list[DailyBasicRow] = []
    seen: set[tuple[str, date]] = set()
    for raw in items:
        symbol = normalize_code(_field(raw, fields, "ts_code"))
        trade_date = _as_date(_field(raw, fields, "trade_date"))
        close = float(_field(raw, fields, "close"))
        if close <= 0:
            raise ValueError(f"{symbol}@{trade_date} 收盘价 {close} 不成立")
        key = (symbol, trade_date)
        if key in seen:
            raise ValueError(f"{symbol}@{trade_date} 在同一页里出现两次（分页重叠）")
        seen.add(key)
        rows.append(
            DailyBasicRow(
                source,
                symbol,
                trade_date,
                close,
                _opt(raw, fields, "turnover_rate"),
                _opt(raw, fields, "turnover_rate_f"),
                _opt(raw, fields, "volume_ratio"),
                _opt(raw, fields, "pe"),
                _opt(raw, fields, "pe_ttm"),
                _opt(raw, fields, "pb"),
                _opt(raw, fields, "ps"),
                _opt(raw, fields, "ps_ttm"),
                _opt(raw, fields, "dv_ratio"),
                _opt(raw, fields, "dv_ttm"),
                _opt(raw, fields, "total_share"),
                _opt(raw, fields, "float_share"),
                _opt(raw, fields, "free_share"),
                _opt(raw, fields, "total_mv"),
                _opt(raw, fields, "circ_mv"),
            )
        )
    return rows


#: 表名 → 解析器。zx-relay 按名取；新表在这里登记才算"可接"（ADR-0015 后果第一条）。
#: 类型显式给全：两张表的行类型不同，不注解会被 mypy 并成 object。
PARSERS: dict[str, Callable[[str, Sequence[str], Sequence[Sequence[object]]], list[Any]]] = {
    "stk_limit": parse_stk_limit,
    "daily_basic": parse_daily_basic,
}
