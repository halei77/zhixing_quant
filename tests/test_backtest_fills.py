"""一笔委托的成交现实（ADR-0010 决定 3、决定 4；07 §5.2；03-4.4）。

四个"不可成交"与判序都在这里逐条手工核算：`03 §二 L1` 说的"不许用自己的函数验证自己"，在这里
就是要数字——成交价、佣金、印花税每一笔都按得出来。费率取 `config/backtest.toml` 的当期标准而不是
凑整的假数：最低 5 元那条佣金只有在 300 股的小单上才咬得住，把费率凑成 0.03% 反而看不清"最低额"
在做什么。配置文件里的真数由 `test_backtest_spec.py` 钉住，两边各测各的，改哪边都红一处。

判序（制度先于流动性、板判不出先于一切）也单独测：同一根K线可以同时缺两样，报告只记一种，
记哪种决定用户看到的拒单原因分布——那是口径，不是随手写的 `if` 顺序。
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
    Outcome,
    Reject,
    attempt,
    blocks,
    fill_price,
)
from zhixing_quant.backtest.limits import PriceBand, clamp_fill, locked, one_price
from zhixing_quant.backtest.spec import Cost, Side
from zhixing_quant.domain.bar import Bar

MORNING = datetime(2026, 9, 18, 9, 35)
#: 主板 10% 档、昨收 100 → 板价 90/110，整数好核对
BAND = PriceBand.bound(10.0, 100.0)
COST = Cost(
    commission_pct=0.025, commission_min=5.0, stamp_pct=0.05, transfer_pct=0.001, slippage_pct=0.02
)
PARTICIPATION = 5.0


def order(code: str = "600519", side: Side = "buy", shares: int = 300) -> Order:
    return Order(code=code, side=side, shares=shares, signal_at=(MORNING.date(), MORNING))


def quote(
    price: float = 100.0,
    *,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1e6,
) -> Bar:
    """一根正常的K线：开收同价，高低各让 1%。

    高低默认**跟着价格走**而不是写死 101/99：写死了就在 10 元的票上造出一根 OHLC 自相矛盾的
    K线，`Bar` 契约当场拒掉，测试报的是 pydantic 错而不是判据错——那种失败长得像代码坏了。
    """
    return minute_span(
        MORNING,
        open_=price,
        high=price * 1.01 if high is None else high,
        low=price * 0.99 if low is None else low,
        close=price,
        volume=volume,
    )


def flat(price: float, *, volume: float = 1e6, when: datetime = MORNING) -> Bar:
    """一根一字的K线：四个价全等。"""
    return minute_span(when, open_=price, high=price, low=price, close=price, volume=volume)


def settle(o: Order, b: Bar | None, band: PriceBand = BAND) -> Outcome:
    return attempt(
        o, b, band, cost=COST, max_participation_pct=PARTICIPATION
    )  # 与引擎调用同一形状，测出来的判序才是引擎那条判序


def test_a_normal_buy_pays_the_slippage_and_both_way_fees() -> None:
    """常规买：100.00 加 0.02% 滑点 = 100.02，费用 = 佣金 5 元（最低额）+ 过户费。

    手工核一遍（07 §七 验收 5 要的就是能手工核）：
    佣金 max(100.02*300*0.00025, 5) = max(7.5015, 5) = 7.5015；过户费 30006*0.00001 = 0.30006；
    买方无印花税。合计 7.80156。
    """
    outcome = settle(order(), quote(100.0), BAND)
    assert isinstance(outcome, Fill)
    assert outcome.price == pytest.approx(100.02)
    assert outcome.fee == pytest.approx(7.80156)
    assert outcome.notional == pytest.approx(30006.0)


def test_a_sell_pays_the_stamp_duty_the_buy_does_not() -> None:
    outcome = settle(order(side="sell"), quote(100.0), BAND)
    assert isinstance(outcome, Fill)
    assert outcome.price == pytest.approx(99.98)  # 卖往不利方向让
    # 佣金 7.4985 + 过户费 0.29994 + 印花税 14.997 = 22.79544
    assert outcome.fee == pytest.approx(22.79544)


def test_the_minimum_commission_binds_on_small_orders() -> None:
    """300 股 10 元 = 3000 元，按 0.025% 只有 0.75 元，收的是 5 元最低额。

    免掉这条，做T 的小单成本会被系统性低估——而 07 §二 说测试单位恰恰全是小单。
    """
    outcome = settle(order(), quote(10.0), PriceBand.bound(10.0, 10.0))
    assert isinstance(outcome, Fill)
    # 价 10.002，名义 3000.6：佣金 0.75015 → 5.0，过户费 0.030006 → 合计 5.030006
    assert outcome.fee == pytest.approx(5.030006)


def test_no_bar_means_no_fill_not_no_board() -> None:
    """停牌/断档那一根根本没有K线：拒单原因是 `no_bar`，不是"板不存在"。

    两者弄混的样子是"今天判不了板 → 当没有板 → 照常成交"，那正是 ADR-0010 决定 4
    用三种状态三份话要挡住的事。
    """
    outcome = settle(order(), None, BAND)
    assert isinstance(outcome, Reject)
    assert outcome.reason == REASON_NO_BAR


def test_an_unjudgeable_band_rejects_and_carries_the_reason() -> None:
    outcome = settle(order(), quote(100.0), PriceBand.unknown("主数据没有这只票"))
    assert isinstance(outcome, Reject)
    assert outcome.reason == REASON_BAND_UNKNOWN
    assert "主数据没有这只票" in outcome.detail


def test_an_unlimited_band_is_not_an_unjudgeable_one() -> None:
    """新股无涨跌幅窗口是"判出来了：没有限制"，所以它不拒单（拒它是决定 4 的第三条例外）。"""
    outcome = settle(order(), quote(100.0), PriceBand.unlimited("新股首日"))
    assert isinstance(outcome, Fill)


def test_a_locked_limit_up_cannot_be_bought_but_can_be_sold() -> None:
    """03-4.4 构造的两个样本之一：一字涨停买不到，但卖出在板上排着队，能成。

    反方向也拒的话等于替策略判死，答错的正是 03-4.4 要防的那个谎。
    """
    sealed = flat(110.0)
    assert locked(sealed, BAND) == "up"
    rejected = settle(order(side="buy"), sealed, BAND)
    assert isinstance(rejected, Reject)
    assert rejected.reason == REASON_LOCKED
    assert isinstance(settle(order(side="sell"), sealed, BAND), Fill)


def test_a_locked_limit_down_cannot_be_sold_but_can_be_bought() -> None:
    sealed = flat(90.0)
    assert locked(sealed, BAND) == "down"
    rejected = settle(order(side="sell"), sealed, BAND)
    assert isinstance(rejected, Reject)
    assert rejected.reason == REASON_LOCKED
    assert isinstance(settle(order(side="buy"), sealed, BAND), Fill)


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_a_flat_bar_in_the_middle_of_the_band_is_not_a_board(side: Side) -> None:
    """一字但没到板（涨 3%）：只是这根没波动，不是封死。"""
    sealed = flat(103.0)
    assert locked(sealed, BAND) is None
    assert isinstance(settle(order(side=side), sealed, BAND), Fill)


def test_the_institution_is_judged_before_the_liquidity() -> None:
    """一根封死又零成交的K线两样都缺，报告只记制度那条（决定 4 的判序）。

    两个都记会让"今天为什么没成交"出现两种算法，而 §三 的拒收率公式立刻读不出来。
    """
    outcome = settle(order(side="buy"), flat(110.0, volume=0.0), BAND)
    assert isinstance(outcome, Reject)
    assert outcome.reason == REASON_LOCKED


def test_a_zero_volume_bar_cannot_be_filled() -> None:
    outcome = settle(order(), quote(100.0, volume=0.0), BAND)
    assert isinstance(outcome, Reject)
    assert outcome.reason == REASON_ZERO_VOLUME


def test_an_order_bigger_than_the_participation_cap_is_rejected() -> None:
    """该根成交 1e6 股，5% 上限 = 5e4：60000 股那一笔不可能不动价格。"""
    outcome = settle(order(shares=60_000), quote(100.0, volume=1e6), BAND)
    assert isinstance(outcome, Reject)
    assert outcome.reason == REASON_PARTICIPATION


def test_the_participation_cap_is_inclusive() -> None:
    """正好顶到上限（50000 = 1e6 × 5%）判成交：规则写的是"超过"，不是"达到"。"""
    outcome = settle(order(shares=50_000), quote(100.0, volume=1e6), BAND)
    assert isinstance(outcome, Fill)


def test_slippage_never_pushes_a_fill_past_the_board() -> None:
    """开在涨停价上再往不利方向让滑点，会被压回 110.0：板价是法规，不是偏好。"""
    assert fill_price(quote(110.0), BAND, "buy", slippage_pct=0.02) == pytest.approx(110.0)
    assert fill_price(quote(90.0), BAND, "sell", slippage_pct=0.02) == pytest.approx(90.0)


def test_the_fill_price_is_not_clamped_into_the_bar_range() -> None:
    """一根振幅很小的K线：滑点让成交价落在 [low, high] 之外，这是有意的（决定 3）。

    夹进区间等于把滑点偷偷抹掉，而滑点是 07 §5.2 点名要建模的现实。
    """
    narrow = minute_span(MORNING, open_=100.0, high=100.01, low=99.99, close=100.0, volume=1e6)
    price = fill_price(narrow, BAND, "buy", slippage_pct=0.02)
    assert price > narrow.high


@pytest.mark.parametrize(
    ("side", "sealed", "expected"),
    [("buy", "up", True), ("sell", "up", False), ("buy", "down", False), ("sell", "down", True)],
)
def test_which_way_a_sealed_board_blocks(side: Side, sealed: str, expected: bool) -> None:
    assert blocks(side, sealed) is expected


def test_clamp_only_moves_the_side_that_has_a_board() -> None:
    assert clamp_fill(115.0, BAND, "buy") == pytest.approx(110.0)
    assert clamp_fill(85.0, BAND, "sell") == pytest.approx(90.0)
    assert clamp_fill(100.0, BAND, "buy") == pytest.approx(100.0)
    assert clamp_fill(100.0, PriceBand.unlimited("新股首日"), "buy") == pytest.approx(100.0)
    assert clamp_fill(100.0, PriceBand.unknown("缺主数据"), "sell") == pytest.approx(100.0)


def test_one_price_needs_all_four_prices_on_the_same_level() -> None:
    """`Bar` 契约已经把 open/close 夹在 high/low 之间，所以 high==low 就够了。"""
    assert one_price(flat(10.0))
    assert not one_price(quote(10.0, high=10.01, low=9.99))


def test_the_same_bar_and_band_give_the_same_outcome_twice() -> None:
    """03-4.1 的最小样本：判定本身不许有状态。跑两遍逐位相等，才谈得上整段回测逐位相等。"""
    first = settle(order(), quote(100.0), BAND)
    second = settle(order(), quote(100.0), BAND)
    assert isinstance(first, Fill) and isinstance(second, Fill)
    assert first.price == second.price and first.fee == second.fee


def test_a_zero_share_order_is_not_an_order() -> None:
    with pytest.raises(ValueError, match="0 或负数"):
        Order(code="600519", side="buy", shares=0, signal_at=(date(2026, 9, 18), MORNING))
