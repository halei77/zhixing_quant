"""一笔委托的成交与拒单（ADR-0010 决定 3、决定 4；03-4.4 的"不产生成交"半边）。

五个拒单原因各测一例，再加一条**判序**测试：同一根K线可以同时缺好几样（封死 + 零成交），
报告只该记一种，而"记哪种"是口径不是随手写的顺序。成交价与费用全部手工核算，
`03 §二 L1` 说的"不许用自己的函数验证自己"在这里就是要数字。
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
    Reject,
    attempt,
)
from zhixing_quant.backtest.limits import PriceBand
from zhixing_quant.backtest.spec import Cost, Side
from zhixing_quant.domain.bar import Bar

WHEN = datetime(2026, 9, 18, 10, 30)
#: 凑整过的费率，好一眼核出手工算的那三位数。真实数字在 `config/backtest.toml`，
#: 由 `test_backtest_spec.py` 钉住——两边各测各的，改哪边都会红一处。
COST = Cost(
    commission_pct=0.03,
    commission_min=5.0,
    stamp_pct=0.05,
    transfer_pct=0.001,
    slippage_pct=0.1,
)
PARTICIPATION = 5.0
BAND = PriceBand.bound(10.0, 95.0)


def normal_bar(**overrides: object) -> Bar:
    kwargs: dict[str, object] = {
        "open_": 100.0,
        "high": 101.0,
        "low": 99.5,
        "close": 100.5,
        "volume": 6000.0,
    }
    kwargs.update(overrides)
    return minute_span(WHEN, **kwargs)  # type: ignore[arg-type]


def order(side: Side = "buy", shares: int = 300) -> Order:
    return Order(code="600519", side=side, shares=shares, signal_at=(date(2026, 9, 18), WHEN))


def try_it(order: Order, bar: Bar | None, band: PriceBand = BAND) -> Fill | Reject:
    return attempt(order, bar, band, cost=COST, max_participation_pct=PARTICIPATION)


def test_a_buy_pays_the_open_plus_slippage_and_the_two_sided_fees() -> None:
    """买：成交价 100×1.001=100.10，成交额 30030，佣金 9.009 + 过户费 0.3003，印花税不收。"""
    filled = try_it(order("buy"), normal_bar())
    assert isinstance(filled, Fill)
    assert filled.price == pytest.approx(100.10)
    assert filled.notional == pytest.approx(30030.0)
    assert filled.fee == pytest.approx(9.3093)


def test_a_sell_pays_the_stamp_tax_that_a_buy_never_sees() -> None:
    """卖：成交价 100×0.999=99.90，成交额 29970，佣金 8.991 + 过户费 0.2997 + 印花税 14.985。"""
    filled = try_it(order("sell"), normal_bar())
    assert isinstance(filled, Fill)
    assert filled.price == pytest.approx(99.90)
    assert filled.fee == pytest.approx(24.2757)


def test_a_missing_bar_is_not_a_free_pass_through() -> None:
    """没有那一根就是没成交，不能当成"价格没变，算它成了"。"""
    rejected = try_it(order(), None)
    assert isinstance(rejected, Reject)
    assert rejected.reason == REASON_NO_BAR


def test_an_unjudgeable_board_refuses_the_order_and_says_what_is_missing() -> None:
    """判不出涨跌停边界时拒单（04 的 fail-closed 一路）。放行=高估可成交性。"""
    band = PriceBand.unknown("认不出代码 920001 的板块")
    rejected = try_it(order(), normal_bar(), band)
    assert isinstance(rejected, Reject)
    assert rejected.reason == REASON_BAND_UNKNOWN
    assert "920001" in rejected.detail


def test_a_locked_limit_up_bar_cannot_be_bought_but_can_be_sold() -> None:
    """一字涨停：买不到，卖得掉（板上排满的是卖单，对手盘不缺）。

    03-4.4 那句"能在跌停板上买入的回测引擎是骗子引擎"按**方向**读，不按"跌停"这个状态读：
    一字跌停时买入是现实中最好成交的一笔，把它也拒掉等于替策略判死（ADR-0010 决定 4）。
    """
    sealed = PriceBand.bound(10.0, 110.0 / 1.10)
    bought = try_it(
        order("buy"), normal_bar(open_=110.0, high=110.0, low=110.0, close=110.0), sealed
    )
    assert isinstance(bought, Reject)
    assert bought.reason == REASON_LOCKED
    sold = try_it(
        order("sell"), normal_bar(open_=110.0, high=110.0, low=110.0, close=110.0), sealed
    )
    assert isinstance(sold, Fill)


def test_a_locked_limit_down_bar_cannot_be_sold_but_can_be_bought() -> None:
    band = PriceBand.bound(10.0, 90.0 / 0.90)
    sold = try_it(order("sell"), normal_bar(open_=90.0, high=90.0, low=90.0, close=90.0), band)
    assert isinstance(sold, Reject)
    assert sold.reason == REASON_LOCKED
    bought = try_it(order("buy"), normal_bar(open_=90.0, high=90.0, low=90.0, close=90.0), band)
    assert isinstance(bought, Fill)


def test_a_zero_volume_bar_has_nothing_to_trade_against() -> None:
    rejected = try_it(order(), normal_bar(volume=0.0))
    assert isinstance(rejected, Reject)
    assert rejected.reason == REASON_ZERO_VOLUME


def test_an_order_that_would_move_the_price_is_not_filled_at_the_old_price() -> None:
    """吃掉该根 5% 以上的量还按开盘价成交，是给回测印钱。"""
    rejected = try_it(order(shares=301), normal_bar(volume=6000.0))
    assert isinstance(rejected, Reject)
    assert rejected.reason == REASON_PARTICIPATION
    assert "301" in rejected.detail


def test_the_participation_cap_is_a_cap_not_a_wall() -> None:
    """刚好压在 300 股上限上算能成交：`>` 写成 `>=` 会让正常单天天被拒。"""
    filled = try_it(order(shares=300), normal_bar(volume=6000.0))
    assert isinstance(filled, Fill)


def test_slippage_cannot_push_a_buy_through_the_board_price() -> None:
    """开在 109.99 的涨停板上加千分之一滑点会到 110.10，而 110.00 是法规边界。"""
    band = PriceBand.bound(10.0, 100.0)
    filled = try_it(
        order("buy"), normal_bar(open_=109.99, high=110.0, low=109.0, close=109.5), band
    )
    assert isinstance(filled, Fill)
    assert filled.price == pytest.approx(110.0)


def test_the_institution_is_judged_before_the_liquidity() -> None:
    """封死又零成交的一根：记"封板"。两个都记，"今天为什么没成交"就有了两种算法。"""
    sealed = PriceBand.bound(10.0, 100.0)
    bar = normal_bar(open_=110.0, high=110.0, low=110.0, close=110.0, volume=0.0)
    rejected = try_it(order("buy"), bar, sealed)
    assert isinstance(rejected, Reject)
    assert rejected.reason == REASON_LOCKED


def test_the_board_is_judged_before_the_shape_of_the_bar() -> None:
    """判不出板时连"有没有量"都不必问：那条单子的命运已经定了。"""
    rejected = try_it(order(), normal_bar(volume=0.0), PriceBand.unknown("缺主数据"))
    assert isinstance(rejected, Reject)
    assert rejected.reason == REASON_BAND_UNKNOWN


def test_the_same_inputs_give_the_same_answer() -> None:
    """03-4.1 的最小单位：`attempt` 里没有时钟、没有随机、没有盘。"""
    assert try_it(order(), normal_bar()) == try_it(order(), normal_bar())


@pytest.mark.parametrize("shares", [0, -300])
def test_an_order_for_nothing_is_a_bug_not_a_trade(shares: int) -> None:
    with pytest.raises(ValueError, match="股"):
        Order(code="600519", side="buy", shares=shares, signal_at=(WHEN.date(), WHEN))
