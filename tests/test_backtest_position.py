"""底仓台账与回合配对的单元测试（ADR-0010 决定 5、决定 6；07 §七 验收 3、验收 5）。

这里测的是"账户允不允许"与"配出来的回合长什么样"，与 `test_backtest_fills.py` 那半
（市场允不允许）分得开：一笔卖单可以市场那头完全成交得了、账户这头却不成立。两类拒单
混成一个数的话，报告就答不出"今天这笔 T 差在哪儿"。
"""

from datetime import date, datetime

import pytest

from tests.fakes import minute_span
from zhixing_quant.backtest.fills import (
    REASON_BAND_UNKNOWN,
    REASON_LOCKED,
    REASON_NO_BAR,
    REASON_PARTICIPATION,
    REASON_ZERO_VOLUME,
    Fill,
    Order,
)
from zhixing_quant.backtest.position import REASON_T1, CodeBook, Ledger, RoundTrip
from zhixing_quant.backtest.spec import Side, Sizing
from zhixing_quant.domain.bar import Bar

DAY1 = date(2026, 9, 17)
DAY2 = date(2026, 9, 18)
T0935 = datetime(2026, 9, 17, 9, 35)
T0940 = datetime(2026, 9, 17, 9, 40)
T1005 = datetime(2026, 9, 18, 10, 5)
SIZING = Sizing(lot_size=100, base_lots=3)


def quote(when: datetime, price: float, *, symbol: str = "600519", volume: float = 1e6) -> Bar:
    return minute_span(
        when, open_=price, high=price, low=price, close=price, volume=volume, symbol=symbol
    )


def fill(
    side: Side,
    when: datetime,
    price: float,
    *,
    shares: int = 300,
    fee: float = 8.0,
    symbol: str = "600519",
) -> Fill:
    """一笔成交。`fee` 直接给，不在这儿重算成本——那半边有别的测试。"""
    return Fill(
        order=Order(
            code=symbol, side=side, shares=shares, signal_at=(when.date(), when), strategy="t"
        ),
        bar=quote(when, price, symbol=symbol),
        price=price,
        fee=fee,
    )


def new_book(price: float = 10.0) -> CodeBook:
    """期初 300 股底仓、按 10.0 入账的一只票。"""
    return CodeBook(
        code="600519",
        lot_size=SIZING.lot_size,
        base_shares=SIZING.base_shares,
        base_cost=SIZING.base_shares * price,
        held=SIZING.base_shares,
        day_start_held=SIZING.base_shares,
        sold_today=0,
        cash=0.0,
        last_price=price,
        opened_on=DAY1,
    )


def test_t_plus_one_the_day_start_position_is_all_you_can_sell() -> None:
    """期初 300 股：卖掉 300 之后今天一分不能再卖，买回来的那 300 不增加额度。

    这就是 07 §七 验收 3 的两个答案：无底仓不能卖、当日买入不能当日卖。
    """
    day = new_book()
    assert day.sellable == 300
    day.apply(fill("sell", T0935, 11.0))
    assert day.sellable == 0
    day.apply(fill("buy", T0940, 10.5))
    assert day.sellable == 0  # 今天买回来的不能今天卖
    assert day.held == 300  # 仓位还原了，额度没还原


def test_buy_first_then_sell_is_legal_because_the_sell_comes_out_of_the_base() -> None:
    """先买后卖的 T入→T出 合法：卖掉的是底仓那 300 股，交易所只限制"当日买入的不可卖"。"""
    day = new_book()
    day.apply(fill("buy", T0935, 10.0))
    assert day.held == 600
    assert day.sellable == 300  # 不是 600
    day.apply(fill("sell", T0940, 10.6))
    assert day.held == 300


def test_the_allowance_rolls_over_at_the_close() -> None:
    """收盘结算把额度翻篇：今天的期初持仓就是明天能卖的额度。"""
    day = new_book()
    day.apply(fill("sell", T0935, 11.0))
    assert day.sellable == 0
    event = day.close_day(DAY1)
    assert event is not None
    assert (event.sells, event.buys) == (1, 0)
    assert day.sellable == 0  # 收盘时持仓还是 0，明天照样卖不出——接回来才有额度
    day.apply(fill("buy", T1005, 10.0))
    day.close_day(DAY1)
    assert day.sellable == 300


def test_a_round_trip_is_the_matched_shares_minus_both_legs_fees() -> None:
    """手工核算（07 §七 验收 5）：卖 11.0 配买 10.0，300 股，两笔费用 6+8 → 292.0。"""
    day = new_book()
    assert day.apply(fill("sell", T0935, 11.0, fee=6.0)) == ()  # 卖出时没东西可配，先欠着
    trips = day.apply(fill("buy", T0940, 10.0, fee=8.0))
    assert len(trips) == 1
    trip = trips[0]
    assert trip.shares == 300
    assert (trip.buy_price, trip.sell_price) == (10.0, 11.0)
    assert trip.fee == pytest.approx(14.0)
    assert trip.pnl == pytest.approx((11.0 - 10.0) * 300 - 14.0)
    assert trip.same_day


def test_pairing_is_fifo_and_allows_the_next_day() -> None:
    """跨日配对：今天的买入吃的是昨天那笔卖出，先欠的先还。

    按日切断就把亏损藏起来了（决定 6）——昨天飞掉的那笔今天接回来，是一个亏的回合，
    不是"昨天一笔 T飞 + 今天一笔白买"。
    """
    day = new_book()
    day.apply(fill("sell", T0935, 11.0, fee=5.0))
    day.apply(fill("sell", T1005, 12.0, fee=5.0))
    assert len(day.owed) == 2
    trips = day.apply(fill("buy", T1005, 11.5, fee=5.0))
    assert len(trips) == 1
    assert trips[0].sell_price == 11.0  # FIFO：吃掉的是早那笔
    assert not trips[0].same_day


def test_one_fill_can_close_several_legs() -> None:
    """一笔 900 股的买入还原两笔欠仓：回合按股数配，一腿拆两截不算意外。"""
    day = new_book()
    day.apply(fill("sell", T0935, 10.5, shares=300))
    day.apply(fill("sell", T0940, 11.5, shares=600))
    trips = day.apply(fill("buy", T1005, 11.0, shares=900))
    assert [(t.shares, t.sell_price) for t in trips] == [(300, 10.5), (600, 11.5)]
    assert len(day.owed) == 0


def test_a_matched_pair_counts_as_neither_a_fly_nor_an_addition() -> None:
    """先买后卖、当天配平：既不是 T飞 也不是加仓，两笔都只是回合的一半。"""
    day = new_book()
    day.apply(fill("buy", T0935, 10.0))
    day.apply(fill("sell", T0940, 10.2))
    event = day.close_day(DAY1)
    assert event is not None
    assert (event.buys, event.sells) == (1, 1)
    assert (event.added, event.flew) == (0, 0)


def test_a_sell_left_unmatched_at_the_close_is_a_fly() -> None:
    day = new_book()
    day.apply(fill("sell", T0935, 11.0))
    day.apply(fill("buy", T0940, 10.0, shares=100))  # 只还原了三分之一
    event = day.close_day(DAY1)
    assert event is not None
    assert (event.flew, event.added) == (1, 0)
    assert day.owed[0].shares == 200


def test_a_buy_left_unmatched_at_the_close_is_an_addition() -> None:
    day = new_book()
    day.apply(fill("buy", T0935, 10.0))
    event = day.close_day(DAY1)
    assert event is not None
    assert (event.added, event.flew) == (1, 0)


def test_yesterdays_fly_is_not_counted_again_today() -> None:
    """事件计数的分母是"那天的成交"：一笔昨天的欠仓今天还没还，只算昨天飞过。

    不然一笔飞掉没接回的 T 会被数成一整串的 T飞，T飞率就成了持有天数的函数。
    """
    day = new_book()
    day.apply(fill("sell", T0935, 11.0))
    assert day.close_day(DAY1) is not None
    assert day.close_day(DAY2) is None  # 今天一笔没做


def test_a_day_with_no_activity_reports_nothing_rather_than_zeros() -> None:
    """没做就没有这一行，不是"0 飞 0 加"（04 §四：没做和做完不能长一样）。"""
    assert new_book().close_day(DAY1) is None


def test_the_ledger_values_the_base_at_the_first_bar_once_per_code() -> None:
    """底仓按这只票第一根的开盘价入账：是个假设不是事实，所以报告要印它。"""
    ledger = Ledger(sizing=SIZING)
    opened = ledger.open(quote(T0935, 10.0))
    assert (opened.base_shares, opened.base_cost) == (300, 3000.0)
    assert ledger.open(quote(T0940, 999.0)) is opened  # 同一只票不重立底仓


def test_cash_and_holding_track_every_fill() -> None:
    """现金：买付钱、卖收钱，两笔都扣费。持仓：买加卖减。"""
    day = new_book()
    day.apply(fill("sell", T0935, 11.0, fee=6.0))
    assert day.cash == pytest.approx(11.0 * 300 - 6.0)
    assert day.held == 0
    day.apply(fill("buy", T0940, 10.0, fee=5.0))
    assert day.cash == pytest.approx(11.0 * 300 - 6.0 - (10.0 * 300 + 5.0))
    assert day.held == 300


def test_closing_reports_the_open_lots_and_their_unrealized_result() -> None:
    """报告那句"还有 N 手未平仓、浮盈亏 X、不在胜率分母里"的出处。"""
    day = new_book()
    day.apply(fill("buy", T0935, 10.0, fee=5.0))
    day.last_price = 10.5
    closing = day.closing()
    assert (closing.open_shares, closing.held) == (300, 600)
    assert closing.unrealized == pytest.approx((10.5 - 10.0) * 300 - 5.0)
    assert closing.base_pnl == pytest.approx((10.5 - 10.0) * 300)  # 底仓那截是 beta，单列


def test_an_owed_leg_makes_money_when_the_price_falls() -> None:
    """欠仓（卖了没买回）的浮盈亏方向与加仓相反：价格跌下来才是还原得划算。"""
    day = new_book()
    day.apply(fill("sell", T0935, 11.0, fee=5.0))
    day.last_price = 10.0
    assert day.closing().unrealized == pytest.approx((11.0 - 10.0) * 300 - 5.0)


def test_the_t_plus_one_reason_does_not_collide_with_the_markets_five() -> None:
    """报告按原因分列各数，撞名就会把两类不同的"没做成"合成一个数。"""
    market = {
        REASON_NO_BAR,
        REASON_BAND_UNKNOWN,
        REASON_LOCKED,
        REASON_ZERO_VOLUME,
        REASON_PARTICIPATION,
    }
    assert REASON_T1 not in market
    assert len(market) == 5


def test_round_trip_direction_is_set_by_who_came_first_not_by_the_plan() -> None:
    """07 §二：T出/T入 由策略判定。回合式子只有一个，方向由配对顺序决定。"""
    day = new_book()
    day.apply(fill("buy", T0935, 10.0, fee=5.0))
    trips = day.apply(fill("sell", T0940, 10.2, fee=5.0))
    assert isinstance(trips[0], RoundTrip)
    assert (trips[0].buy_at[1], trips[0].sell_at[1]) == (T0935, T0940)
    assert trips[0].pnl == pytest.approx((10.2 - 10.0) * 300 - 10.0)
