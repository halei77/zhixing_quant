"""转接源参考表的行模型、解析与表级校验（ADR-0015 决定 1、3）。

每张表三样东西，缺一不接：行模型（NamedTuple，列名与 `storage.tables.TableSpec` 对齐）、
`tushare fields/items → 行` 的解析器（`zip(fields, row)`，字段顺序不固定是这族源的已知
陷阱）、值域校验（不合法当场响，不产出"看着像数据"的行）。

解析器的输入就是快照里的形状（fields + items 的裸列表），CI 里离线重放用的同一形状。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import NamedTuple

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


#: 表名 → 解析器。zx-relay 按名取；新表在这里登记才算"可接"（ADR-0015 后果第一条）。
PARSERS = {"stk_limit": parse_stk_limit}
