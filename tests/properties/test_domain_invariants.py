"""L3 属性测试（03 §二 L3）：OHLC 不变量与复权恒等式。

这里刻意不用被测实现当判据。`_ohlc_holds` 是把 04 §二 R001 那行不等式按"两两 ≥"
另写一遍的独立判据：实现里 `max()/min()` 与 NaN 的相互作用出过岔子（close=NaN 且
其余合法的K线当时算"通过"），同源自证永远抓不到它。
"""

import math
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from tests.properties._profile import property_settings
from zhixing_quant.domain.adjust import (
    REL_TOL,
    close_enough,
    from_forward,
    to_backward,
    to_forward,
)
from zhixing_quant.domain.bar import Bar, ohlc_violations

# 含 NaN/±inf/0/负数：这几类正是适配器会原样递给门禁的东西
# （hypothesis 不允许 allow_nan 与区间共存，所以用 one_of 显式把边界形状加进来）
price = st.one_of(
    st.floats(min_value=-1e6, max_value=1e6, allow_infinity=False),
    st.just(float("nan")),
    st.just(float("inf")),
    st.just(float("-inf")),
)
positive = st.floats(min_value=1e-4, max_value=1e6, allow_nan=False, allow_infinity=False)
factor = st.floats(min_value=1e-3, max_value=1e3, allow_nan=False, allow_infinity=False)


def _ohlc_holds(open_: float, high: float, low: float, close: float) -> bool:
    """R001 不等式的独立写法：非有限值先判不成立，比较拆成四条两两 ≥。

    inf 必须显式排除而不是交给不等式：`inf >= inf` 成立，所以全 inf 的四元组在比较式
    眼里"完美合法"，而那样的价格是脏数据里最不该进干净区的一种。
    """
    if not all(math.isfinite(x) for x in (open_, high, low, close)):
        return False
    return high >= open_ and high >= close and open_ >= low and close >= low


@given(price, price, price, price)
@property_settings
def test_ohlc_predicate_agrees_with_the_rule_text(
    open_: float, high: float, low: float, close: float
) -> None:
    assert bool(ohlc_violations(open_, high, low, close)) is not _ohlc_holds(
        open_, high, low, close
    )


@given(price, price, price, price)
@property_settings
def test_clean_zone_model_refuses_exactly_what_the_rule_refuses(
    open_: float, high: float, low: float, close: float
) -> None:
    """Bar 构造失败 ⟺ R001 或 R002 判定不通过。两侧都不许比对方宽。"""
    row = {
        "symbol": "600519",
        "trade_date": date(2024, 1, 2),
        "volume": 1.0,
        "amount": 1.0,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
    }
    legal = _ohlc_holds(open_, high, low, close) and all(x > 0 for x in (open_, high, low, close))
    if legal:
        assert Bar.model_validate(row)
    else:
        with pytest.raises(ValidationError):
            Bar.model_validate(row)


@given(positive, factor, factor)
@property_settings
def test_forward_backward_round_trip(raw: float, f: float, base: float) -> None:
    """不复权 → 前复权 → 不复权 还原（03 §二 L3 复权恒等式）。"""
    assert close_enough(from_forward(to_forward(raw, f, base), f, base), raw, REL_TOL)


@given(positive, factor, factor)
@property_settings
def test_backward_forward_relationship(raw: float, f: float, base: float) -> None:
    """前复权 = 后复权 ÷ 基准因子：两条换算式必须给出同一个数。"""
    assert close_enough(to_forward(raw, f, base), to_backward(raw, f) / base)
