"""证券主数据的 point-in-time 查询（01 Step 1 验收 4；03 §二 L4.6）。

这里测的是最要命的一类错误：不报错、只是答得偏了——用今天的股票池回测历史，
幸存者偏差让结果虚高，而没有任何一处代码会红。
"""

from datetime import date

import pytest

from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import (
    Interval,
    Listing,
    MasterConflict,
    Rename,
    SecurityMaster,
)
from zhixing_quant.domain.symbol import Board

DAYS = [date(2024, 1, d) for d in (2, 3, 4, 5, 8, 9, 10, 11)]
CAL = TradingCalendar(DAYS)

ALIVE = Listing("600519", "贵州茅台", date(1999, 1, 1))
DEAD = Listing("000001", "早逝股份", date(2020, 1, 2), date(2024, 1, 5))
NEWBIE = Listing("300750", "宁德时代", date(2024, 1, 3))
STATTER = Listing("002415", "波动科技", date(2021, 1, 1))

MASTER = SecurityMaster(
    listings=[ALIVE, DEAD, NEWBIE, STATTER],
    st_periods=[Interval("002415", date(2022, 1, 1), date(2022, 6, 30))],
    suspensions=[Interval("000001", date(2024, 1, 4), date(2024, 1, 5))],
    renames=[Rename("002415", date(2022, 7, 1), "稳定科技")],
)


def test_universe_on_includes_later_delisted_names() -> None:
    """01 验收 4：查历史任意时点的全市场清单，含"当时在册、后退市"的那些。"""
    assert "000001" in MASTER.universe_on(date(2024, 1, 4))
    assert "000001" not in MASTER.universe_on(date(2024, 1, 8))
    assert set(MASTER.universe_on(date(1998, 12, 31))) == set()  # 一票未上市
    assert set(MASTER.universe_on(date(2024, 1, 10))) == {"600519", "002415", "300750"}


def test_delisting_day_still_counts_as_listed() -> None:
    state = MASTER.state_on("000001", date(2024, 1, 5))
    assert state.is_listed and state.is_suspended


@pytest.mark.parametrize(
    ("on", "is_st", "name"),
    [
        (date(2021, 6, 1), False, "波动科技"),
        (date(2022, 1, 1), True, "波动科技"),
        (date(2022, 6, 30), True, "波动科技"),
        (date(2022, 7, 1), False, "稳定科技"),
    ],
)
def test_st_and_name_are_read_as_of_the_date(on: date, is_st: bool, name: str) -> None:
    """ST 摘帽与更名是两件事：只用其一都会把某半年判成别的票。"""
    state = MASTER.state_on("002415", on)
    assert (state.is_st, state.name) == (is_st, name)


def test_board_comes_from_the_code_not_the_record() -> None:
    assert MASTER.state_on("300750", date(2024, 1, 5)).board is Board.GEM


def test_listing_day_index_counts_trading_days_only() -> None:
    assert MASTER.listing_day_index("300750", date(2024, 1, 3), CAL) == 1
    assert MASTER.listing_day_index("300750", date(2024, 1, 8), CAL) == 4
    assert MASTER.listing_day_index("300750", date(2024, 1, 6), CAL) is None  # 非交易日
    assert MASTER.listing_day_index("300750", date(2024, 1, 2), CAL) is None  # 还没上市


def test_a_truncated_calendar_does_not_make_an_old_stock_look_new() -> None:
    """增量抓取通常只装最近几十天的日历：老票在这份日历上"数得出 3 个交易日"。

    照实返回就会被当成新股，R004 的涨跌幅检查被静默豁免——门禁最坏的一种失效，
    因为日报上它长得像正常放行。数不出来就返回 None，让规则按"不在豁免窗口"处理。
    """
    assert MASTER.listing_day_index("600519", date(2024, 1, 3), CAL) is None
    assert not MASTER.no_limit_period("600519", date(2024, 1, 3), CAL, trading_days=5)
    # 日历真覆盖到上市日时常数得出来：这条不是"一律返回 None"。
    since_listing = TradingCalendar([date(1999, 1, 1), date(1999, 1, 4), date(2024, 1, 3)])
    assert MASTER.listing_day_index("600519", date(2024, 1, 3), since_listing) == 3


@pytest.mark.parametrize(
    ("on", "expected"),
    [(date(2024, 1, 3), True), (date(2024, 1, 9), True), (date(2024, 1, 10), False)],
)
def test_no_limit_period_window_is_configurable_not_hardcoded(on: date, expected: bool) -> None:
    assert MASTER.no_limit_period("300750", on, CAL, trading_days=5) is expected
    assert MASTER.no_limit_period("300750", date(2024, 1, 5), CAL, trading_days=2) is False


def test_unknown_code_raises_instead_of_returning_none() -> None:
    """主数据与股票池不同源时，返回 None 会让门禁以为"不是 ST"从而用错阈值。"""
    with pytest.raises(KeyError, match="主数据无"):
        MASTER.listing("601999")


def test_listing_code_is_normalized() -> None:
    assert Listing("sh600519", "x", date(2024, 1, 2)).code == "600519"


def test_duplicate_listing_is_rejected() -> None:
    with pytest.raises(MasterConflict, match="重复登记"):
        SecurityMaster(listings=[ALIVE, Listing("600519", "重号", date(2000, 1, 1))])


def test_overlapping_st_periods_are_rejected_at_load() -> None:
    """重叠区间会让同一时点同时答"是 ST"与"不是 ST"，装载时就该拒绝。"""
    with pytest.raises(MasterConflict, match="ST 002415 区间重叠"):
        SecurityMaster(
            listings=[STATTER],
            st_periods=[
                Interval("002415", date(2022, 1, 1), date(2022, 6, 30)),
                Interval("002415", date(2022, 6, 1), date(2022, 12, 1)),
            ],
        )


def test_open_ended_period_blocks_any_later_one() -> None:
    with pytest.raises(MasterConflict, match="区间重叠"):
        SecurityMaster(
            listings=[ALIVE],
            suspensions=[
                Interval("600519", date(2022, 1, 1)),
                Interval("600519", date(2023, 1, 1), date(2023, 2, 1)),
            ],
        )


def test_delisted_before_listed_is_rejected() -> None:
    with pytest.raises(MasterConflict, match="退市日早于上市日"):
        Listing("600519", "时序倒了", date(2024, 1, 5), date(2023, 1, 5))


def test_two_codes_may_share_one_interval_kind() -> None:
    """重叠检查按代码分组：跨代码同一天停牌是常态，不该被当成冲突。"""
    ok = SecurityMaster(
        listings=[ALIVE, STATTER],
        st_periods=[Interval("600519", date(2022, 1, 1)), Interval("002415", date(2022, 1, 1))],
    )
    assert ok.st_on("600519", date(2023, 1, 1)) and ok.st_on("002415", date(2023, 1, 1))
