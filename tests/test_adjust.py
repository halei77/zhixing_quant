"""复权三口径换算（03 §二 L3 恒等式的单测面；随机轰炸在 tests/properties/）。"""

from datetime import date

import pytest

from zhixing_quant.domain.adjust import (
    AdjustError,
    AdjustmentFactor,
    close_enough,
    factor_on,
    from_forward,
    sorted_factors,
    to_backward,
    to_forward,
)


def _f(day: int, factor: float) -> AdjustmentFactor:
    return AdjustmentFactor("600519", date(2024, 1, day), factor)


def test_factor_must_be_positive() -> None:
    """因子为 0 或负会把价格整段抹平/翻号，且下游没有任何一处会再问一次。"""
    with pytest.raises(AdjustError):
        AdjustmentFactor("600519", date(2024, 1, 2), 0.0)
    with pytest.raises(AdjustError):
        AdjustmentFactor("600519", date(2024, 1, 2), -1.5)


def test_factor_on_takes_the_last_one_not_the_last_written() -> None:
    factors = [_f(5, 2.0), _f(2, 1.0)]
    assert factor_on(factors, date(2024, 1, 1)) == 1.0  # 首个因子之前按未除权
    assert factor_on(factors, date(2024, 1, 2)) == 1.0
    assert factor_on(factors, date(2024, 1, 6)) == 2.0
    assert factor_on(factors, date(2024, 1, 31)) == 2.0


def test_sorted_factors_is_order_independent() -> None:
    messy = [_f(9, 3.0), _f(2, 1.0), _f(5, 2.0)]
    assert [x.effective_on.day for x in sorted_factors(messy)] == [2, 5, 9]


def test_forward_price_equals_raw_on_the_base_day() -> None:
    """前复权的定义就是"尾日等于实价"，这条不成立就说明基准因子取错了。"""
    assert close_enough(to_forward(10.0, 2.0, 2.0), 10.0)


def test_backward_and_forward_differ_by_the_base_factor() -> None:
    raw, factor, base = 10.0, 2.0, 4.0
    assert close_enough(to_forward(raw, factor, base), to_backward(raw, factor) / base)


def test_base_factor_must_be_positive() -> None:
    with pytest.raises(AdjustError):
        to_forward(10.0, 2.0, 0.0)


@pytest.mark.parametrize(
    ("raw", "factor", "base"), [(10.0, 2.0, 4.0), (0.01, 1.0, 1.0), (1e6, 3.7, 2.1)]
)
def test_round_trip_restores_raw_price(raw: float, factor: float, base: float) -> None:
    assert close_enough(from_forward(to_forward(raw, factor, base), factor, base), raw)


def test_close_enough_is_scale_relative() -> None:
    assert close_enough(1e6, 1e6 + 1e-7)
    assert not close_enough(1.0, 1.0001)
    assert not close_enough(0.0, 1e-18)
    assert close_enough(0.0, 0.0)
