"""日内候选策略的对抗性测试（05 §三：假设一定有 bug，写出让它暴露的测试）。

怀疑点清单（写测试前先列，规则要求）：
1. 预热期不足时策略是否硬算（短序列的 z 是噪声，还会除零）
2. 状态是否从 holding 推：入场委托被拒后，策略会不会对着底仓发裸卖/裸买
3. 一回合未平时是否沉默（堆仓 = 失控）
4. 零量根、全天一根价这类死水是否会崩或编出信号
5. B0 的可复现性（同种子逐位同、异种子异）
6. 参数校验（z_enter ≤ z_exit 这类自相矛盾的配置要当场响）
7. 全链路：策略接进引擎，一笔回合真的能配对成交

C2/C3 的方向判据用"足够极端"的行情构造，落点断言方向与顺序，不钉具体 z 值——
z 的中间地带是参数调优的事，测试只钉边界行为。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from tests.fakes import assumptions, flat_bands, minute_span
from zhixing_quant.backtest.engine import Signal, run
from zhixing_quant.domain.bar import Bar
from zhixing_quant.strategy.intraday import (
    CANDIDATES,
    DonchianBreakout,
    RandomBaseline,
    RollingReversion,
    VolumeSpike,
)

DAY1 = date(2026, 9, 17)
DAY2 = date(2026, 9, 18)


def path(
    day: date, closes: list[float], volumes: list[float] | None = None, *, symbol: str = "600519"
) -> list[Bar]:
    """一天的价格路径，5 分钟一根、09:35 收第一根。四价全等（VWAP = 量的加权均价）。"""
    start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30)
    bars = []
    for i, price in enumerate(closes):
        volume = 1e5 if volumes is None else volumes[i]
        bars.append(
            minute_span(
                start + timedelta(minutes=5 * (i + 1)),
                open_=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
                symbol=symbol,
            )
        )
    return bars


def drive(
    strategy: Any,
    bars: list[Bar],
    *,
    holding: int = 300,
    sellable: int = 300,
    fill: bool = True,
) -> list[tuple[int, Signal]]:
    """把策略跑过整段历史，收下全部信号：(索引, 信号) 对。

    与引擎同款的极简成交模拟：信号在下一根**成交**（delay=1），买入加持仓（新买的不可卖）、
    卖出减持仓减可卖。不模拟这一拍，holding 永远不动，"从持仓推状态"的机制在单元测试里
    根本走不起来——第一版助手就栽在这里，七个用例集体测了个寂寞。
    """
    from zhixing_quant.backtest.engine import View
    from zhixing_quant.domain.bar import stamp_of

    out = []
    strategy._bases.clear()  # 直接构造 View 的单元测试里，同一实例跑多条场景要先抹底仓
    if hasattr(strategy, "_opened_at"):
        strategy._opened_at.clear()
    hold, sell, pending = holding, sellable, None
    for i in range(1, len(bars) + 1):
        prefix = bars[:i]
        bar = prefix[-1]
        if pending is not None and fill:
            if pending == "buy":
                hold += 100
            else:
                hold -= 100
                sell -= 100
            pending = None
        view = View(
            code=bar.symbol,
            stamp=stamp_of(bar.trade_date, bar.ts),
            bar=bar,
            bars=tuple(prefix),
            holding=hold,
            sellable=sell,
            lot_size=100,
            index=i - 1,
        )
        signal = strategy.signals(view)
        if signal is not None:
            out.append((i - 1, signal))
            pending = signal.side
    return out


# ── C2 VWAP 均值回归 ──────────────────────────────────────────────────────────


def oscillation(n: int, base: float = 10.0, amp: float = 0.4) -> list[float]:
    return [base + amp * (i % 2) for i in range(n)]


def test_c2_stays_silent_through_warmup() -> None:
    """预热不足不许硬算：短序列的 z 是噪声。"""
    strategy = RollingReversion(lookback=20)
    bars = path(DAY1, oscillation(15))
    assert drive(strategy, bars) == []


def test_c2_buys_a_deep_dip_and_exits_on_recovery() -> None:
    """下探极值 → 买（T入）；回到 VWAP 附近 → 卖平回合。这是它存在的全部理由。"""
    strategy = RollingReversion(lookback=20)
    day1 = [*oscillation(17), 7.0, 7.2, 7.1]  # 尾部深跌
    day2 = [8.5, 9.5, 10.0, *oscillation(18)]  # 次日拉回 + 震荡
    signals = drive(strategy, path(DAY1, day1) + path(DAY2, day2))
    sides = [s.side for _, s in signals]
    assert "buy" in sides, "深跌必须触发 T入"
    assert "sell" in sides, "拉回后必须平仓"
    assert sides.index("buy") < sides.index("sell"), "先入后平"


def test_c2_sells_a_spike_then_buys_back() -> None:
    """冲高极值 → 卖（T出）；跌回 → 买回。空回合那一半同样要转。"""
    strategy = RollingReversion(lookback=20)
    day1 = [*oscillation(17), 13.5, 13.7, 13.6]  # 尾部冲高
    day2 = [12.0, 11.0, 10.5, *oscillation(18)]
    signals = drive(strategy, path(DAY1, day1) + path(DAY2, day2))
    sides = [s.side for _, s in signals]
    assert sides and sides[0] == "sell", "冲高先 T出"
    assert "buy" in sides, "跌回后买回"


def test_c2_flat_water_is_silent_not_a_crash() -> None:
    """全天一根价：std=0 问不出 z → None。除零或编信号都算 bug。"""
    strategy = RollingReversion(lookback=5)
    bars = path(DAY1, [10.0] * 30)
    assert drive(strategy, bars) == []


def test_c2_rejects_self_contradicting_bands() -> None:
    with pytest.raises(ValueError, match="z_enter"):
        RollingReversion(z_enter=0.5, z_exit=0.5)
    with pytest.raises(ValueError, match="lookback"):
        RollingReversion(lookback=1)


# ── C3 区间突破 ──────────────────────────────────────────────────────────────


def test_c3_needs_the_full_range_before_any_signal() -> None:
    strategy = DonchianBreakout(n_break=10)
    bars = path(DAY1, oscillation(11, amp=0.5))
    assert drive(strategy, bars) == []


def test_c3_buys_confirmed_breakout_and_sells_the_fall_back() -> None:
    strategy = DonchianBreakout(n_break=10, confirm=2)
    closes = [*oscillation(10, amp=0.5), 11.0, 11.0, 11.0, 9.5, 9.5, 9.0, 9.0]
    signals = drive(strategy, path(DAY1, closes))
    sides = [s.side for _, s in signals]
    # 三个信号各有其位：突破买 → 跌回边界平多 → 价格仍在区间下方 = 下方突破，开空回合
    assert sides == ["buy", "sell", "sell"], f"实际 {sides}"


def test_c3_ignores_wicks_that_do_not_close_outside() -> None:
    """盘中刺穿不算突破：判据看的是收盘。影线触顶 → 全程沉默。"""
    strategy = DonchianBreakout(n_break=10, confirm=1)
    closes = oscillation(10, amp=0.5) + [10.25] * 6  # 10.25 在区间内
    assert drive(strategy, path(DAY1, closes)) == []


def test_c3_rejects_degenerate_range() -> None:
    with pytest.raises(ValueError, match="n_break"):
        DonchianBreakout(n_break=1)


# ── C4 量能异动 ──────────────────────────────────────────────────────────────


def test_c4_follows_an_up_spike_and_time_stops() -> None:
    strategy = VolumeSpike(k=3.0, confirm=1, confirm_window=10, exit_bars=5)
    volumes = [1e5] * 12 + [5e5] + [1e5] * 8  # 第 13 根（下标 12）放量 5 倍
    closes = [*oscillation(12, amp=0.2), 10.6, *oscillation(8, base=10.6, amp=0.2)]
    signals = drive(strategy, path(DAY1, closes, volumes))
    sides = [s.side for _, s in signals]
    assert sides[0] == "buy", "放量收涨 → T入"
    assert "sell" in sides, "时间止损要把回合平掉"


def test_c4_waits_out_the_time_stop_before_exiting() -> None:
    strategy = VolumeSpike(k=3.0, confirm=1, confirm_window=5, exit_bars=4)
    volumes = [1e5] * 6 + [5e5] + [1e5] * 8
    closes = [*oscillation(6, amp=0.2), 10.6, *oscillation(8, base=10.6, amp=0.2)]
    indexes = [i for i, _ in drive(strategy, path(DAY1, closes, volumes))]
    assert len(indexes) >= 2 and indexes[1] - indexes[0] >= 4, "止损信号必须等满 exit_bars 根"


def test_c4_wont_naked_sell_when_the_entry_never_filled() -> None:
    """入场委托被拒（holding 不动）之后，时间到也不许对着底仓发卖出。"""
    strategy = VolumeSpike(k=3.0, confirm=1, confirm_window=5, exit_bars=2)
    volumes = [1e5] * 5 + [1e5, 5e5] + [1e5] * 6
    closes = [*oscillation(6, amp=0.2), 10.6, *oscillation(6, base=10.6, amp=0.2)]
    # fill=False：入场单从未成交，holding 恒为 300——全程不许出现对着底仓的 sell
    signals = drive(strategy, path(DAY1, closes, volumes), fill=False)
    assert all(s.side != "sell" for _, s in signals)


def test_c4_follows_a_down_spike_with_a_sell() -> None:
    strategy = VolumeSpike(k=3.0, confirm=1, confirm_window=5, exit_bars=4)
    volumes = [1e5] * 5 + [1e5, 5e5] + [1e5] * 2
    closes = [*oscillation(6, amp=0.2), 9.0, *oscillation(2, base=9.0, amp=0.2)]
    signals = drive(strategy, path(DAY1, closes, volumes))
    assert signals and signals[0][1].side == "sell", "放量收跌 → T出"


def test_c4_rejects_nonpositive_thresholds() -> None:
    with pytest.raises(ValueError, match="k"):
        VolumeSpike(k=0.0)
    with pytest.raises(ValueError, match="exit_bars"):
        VolumeSpike(exit_bars=0)


# ── B0 随机对照 ──────────────────────────────────────────────────────────────


def test_b0_is_deterministic_per_seed_and_differs_across_seeds() -> None:
    bars = path(DAY1, oscillation(60))
    first = [(s.side, s.note) for _, s in drive(RandomBaseline(seed=1), bars)]
    second = [(s.side, s.note) for _, s in drive(RandomBaseline(seed=1), bars)]
    other = [(s.side, s.note) for _, s in drive(RandomBaseline(seed=2), bars)]
    assert first == second, "同种子必须逐位相同（03-4.1）"
    assert first != other, "异种子必须不同，否则对照失去随机性"


def test_b0_never_sells_without_sellable_shares() -> None:
    strategy = RandomBaseline(seed=3)
    bars = path(DAY1, oscillation(40))
    from zhixing_quant.backtest.engine import View
    from zhixing_quant.domain.bar import stamp_of

    for i in range(1, len(bars) + 1):
        prefix = bars[:i]
        bar = prefix[-1]
        signal = strategy.signals(
            View(
                code=bar.symbol,
                stamp=stamp_of(bar.trade_date, bar.ts),
                bar=bar,
                bars=tuple(prefix),
                holding=300,
                sellable=0,  # 整段都可卖额度为零（极端 T+1 现场）
                lot_size=100,
                index=i - 1,
            )
        )
        assert signal is None or signal.side == "buy", "可卖额度为零时绝不允许出现卖出"


def test_b0_rejects_zero_frequency() -> None:
    with pytest.raises(ValueError, match="every_n"):
        RandomBaseline(every_n=0)


# ── 全链路：接进引擎，回合真的能配对 ─────────────────────────────────────────


def test_c2_round_trip_through_the_engine() -> None:
    """策略接进引擎跑通一回合：先买后卖、一笔回合、成交在信号后第一根。"""
    strategy = RollingReversion(lookback=20, z_enter=1.5)
    day1 = [*oscillation(17), 9.2, 9.2, 9.2, 9.2, 9.2]  # 板内深跌，留根给入场单成交
    day2 = [9.4, 9.6, 9.8, *oscillation(20)]
    bars = path(DAY1, day1) + path(DAY2, day2)
    result = run(bars, strategy=strategy, assumptions=assumptions(), bands=flat_bands())
    sides = [fill.order.side for fill in result.fills]
    assert sides and sides[0] == "buy", "先 T入"
    # 跨日窗口在恢复后的震荡里会继续开平回合——断言每一回合都完整配对，而不是只许一回合
    assert len(result.round_trips) == len(sides) // 2
    assert all(a == "buy" and b == "sell" for a, b in zip(sides[::2], sides[1::2], strict=True))


def test_every_candidate_constructs_zero_arg_and_is_named() -> None:
    """zx-backtest 的零参构造入口（cli 的 strategies() 工厂）依赖这两件事。"""
    for candidate in (*CANDIDATES, RandomBaseline):
        instance = candidate()
        assert isinstance(instance.name, str) and instance.name
        assert callable(instance.signals)
