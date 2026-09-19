"""绩效指标的算术（07 §七 验收 5：这五个数要能手工核算，误差 < 1e-6）。

数字是**手填的**而不是跑出来的：回合盈亏在这里就是几个整数，谁都能拿计算器复核一遍
胜率、盈亏比、期望、连败。费用与成交价的算术在 `test_backtest_fills.py` 已经逐笔核过，
这里重跑一遍只会把两层错误混成一个对不上的数。

唯一跑引擎的那条是恒等式测试：`总盈亏 = 回合已实现 + 未还原浮盈亏 + 底仓 beta`。
它不在这里核算术，核的是"三块加回去等于账户"——账配不平，前面每一个指标都白算。

第二个重点是 `None` 的分布：分母为 0 时**不给 0.0**。0.0% 的胜率读起来像"测过了，很差"，
`None` 才是一句实话："没测出来"。这条与 04 §四 的三态规矩是同一件事。
"""

from datetime import date, datetime, timedelta

import pytest

from tests.fakes import Scripted, assumptions, flat_bands, minute_span, minutes
from zhixing_quant.backtest.engine import Result, run
from zhixing_quant.backtest.fills import Fill, Order
from zhixing_quant.backtest.metrics import measure
from zhixing_quant.backtest.position import Closing, DayEvent, Leg, RoundTrip
from zhixing_quant.backtest.spec import Side

DAY = date(2026, 9, 17)
MORNING = datetime(2026, 9, 17, 9, 35)
LATER = datetime(2026, 9, 17, 9, 40)
ASSUMPTIONS = assumptions()
EMPTY = Result(
    assumptions=ASSUMPTIONS,
    fills=(),
    rejects=(),
    round_trips=(),
    day_events=(),
    equity=(),
    closings=(),
)


def trip(pnl: float, code: str = "600519", minutes_: int = 0) -> RoundTrip:
    """一个盈亏恰好是 `pnl` 元的回合：100 股、零费用，买 10 元、卖 `10 + pnl/100` 元。

    把"回合盈亏"直接当参数而不是把价格当参数，是因为指标读的就是 `pnl`；写成价格反而要
    读者先心算一遍 `(卖−买)×100`，手工核算的判据就变成了测这个 helper 自己。
    """
    at: tuple[date, datetime] = (DAY, MORNING if minutes_ == 0 else LATER)
    return RoundTrip(
        code=code,
        buy_at=at,
        sell_at=at,
        shares=100,
        buy_price=10.0,
        sell_price=10.0 + pnl / 100.0,
        fee=0.0,
    )


def _fill(side: Side, code: str = "600519") -> Fill:
    bar = minute_span(MORNING, open_=10.0, high=10.0, low=10.0, close=10.0, volume=1e6)
    return Fill(
        order=Order(code=code, side=side, shares=100, signal_at=(DAY, MORNING)),
        bar=bar,
        price=10.0,
        fee=1.0,
    )


def _result(
    trips: tuple[RoundTrip, ...] = (),
    fills: tuple[Fill, ...] = (),
    events: tuple[DayEvent, ...] = (),
    closings: tuple[Closing, ...] = (),
) -> Result:
    return Result(
        assumptions=ASSUMPTIONS,
        fills=fills,
        rejects=(),
        round_trips=trips,
        day_events=events,
        equity=(),
        closings=closings,
    )


def test_win_rate_keeps_the_ties_in_the_denominator() -> None:
    """2 胜 1 负 1 平 → 胜率 2/4 = 50%，不是 2/3。把打平剔出分母是偷偷调高胜率。"""
    m = measure(_result(trips=(trip(100.0), trip(50.0), trip(-50.0), trip(0.0))))
    assert (m.trips, m.wins, m.losses, m.even) == (4, 2, 1, 1)
    assert m.win_rate == pytest.approx(0.5, abs=1e-12)
    assert m.trip_pnl == pytest.approx(100.0, abs=1e-9)


def test_payoff_ratio_compares_average_amounts_not_totals() -> None:
    """盈亏比 = 平均盈利 / 平均亏损。总盈 300（3 笔）÷ 总亏 100（1 笔）是 3，平均比是 1。

    这两个数都叫过"盈亏比"，取平均那个是因为总额之比把胜率偷渡进了赔率：赢三次小的是
    "手熟"，不是"赔率好"，而 07 §七 要分开看的就是这两件事。
    """
    m = measure(_result(trips=(trip(100.0), trip(100.0), trip(100.0), trip(-100.0))))
    assert m.payoff == pytest.approx(1.0, abs=1e-12)
    assert m.win_sum == pytest.approx(300.0, abs=1e-9)
    assert m.loss_sum == pytest.approx(-100.0, abs=1e-9)


def test_payoff_ratio_is_absent_when_there_is_nothing_to_compare() -> None:
    """没有亏损、或没有盈利，都算不出盈亏比。"零亏损"给 `None` 而不是无穷大。"""
    only_wins = measure(_result(trips=(trip(10.0),)))
    assert only_wins.payoff is None
    assert measure(_result(trips=(trip(-10.0),))).payoff is None
    assert measure(EMPTY).payoff is None


def test_expectancy_is_after_both_legs_of_cost() -> None:
    """期望 = 回合盈亏合计 ÷ 回合数。`pnl` 已扣两笔费用，所以它就是"扣完成本划不划算"。"""
    m = measure(_result(trips=(trip(30.0), trip(-10.0), trip(-5.0))))
    assert m.expectancy == pytest.approx(15.0 / 3.0, abs=1e-9)


def test_loss_streak_is_counted_over_completion_order_across_codes() -> None:
    """连败按**配上的先后**、跨票一起数：两只票各亏两笔，用户感受到的是四连败。

    打平那一笔（pnl == 0）把链子剪断——它不是亏损。写在数据里是 `0, -1, -1, 0, -1`：
    最长串是 2。
    """
    trips = (trip(0.0), trip(-1.0), trip(-1.0, code="000001"), trip(0.0), trip(-1.0))
    assert measure(_result(trips=trips)).max_loss_streak == 2


def test_streak_of_wins_is_not_a_losing_streak() -> None:
    assert measure(_result(trips=(trip(5.0), trip(5.0), trip(5.0)))).max_loss_streak == 0


def test_fly_rate_divides_by_the_fills_that_were_meant_as_t_out() -> None:
    """T飞率的分母是**成交**的卖出笔数（决定 6）。被拒的委托不配进分母：它没做成 T出。"""
    events = (DayEvent(code="600519", trade_date=DAY, sells=2, buys=1, flew=1, added=0),)
    m = measure(
        _result(
            events=events,
            fills=(_fill("sell"), _fill("sell"), _fill("buy")),
        )
    )
    assert (m.t_out_fills, m.t_in_fills, m.flew, m.added) == (2, 1, 1, 0)
    assert m.fly_rate == pytest.approx(0.5, abs=1e-12)
    assert m.add_rate == pytest.approx(0.0, abs=1e-12)  # 有 T入，所以这个 0 是算出来的 0


def test_rates_are_absent_rather_than_zero_when_nothing_was_traded() -> None:
    """一笔没成交：T飞率与加仓率都不存在。印 0.00% 会被读成"一次都没飞"。"""
    m = measure(EMPTY)
    assert m.fly_rate is None and m.add_rate is None
    assert m.win_rate is None and m.expectancy is None
    assert m.trips == 0 and m.max_loss_streak == 0


def test_final_pnl_adds_every_book_up_separately() -> None:
    """总盈亏 = Σ(现金 + 持仓×最后价 − 底仓成本)，一只票一截一截算完再加。

    茅台现金为负（买过没卖完），平安现金为正（卖过没接回）。两本的持仓都等于底仓手数×一手，
    也就是两本都是配平过的账——引擎造不出持仓与两截队列不自洽的 `Closing`，这里也不造。
    """
    a = _closing_with(code="600519", held=300, cash=-100.0, last=11.0, base_shares=300)
    b = _closing_with(code="000001", held=200, cash=500.0, last=9.5, base_shares=200)
    m = measure(_result(closings=(a, b)))
    assert m.final_pnl == pytest.approx(200.0 + 400.0, abs=1e-9)
    assert m.base_pnl == pytest.approx((11.0 - 10.0) * 300 + (9.5 - 10.0) * 200, abs=1e-9)
    assert m.unrealized == pytest.approx(0.0, abs=1e-12)  # 两截队列都空


def _closing_with(
    code: str,
    held: int,
    cash: float,
    last: float,
    base_shares: int,
    extra: tuple[Leg, ...] = (),
    owed: tuple[Leg, ...] = (),
) -> Closing:
    """一本期末账。底仓入账价一律 10 元——`base_cost` 就是 `base_shares × 10`。"""
    return Closing(
        code=code,
        held=held,
        cash=cash,
        last_price=last,
        base_shares=base_shares,
        base_cost=base_shares * 10.0,
        owed=owed,
        extra=extra,
    )


def test_the_money_decomposes_exactly() -> None:
    """`总盈亏 = 回合已实现 + 未还原浮盈亏 + 底仓 beta`，一条恒等式，报告第三节的依据。

    手工造一本配得平的账：底仓 300 股 @10 元（成本 3000）、期末价 11 → beta 300；再加一截没
    卖掉的加仓 100 股 @10.5、每股费用 0.01 → 浮盈 (11−10.5)×100 − 1 = 49；那 100 股连钱带费
    都付出去了，所以现金 −1051；回合 0 个。按定义 `−1051 + 400×11 − 3000 = 349`，按拆解
    `0 + 49 + 300 = 349`。两个数不等就是有一笔钱被算了两次或漏掉一次。
    """
    extra = (
        Leg(opened_on=DAY, opened_at=(DAY, MORNING), shares=100, price=10.5, fee_per_share=0.01),
    )
    book = _closing_with("600519", 400, -1051.0, 11.0, 300, extra=extra)
    m = measure(_result(closings=(book,)))
    assert m.trip_pnl == pytest.approx(0.0, abs=1e-12)  # 没有回合，所以那 349 里一分钱都不是手艺
    assert m.final_pnl == pytest.approx(349.0, abs=1e-9)
    assert m.final_pnl == pytest.approx(m.trip_pnl + m.unrealized + m.base_pnl, abs=1e-9)
    assert m.unrealized == pytest.approx((11.0 - 10.5) * 100 - 0.01 * 100, abs=1e-9)
    assert m.base_pnl == pytest.approx((11.0 - 10.0) * 300, abs=1e-9)


def test_the_identity_holds_on_a_real_run_with_a_half_open_leg() -> None:
    """真跑两天引擎：一个**跨日**回合 + 一笔收盘还没还原的加仓，恒等式照样配平。

    手写的那条测的是式子，这条测的是记账：`CodeBook.apply` 把配对与摊费用分散在两处，
    配不平就是有一笔钱被算了两次或者漏掉——那才是回测里最贵的 bug。
    """
    bars = minutes(DAY, [10.0, 10.2, 10.4]) + minutes(DAY + timedelta(days=1), [10.3, 10.5, 10.7])
    result = run(
        bars,
        strategy=Scripted({("600519", 0): "sell", ("600519", 3): "buy", ("600519", 4): "buy"}),
        assumptions=assumptions(),
        bands=flat_bands(),
    )
    m = measure(result)
    assert m.trips == 1  # 昨天卖的那截今天接回；第三笔买入没东西可配，挂着
    assert not result.round_trips[0].same_day
    assert m.final_pnl == pytest.approx(m.trip_pnl + m.unrealized + m.base_pnl, abs=1e-9)
    assert m.unrealized != 0.0  # 那笔没还原的加仓是真的挂着，不是恰好为零
    assert (m.t_out_fills, m.t_in_fills, m.flew, m.added) == (1, 2, 1, 1)
    assert m.fly_rate == pytest.approx(1.0, abs=1e-12)  # 唯一一笔 T出当天没接回来
    assert m.add_rate == pytest.approx(0.5, abs=1e-12)  # 两笔 T入，其中一笔成了加仓


def test_metrics_is_immutable_so_a_report_cannot_edit_history() -> None:
    """成绩单是冻结的：报告只能转述它，改不动它。"""
    m = measure(_result(trips=(trip(1.0),)))
    with pytest.raises(AttributeError):
        m.trips = 99  # type: ignore[misc]
