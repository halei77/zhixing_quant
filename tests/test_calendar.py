"""交易日历（04 §二 R006 的判据）。"""

from datetime import date
from typing import cast

import pytest

from zhixing_quant.domain.calendar import TradingCalendar

# 2024-01-02..2024-01-05 开市，1/6-1/7 周末，1/8 开市
DAYS = [date(2024, 1, d) for d in (2, 3, 4, 5, 8, 9)]
CAL = TradingCalendar(DAYS)


def test_trading_day_membership() -> None:
    assert CAL.is_trading_day(date(2024, 1, 4))
    assert not CAL.is_trading_day(date(2024, 1, 6))


def test_unsorted_and_duplicated_input_is_normalised() -> None:
    messy = TradingCalendar([*reversed(DAYS), DAYS[0], DAYS[-1]])
    assert list(messy.days) == DAYS


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2024, 1, 3), date(2024, 1, 4), 2),
        (date(2024, 1, 6), date(2024, 1, 7), 0),  # 整段落在周末
        (date(2024, 1, 1), date(2024, 1, 31), len(DAYS)),
        (date(2024, 1, 4), date(2024, 1, 4), 1),  # 闭区间
    ],
)
def test_count_between_is_inclusive(start: date, end: date, expected: int) -> None:
    assert CAL.count_between(start, end) == expected


def test_neighbours_clamp_outside_the_range() -> None:
    assert CAL.prev_trading_day(date(2024, 1, 2)) is None
    assert CAL.next_trading_day(date(2024, 1, 9)) is None
    assert CAL.prev_trading_day(date(2024, 1, 8)) == date(2024, 1, 5)
    assert CAL.next_trading_day(date(2024, 1, 5)) == date(2024, 1, 8)
    assert CAL.next_trading_day(date(2024, 1, 6)) == date(2024, 1, 8)


def test_missing_days_only_looks_at_the_observed_window() -> None:
    """日历尾部还没到的日子不算缺：否则每次增量抓取都报一屏假缺失。"""
    observed = [date(2024, 1, 2), date(2024, 1, 4)]
    assert list(CAL.missing_days(observed)) == [date(2024, 1, 3)]
    assert list(CAL.missing_days([date(2024, 1, 2), date(2024, 1, 9)])) == [
        date(2024, 1, 3),
        date(2024, 1, 4),
        date(2024, 1, 5),
        date(2024, 1, 8),
    ]
    assert CAL.missing_days([]) == ()


def test_nth_trading_day_onward_slides_forward_when_not_open() -> None:
    assert CAL.nth_trading_day_onward(date(2024, 1, 3), 1) == date(2024, 1, 3)
    assert CAL.nth_trading_day_onward(date(2024, 1, 6), 1) == date(2024, 1, 8)
    assert CAL.nth_trading_day_onward(date(2024, 1, 6), 2) == date(2024, 1, 9)
    assert CAL.nth_trading_day_onward(date(2024, 1, 11), 2) is None
    with pytest.raises(ValueError, match="n 从 1 起算"):
        CAL.nth_trading_day_onward(date(2024, 1, 2), 0)


def test_days_view_is_not_writable() -> None:
    """日历被下游改掉一天，R006 就开始整批误判 FATAL。"""
    days = CAL.days
    with pytest.raises(AttributeError):
        cast("list[date]", days).append(date(2024, 2, 1))
