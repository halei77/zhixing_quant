"""03 §二 L3 "门禁判定完备性"：脏数据必须 100% 被拦住，合法数据必须 100% 放行。

判据是**另写一遍**的：R001 的两两比较、R002 的符号检查、R004/R007 的倍数式限幅，
都不复用 `quality/rules.py` 里的算式。同源自证是这类测试唯一的失败方式——把实现
里的 `abs(change) > limit + tolerance` 抄一遍，实现错它也跟着错。

双向断言（`violated ⟺ 不在干净区`）比单向"脏数据被拦"强：单向容得下一个"什么都不放行"
的门禁，那种门禁分数好看、数据全废，是 04 §三 最不喜欢的一种失效。
"""

import math
from datetime import date
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from tests.properties._profile import property_settings
from zhixing_quant import config as app_config
from zhixing_quant.domain.bar import BarDraft
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Listing, SecurityMaster
from zhixing_quant.quality import gate_config
from zhixing_quant.quality.engine import GateEngine

PREV_CLOSE = 10.0
DAY1, DAY2 = date(2024, 1, 4), date(2024, 1, 5)
#: 上市日远早于日历首日 → 不在新股豁免窗口（R004 必须照判）。日历本身只有 5 天，
#: 这正是"增量抓取带截断日历"的形状。
MASTER = SecurityMaster([Listing("600519", "测试白酒", date(2000, 1, 4))])
CALENDAR = TradingCalendar([date(2024, 1, 2), date(2024, 1, 3), DAY1, DAY2, date(2024, 1, 8)])
ENGINE = GateEngine(gate_config.load(app_config.gate_config_file()), MASTER, CALENDAR)

REFERENCE = BarDraft(
    source="prop",
    symbol="600519",
    trade_date=DAY1,
    open=10.0,
    high=10.5,
    low=9.8,
    close=PREV_CLOSE,
    volume=1000.0,
    amount=10000.0,
    adj_factor=1.0,
)

# 合法形状：high 恒 ≥ 开/收，low 恒 ≤ 开/收，四价全正且收盘在昨收 ±10% 内。
_IN_RANGE = st.floats(min_value=9.0, max_value=11.0, allow_nan=False, allow_infinity=False)
_HIGH = st.floats(min_value=11.0, max_value=12.0, allow_nan=False, allow_infinity=False)
_LOW = st.floats(min_value=8.0, max_value=9.0, allow_nan=False, allow_infinity=False)
_COUNT = st.floats(min_value=0.0, max_value=1e5, allow_nan=False, allow_infinity=False)

# 破坏值：NaN/±inf 是"判不了"，0/-1 是"非正/非负"，区间随机是"可能越限也可能没越"。
_CORRUPT = st.one_of(
    st.just(float("nan")),
    st.just(float("inf")),
    st.just(float("-inf")),
    st.just(0.0),
    st.just(-1.0),
    st.floats(min_value=-1e3, max_value=1e3, allow_nan=False, allow_infinity=False),
)


@st.composite
def candidate(draw: Any) -> dict[str, float]:
    row = {
        "open": draw(_IN_RANGE),
        "high": draw(_HIGH),
        "low": draw(_LOW),
        "close": draw(_IN_RANGE),
        "volume": draw(_COUNT),
        "amount": draw(_COUNT),
    }
    field = draw(st.sampled_from(("none", "open", "high", "low", "close", "volume", "amount")))
    if field != "none":
        row[field] = draw(_CORRUPT)
    return row


def _shape_is_clean(row: dict[str, float]) -> bool:
    """04 §二 R001 + R002 的原文，逐条另写：有限性、两两大小、符号各自判。"""
    prices = (row["open"], row["high"], row["low"], row["close"])
    if not all(math.isfinite(x) for x in (*prices, row["volume"], row["amount"])):
        return False
    if not (row["high"] >= row["open"] and row["high"] >= row["close"]):
        return False
    if not (row["low"] <= row["open"] and row["low"] <= row["close"]):
        return False
    if row["high"] < row["low"]:
        return False
    if any(x <= 0 for x in prices):
        return False
    return row["volume"] >= 0 and row["amount"] >= 0


def _moves_are_within_limits(row: dict[str, float]) -> bool:
    """R004 与 R007 写成倍数而不是"百分比 > 上限 + 容差"：主板 10% + 0.5% 容差、
    R007 再加 0.5% 加宽（04 §二 "阈值与 R004 板块阈值联动"）。同一条判据换个写法，
    实现里方向搞反或忘了容差就会在这里露出来。
    """
    upper, lower = PREV_CLOSE * 1.105, PREV_CLOSE * 0.895
    if not lower <= row["close"] <= upper:
        return False
    return PREV_CLOSE * 0.89 <= row["open"] <= PREV_CLOSE * 1.11


@given(candidate())
@property_settings
def test_the_gate_blocks_exactly_the_rows_that_violate_the_invariants(
    row: dict[str, float],
) -> None:
    draft = BarDraft(
        source="prop",
        symbol="600519",
        trade_date=DAY2,
        adj_factor=1.0,
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume=row["volume"],
        amount=row["amount"],
    )
    outcome = ENGINE.run([REFERENCE, draft])
    admitted = any(b.trade_date == DAY2 for b in outcome.clean_zone)
    blocked = any(q.draft.trade_date == DAY2 for q in outcome.quarantined)

    should_pass = _shape_is_clean(row) and _moves_are_within_limits(row)
    reasons = [v.reason for q in outcome.quarantined for v in q.violations]
    assert admitted is should_pass, (row, reasons)
    # 拦下 ≠ 消失：被拒的行必须带着违规原因待在隔离区里（04 §一、ADR-0002 第 2 条）。
    assert blocked is not should_pass
