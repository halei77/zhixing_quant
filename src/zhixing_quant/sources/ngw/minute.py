"""牛股王 1 分钟K 适配器：`timedata` → `BarDraft`（设计合成 §2.3 四条硬约束逐条兑现）。

与 akshare 分钟适配器同一条分工：**这里不碰网络**，抓取在 `client.py`，样本在 CI 里离线
重放（03 §二 L2）。四条硬约束（2026-09-24 实测证据见数据根
`reports/source-probe/2026-09-24-ngw-niuguwang-source-access.md`）：

1. **`ts` 映射 = `times` 原样解析（identity），不再逐段 +1min 重标**。
   实测完整交易日 241 根，标签 = 09:30（开盘竞价栏，OHLC 全等开盘价）+ 09:31–11:30 +
   13:01–15:00；后两段恰好是 A股标准 1 分钟**右端点**集合（240 根），identity 即 ADR-0009
   决定 2 的「收盘时刻」。设计合成建议的「上午 +1min 且 11:30→11:30、下午 +1min 且
   14:59→15:00」建立在「末根 14:59、缺 15:00、共 240 根」的实测上，而那是 `start` 截止
   参数**排他**边界的假象：`start=20260923150000` → 240 根末根 14:59（复现调研数）、
   `start=20260923145900` → 239 根末根 14:58、不传 `start` → 241 根含 15:00。对真实标签
   做 +1min 还会在 11:30（11:29+1 与钉住的 11:30 撞）与 15:00（14:59+1 与 15:00 撞）
   两处撞键——同一分钟两根K线一个主键，R008 只能拒一条。故 identity + 会话外标签丢弃，
   黄金样本把边界（09:30/11:30/13:01/15:00）钉死。
   解析不出 `times` 的行**不编日期**：`trade_date`/`ts` 留空交 R010 判（与 akshare 适配器
   「认不出给 None」同一条）；能解析但落在午休/盘外的行整行丢弃——1 分钟K 不可能收盘在
   午休，源实测从不给，丢弃的防御分支由 L3 性质测试钉住（保序、单射、不制造午休 ts）。
2. **`ex` 不传 = 不复权**（ADR-0009 决定 4「抓不复权、因子在日线」）：本层不碰参数，
   `client.kline` 默认不传；同刻三口径实测 600519@20260106145400：不复权 1427.88 /
   ex=1（前复权）1399.86 / ex=2（后复权）8431.67——同一分钟干净区内绝不混复权语义。
3. **`count≥1500` 静默 0 行**：拦在 `client.kline` 的参数校验（发不出去就不会冒充
   「到底了」）；本层 0 行 → 返回空列表，调用方把它当「这批没有行」（日报的
   REASON_NO_ROWS 显式分支），「到底了」只由 `KlinePage.exhausted` 说了算。
4. **单位**：OHLC 是字符串·分 → ÷100 → 元；`curvol` = 股原样；**`curvalue` = 元，不除 100**
   ——实测 `curvalue/(curvol×价)` p50=1.0000（238 根），20260924150000 那根
   27663×1237.00=34,219,131 与 `curvalue` 逐分相等。Qoute T-001 写「`curvalue/100`」
   把量纲记反了（fixture 按分造的数），照抄会把成交额缩小 100 倍。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from datetime import time as time_of

from zhixing_quant.domain.bar import BarDraft
from zhixing_quant.sources.rows import to_float
from zhixing_quant.storage import layout


def source_for(period: str) -> str:
    """周期（`1`/`5`/`30`/`60`）→ 源标识。认不出的周期在这里就抛（周期表只有 layout 一份）。"""
    return f"ngw_{layout.minute_dataset(period)}"


def _stamp(value: object) -> datetime | None:
    """`times`（YYYYMMDDHHMMSS）→ naive datetime。认不出给 None，不猜（rows.to_date 同款）。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) != 14 or not text.isdigit():
        return None
    try:
        return datetime.strptime(text, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _in_session(stamp: datetime) -> bool:
    """这根K线的收盘时刻在不在两个交易段里：上午 [09:30, 11:30]、下午 [13:01, 15:00]。

    上界含 11:30/15:00（右端点标签），下界 09:30 含开盘竞价栏、13:01 排除 13:00——
    实测标签集的闭区间恰是这两个段，午休 (11:30, 13:01) 与盘外一律 False。
    """
    moment = stamp.time()
    return time_of(9, 30) <= moment <= time_of(11, 30) or time_of(13, 1) <= moment <= time_of(15, 0)


def _yuan(value: object) -> float | None:
    """分（字符串）→ 元。源给的是整数分的字符串（`"125158"` → 1251.58）。"""
    number = to_float(value)
    return None if number is None else number / 100.0


def minute_drafts(rows: Sequence[Mapping[str, object]], *, symbol: str) -> list[BarDraft]:
    """一行 `timedata` → 一条待判定的 1 分钟K线。行序原样保留（R009 判的是源交来的顺序）。

    `period` 不是参数：ngw 这层只供 1 分钟（type=11），源标识恒为 `ngw_minute_1`——
    60 分钟的接口挂了不该扣 1 分钟的健康分，反过来 1 分钟的口径也别长进别的周期里。
    """
    source = source_for("1")
    drafts: list[BarDraft] = []
    for row in rows:
        stamp = _stamp(row.get("times"))
        if stamp is not None and not _in_session(stamp):
            # 能解析但不在交易段（午休 11:31–13:00、盘外）：这不是一根K线，整行丢弃。
            continue
        drafts.append(
            BarDraft(
                source=source,
                symbol=symbol,
                trade_date=stamp.date() if stamp is not None else None,
                ts=stamp,
                open=_yuan(row.get("openp")),
                high=_yuan(row.get("highp")),
                low=_yuan(row.get("lowp")),
                close=_yuan(row.get("nowv")),
                volume=to_float(row.get("curvol")),
                # curvalue 单位是元（硬约束 4）：这里不除 100，T-001 的「/100」是错的。
                amount=to_float(row.get("curvalue")),
                adj_factor=None,
                is_suspended=False,
            )
        )
    return drafts
