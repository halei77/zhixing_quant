"""事件循环的时钟、顺序与留痕（ADR-0010 决定 2、3、5、6；03-4.1、03-4.2）。

引擎是这台机器里唯一"知道时间怎么走"的一块，所以这里的测试全在问同一类问题：一笔单子
在哪一根成交、策略能看见什么、同一时刻谁先谁后、跑三遍是不是逐位一样。四条判据本身在
`test_backtest_fills.py` 与 `test_backtest_position.py`，不在这里重复。
"""

from datetime import date, datetime, timedelta

import pytest

from tests.fakes import minute_span
from zhixing_quant.backtest.engine import (
    BandLookup,
    EquityPoint,
    NeverSignal,
    Signal,
    View,
    run,
    say,
)
from zhixing_quant.backtest.fills import REASON_LOCKED, REASON_NO_BAR, Order, Reject
from zhixing_quant.backtest.limits import PriceBand
from zhixing_quant.backtest.position import REASON_T1
from zhixing_quant.backtest.spec import Assumptions, Cost, Execution, Side, Sizing
from zhixing_quant.domain.bar import Bar, stamp_of

DAY1 = date(2026, 9, 17)
DAY2 = date(2026, 9, 18)
COST = Cost(
    commission_pct=0.025, commission_min=5.0, stamp_pct=0.05, transfer_pct=0.001, slippage_pct=0.02
)
SIZING = Sizing(lot_size=100, base_lots=3)


def assumptions(delay: int = 1, participation: float = 5.0) -> Assumptions:
    return Assumptions(
        cost=COST,
        execution=Execution(delay_bars=delay, max_participation_pct=participation),
        sizing=SIZING,
    )


def minutes(
    day: date, prices: list[float], *, symbol: str = "600519", volume: float = 1e6
) -> list[Bar]:
    """从 09:30 起每 5 分钟一根，`ts` 是收盘时刻：第一根 09:35。四价全等，好核对成交在哪个价上。"""
    start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30)
    return [
        minute_span(
            start + timedelta(minutes=5 * (offset + 1)),
            open_=price,
            high=price,
            low=price,
            close=price,
            volume=volume,
            symbol=symbol,
        )
        for offset, price in enumerate(prices)
    ]


def flat_bands(prev_close: float = 10.0) -> BandLookup:
    """一只不封板的板：主板 10% 档、昨收 `prev_close`。判据本身有别处测，这里要的是"有板"。

    参数带下划线是因为它真的不看是哪只票、哪天——`BandLookup` 的签名要求它在那儿。
    """

    def bands(_code: str, _day: date) -> PriceBand:
        return PriceBand.bound(10.0, prev_close)

    return bands


class Scripted:
    """按"该票第几根"给信号的桩策略。索引按票各算，与 `delay_bars` 同一把尺。"""

    name = "scripted"

    def __init__(self, plan: dict[tuple[str, int], Side]) -> None:
        self.plan = plan
        self.seen: list[View] = []

    def signals(self, view: View, /) -> Signal | None:
        self.seen.append(view)
        side = self.plan.get((view.code, view.index))
        return None if side is None else Signal(side=side, lots=3)


def test_a_signal_fills_on_the_next_bars_open_not_on_its_own() -> None:
    """07 §5.2 的禁止项：不许用信号价成交。信号在 09:35 那根发出，成交必须是 09:40 的开盘价。"""
    result = run(
        minutes(DAY1, [10.0, 10.2, 10.4]),
        strategy=Scripted({("600519", 0): "buy"}),
        assumptions=assumptions(),
        bands=flat_bands(),
    )
    assert len(result.fills) == 1
    filled = result.fills[0]
    assert filled.bar.ts == datetime(2026, 9, 17, 9, 40)  # 信号那根的下一根
    assert filled.price == pytest.approx(10.2 * 1.0002)  # 滑点往不利方向


def test_two_bars_of_delay_skips_one_bar_of_the_market() -> None:
    """`delay_bars` 是真参数不是装饰：隔两根就是跨过 09:40 那根，成交在 09:45。"""
    result = run(
        minutes(DAY1, [10.0, 10.2, 10.4, 10.6]),
        strategy=Scripted({("600519", 0): "buy"}),
        assumptions=assumptions(delay=2),
        bands=flat_bands(),
    )
    assert result.fills[0].bar.ts == datetime(2026, 9, 17, 9, 45)  # 隔两根 = 跨过 09:40
    assert result.fills[0].price == pytest.approx(10.4 * 1.0002)


def test_a_sealed_board_on_the_execution_bar_refuses_the_buy() -> None:
    """制度的四条判据在引擎里生效：要成交的那根一字封在涨停上，买方向就是买不到。"""
    result = run(
        minutes(DAY1, [10.0, 11.0]),
        strategy=Scripted({("600519", 0): "buy"}),
        assumptions=assumptions(),
        bands=flat_bands(),
    )
    assert result.fills == ()
    assert [r.reason for r in result.rejects] == [REASON_LOCKED]


def test_the_second_sell_of_the_day_hits_the_t_plus_one_wall() -> None:
    """底仓只有 300 股：今天第二次卖是账户层面不成立的委托，与那根行情好坏无关。"""
    result = run(
        minutes(DAY1, [10.0, 10.2, 10.4, 10.6]),
        strategy=Scripted({("600519", 0): "sell", ("600519", 1): "sell"}),
        assumptions=assumptions(),
        bands=flat_bands(),
    )
    assert [f.order.side for f in result.fills] == ["sell"]
    assert [r.reason for r in result.rejects] == [REASON_T1]
    assert "T+1" in result.rejects[0].detail


def test_selling_the_base_without_buying_it_back_leaves_nothing_to_sell_tomorrow() -> None:
    """昨天把底仓全卖了、没接回来 → 今天期初持仓是 0，T+1 不是"等一天就有额度"。"""
    bars = minutes(DAY1, [10.0, 10.2, 10.4]) + minutes(DAY2, [10.3, 10.5, 10.7])
    result = run(
        bars,
        strategy=Scripted({("600519", 0): "sell", ("600519", 4): "sell"}),
        assumptions=assumptions(),
        bands=flat_bands(),
    )
    assert [f.order.side for f in result.fills] == ["sell"]
    assert [r.reason for r in result.rejects] == [REASON_T1]


def test_a_bought_back_base_opens_tomorrow_s_allowance() -> None:
    """昨天卖、昨天接回 → 今天期初又是 300 股，能再 T 一次。这才是做T 的正常节奏。"""
    bars = minutes(DAY1, [10.0, 10.2, 10.4]) + minutes(DAY2, [10.3, 10.5, 10.7])
    plan: dict[tuple[str, int], Side] = {
        ("600519", 0): "sell",  # 成交 i1
        ("600519", 1): "buy",  # 成交 i2（当天接回）
        ("600519", 3): "sell",  # 成交 i4（第二天）
    }
    result = run(bars, strategy=Scripted(plan), assumptions=assumptions(), bands=flat_bands())
    assert [f.order.side for f in result.fills] == ["sell", "buy", "sell"]
    assert result.rejects == ()
    # 前两笔配成一个日内回合；第二天那笔卖出没有可配的买入，还欠着
    assert [t.same_day for t in result.round_trips] == [True]


def test_day_events_are_counted_at_the_close_of_the_day_they_happened() -> None:
    """T飞的计数挂在**成交那天**的收盘；跨日接回是一个回合，不回头改昨天的数。"""
    bars = minutes(DAY1, [10.0, 10.2, 10.4]) + minutes(DAY2, [10.3, 10.5, 10.7])
    plan: dict[tuple[str, int], Side] = {("600519", 0): "sell", ("600519", 3): "buy"}
    result = run(bars, strategy=Scripted(plan), assumptions=assumptions(), bands=flat_bands())
    assert [(e.trade_date, e.flew, e.added) for e in result.day_events] == [
        (DAY1, 1, 0),
        (DAY2, 0, 0),
    ]
    trip = result.round_trips[0]
    assert not trip.same_day
    assert trip.pnl < 0  # 昨天卖 10.2、今天接回 10.5：接贵了，回合是亏的


def test_the_same_moment_is_ordered_by_code_so_two_runs_agree() -> None:
    """同一分钟两只票都有行：谁先处理会决定谁先占用当天的额度，所以顺序必须写死（决定 2）。

    输入顺序也验一次——`sorted` 稳不稳是 03-4.1 点名的不确定来源之一。
    """
    bars = minutes(DAY1, [10.0, 10.2]) + minutes(DAY1, [10.4, 10.6], symbol="000001")
    plan: dict[tuple[str, int], Side] = {("600519", 0): "sell", ("000001", 0): "sell"}
    first = run(bars, strategy=Scripted(plan), assumptions=assumptions(), bands=flat_bands())
    second = run(
        list(reversed(bars)), strategy=Scripted(plan), assumptions=assumptions(), bands=flat_bands()
    )
    assert [f.order.code for f in first.fills] == ["000001", "600519"]
    assert first == second


def test_the_equity_curve_is_marked_at_the_close_of_every_bar() -> None:
    """空仓策略也要有净值曲线：`NeverSignal` 跑出来的那条就是底仓自身的涨跌。"""
    result = run(
        minutes(DAY1, [10.0, 11.0, 12.0]),
        strategy=NeverSignal(),
        assumptions=assumptions(),
        bands=flat_bands(),
    )
    assert result.fills == () and result.round_trips == ()
    assert [round(p.pnl, 6) for p in result.equity] == [0.0, 300.0, 600.0]
    assert result.equity[-1] == EquityPoint(
        stamp=(DAY1, datetime(2026, 9, 17, 9, 45)),
        code="600519",
        cash=0.0,
        held=300,
        market_value=3600.0,
        pnl=600.0,
    )


def test_an_order_that_waits_past_the_last_bar_is_rejected_not_chased() -> None:
    """区间里没有第 `delay` 根了：记一条 `no_bar` 拒单，不追单（决定 4 末段）。"""
    result = run(
        minutes(DAY1, [10.0, 10.2]),
        strategy=Scripted({("600519", 1): "buy"}),
        assumptions=assumptions(),
        bands=flat_bands(),
    )
    assert result.fills == ()
    assert [r.reason for r in result.rejects] == [REASON_NO_BAR]
    assert "区间到头" in result.rejects[0].detail


def test_a_zero_lot_signal_is_not_an_action() -> None:
    with pytest.raises(ValueError, match="0 手或负数"):
        Signal(side="buy", lots=0)


def test_the_view_counts_hands_separately_from_the_t_plus_one_allowance() -> None:
    """今天买进来的 3 手在持仓里、不在额度里：这两个数必须分开报给策略。

    合并成一个"可卖手数"是这里最省事的写法，也是决定 5 最容易被绕过去的地方——策略据此
    下的卖单会天天撞 T+1，而看起来像是引擎在无理拒单。
    """
    strategy = Scripted({("600519", 0): "buy"})
    run(
        minutes(DAY1, [10.0, 10.2, 10.4]),
        strategy=strategy,
        assumptions=assumptions(),
        bands=flat_bands(),
    )
    # 3 手底仓在 09:40 那根成交后才涨到 6 手，而额度从来只有期初那 3 手
    assert [(v.lots_held, v.lots_sellable) for v in strategy.seen] == [(3, 3), (6, 3), (6, 3)]


def test_rejects_are_tallied_by_reason_with_absent_reasons_left_out() -> None:
    """一张只有真发生过的拒因的表。补零是报告的活，引擎不替谁假装查全了。"""
    bars = minutes(DAY1, [10.0, 10.2, 10.4])
    plan: dict[tuple[str, int], Side] = {
        ("600519", 0): "sell",  # 成交，用掉今天的额度
        ("600519", 1): "sell",  # 撞 T+1
        ("600519", 2): "buy",  # 后面没有根了 → no_bar
    }
    result = run(bars, strategy=Scripted(plan), assumptions=assumptions(), bands=flat_bands())
    assert result.rejects_by_reason == {REASON_T1: 1, REASON_NO_BAR: 1}
    assert REASON_LOCKED not in result.rejects_by_reason


def test_running_the_same_bars_three_times_gives_the_identical_result() -> None:
    """03-4.1 / 07 验收 1：同一份输入跑三遍，结果**逐位**相等（不是近似相等）。"""
    bars = minutes(DAY1, [10.0, 10.2, 10.4, 10.3]) + minutes(DAY2, [10.5, 10.7, 10.6, 10.8])
    plan: dict[tuple[str, int], Side] = {
        ("600519", 0): "sell",
        ("600519", 2): "buy",
        ("600519", 5): "sell",
    }
    runs = [
        run(bars, strategy=Scripted(plan), assumptions=assumptions(), bands=flat_bands())
        for _ in range(3)
    ]
    assert runs[0] == runs[1] == runs[2]
    assert runs[0].fills and runs[0].round_trips


def test_no_strategy_ever_sees_a_bar_from_the_future() -> None:
    """03-4.2 的那句话，逐根验：`view.bars` 的最后一根就是当前根，一根不多。"""
    bars = minutes(DAY1, [10.0, 10.2, 10.4]) + minutes(DAY2, [10.3, 10.5])
    strategy = Scripted({})
    run(bars, strategy=strategy, assumptions=assumptions(), bands=flat_bands())
    assert len(strategy.seen) == len(bars)
    for view in strategy.seen:
        assert view.bars[-1] is view.bar
        assert len(view.bars) == view.index + 1


@pytest.mark.parametrize("cut", [1, 2, 3, 4, 5])
def test_truncating_the_data_changes_nothing_before_the_cut(cut: int) -> None:
    """截断一致性（03-4.2）：前 `cut` 根单独跑，与全量跑出来的前 `cut` 根逐字段相等。

    "策略必须是纯函数"这条要求，机器验收点就在这里。截断处那笔还在路上的单子会多出一条
    `no_bar`——那是截断的正确结果，不是不一致，所以它单独判。
    """
    bars = minutes(DAY1, [10.0, 10.2, 10.4]) + minutes(DAY2, [10.3, 10.5])
    plan: dict[tuple[str, int], Side] = {("600519", 0): "sell", ("600519", 2): "buy"}
    full = run(bars, strategy=Scripted(plan), assumptions=assumptions(), bands=flat_bands())
    head = run(bars[:cut], strategy=Scripted(plan), assumptions=assumptions(), bands=flat_bands())
    assert head.equity == full.equity[:cut]
    assert head.fills == tuple(f for f in full.fills if _index(bars, f.bar) < cut)
    settled = [r for r in head.rejects if r.reason != REASON_NO_BAR]
    assert tuple(r.reason for r in settled) == tuple(
        r.reason for r in full.rejects if r.reason != REASON_NO_BAR and _signal_index(bars, r) < cut
    )
    # 截断处还挂着的单子只能等成 no_bar：它等的那一根在截断之后的数据里，而那份数据没给
    assert all(
        r.reason == REASON_NO_BAR and _signal_index(bars, r) < cut
        for r in head.rejects
        if r.reason == REASON_NO_BAR
    )


def test_the_truncation_check_has_teeth() -> None:
    """上一条不是恒等式：把未来那根塞进 `view.bars`，同一个策略的决定当场就变。

    没有这一条，"截断一致"可能只是因为那个策略什么都没做而成立——那种绿比红糟。
    """
    quiet = minutes(DAY1, [10.0, 10.2, 10.4])
    planted = quiet + minutes(DAY1, [100.0])

    class Tail:
        """只看得见"给我的那些根里最后一根"的策略：引擎给多少未来，它就犯多少错。"""

        name = "tail"

        def signals(self, view: View, /) -> Signal | None:
            return (
                None
                if view.index
                else Signal(side="buy" if view.bars[-1].close > 11.0 else "sell", lots=3)
            )

    honest = run(quiet, strategy=Tail(), assumptions=assumptions(), bands=flat_bands())
    assert [f.order.side for f in honest.fills] == ["sell"]

    leaked = View(
        code="600519",
        stamp=stamp_of(quiet[0].trade_date, quiet[0].ts),
        bar=quiet[0],
        bars=tuple(planted),  # 引擎若把整段都递过去
        holding=300,
        sellable=300,
        lot_size=100,
        index=0,
    )
    assert Tail().signals(leaked) == Signal(side="buy", lots=3)


def test_say_prints_a_time_only_when_there_is_one() -> None:
    """日线的 `ts` 是 `datetime.min`（ADR-0009 决定 3 的那把退化），报成 00:00 是假话。"""
    assert say((DAY1, datetime.min)) == "2026-09-17"
    assert say((DAY1, datetime(2026, 9, 17, 9, 40))) == "2026-09-17 09:40"


def _index(bars: list[Bar], bar: Bar) -> int:
    return next(i for i, given in enumerate(bars) if given is bar)


def _signal_index(bars: list[Bar], reject: Reject) -> int:
    order: Order = reject.order
    return next(
        i
        for i, given in enumerate(bars)
        if given.code == order.code and stamp_of(given.trade_date, given.ts) == order.signal_at
    )
