"""牛股王日K适配器：`timedata`（`type=5`）→ `BarDraft`。

与分钟适配器同一条分工：**这里不碰网络**，抓取在 `client.py`。行形状与分钟同一套
（2026-11-11 实测 300308，`client.KLINE_TYPE_DAILY`）：

- `times` 是 `YYYYMMDD000000`——日K没有时刻，取日期部分，`ts` 留空（`BarDraft.identity`
  对日线本就退化回 `(symbol, trade_date)`）。
- 价格字段同分钟：`openp`/`highp`/`lowp`/`nowv` 是整数分的字符串 → ÷100 → 元
  （`nowv` 即收盘，分钟侧同一字段）；`curvol` = 股原样、`curvalue` = 元不除 100
  ——量纲判据与分钟适配器硬约束 4 同一条实测，不在这里另立一份。
- 解析不出 `times` 的行**不编日期**：`trade_date` 留空交 R010 判（与分钟/ akshare
  适配器「认不出给 None」同一条）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from zhixing_quant.domain.bar import BarDraft
from zhixing_quant.sources.rows import to_float

#: 源标识（04 §三 按源打分）。改名等于给历史分数换了主语，不许随手改。
SOURCE = "ngw_daily"


def _day(value: object) -> date | None:
    """`times`（YYYYMMDDHHMMSS）→ 日期。认不出给 None，不猜（rows.to_date 同款）。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 14 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _yuan(value: object) -> float | None:
    """分（字符串）→ 元。源给的是整数分的字符串（`"54200"` → 542.00）。"""
    number = to_float(value)
    return None if number is None else number / 100.0


def daily_drafts(rows: Sequence[Mapping[str, object]], *, symbol: str) -> list[BarDraft]:
    """一行 `timedata` → 一条待判定的日K线。行序原样保留（R009 判的是源交来的顺序）。"""
    return [
        BarDraft(
            source=SOURCE,
            symbol=symbol,
            trade_date=_day(row.get("times")),
            ts=None,
            open=_yuan(row.get("openp")),
            high=_yuan(row.get("highp")),
            low=_yuan(row.get("lowp")),
            close=_yuan(row.get("nowv")),
            volume=to_float(row.get("curvol")),
            # curvalue 单位是元（分钟适配器硬约束 4）：这里不除 100。
            amount=to_float(row.get("curvalue")),
            adj_factor=None,
            is_suspended=False,
        )
        for row in rows
    ]
