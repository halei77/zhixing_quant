"""竞争框架的对抗性测试（grid / folds / stats / runner / cli 装配层）。

怀疑点清单：
1. 测试段会不会被选择过程碰到（§5.3"只许用一次"的机器形态是集合不相交）
2. 折内训练段是否总在验证段之前（时间倒流的 walk-forward 比没有还糟）
3. 二项检验的递推在边界（w=0、w=n、n=1）会不会溢出或翻转
4. merge 的连败取 max（合并成 sum 会把"各折各连败 3"吹成 6）
5. best 的平手稳定性（同胜率时给后到的参数加分 = p-hacking 的自动化）
6. cli 的门槛判定行：无回合、样本下限、连败上限各自独立拦
7. 期望百分比口径：绝对金额跨票价不可比，5.4-1 的 +0.05% 必须按 pct 判
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from tests.fakes import assumptions, flat_bands, minute_span
from zhixing_quant.compete import folds as fold_mod
from zhixing_quant.compete import runner, stats
from zhixing_quant.compete.cli import _gate_row, _range
from zhixing_quant.compete.grid import Slot, slots
from zhixing_quant.compete.runner import Score
from zhixing_quant.domain.bar import Bar
from zhixing_quant.strategy.intraday import VWAPReversion

DAY0 = date(2026, 6, 1)


# ── grid ─────────────────────────────────────────────────────────────────────


def test_grid_sizes_match_the_seven_contract() -> None:
    """07 §四 写死的网格上限：C2 ≤18、C3/C4 ≤6、B0 对照 3 档。超了就是改契约，先改 07。"""
    sizes = {slot.name: len(slot.grid) for slot in slots()}
    assert sizes == {"C2": 18, "C3": 6, "C4": 6, "B0": 3}


def test_every_grid_entry_builds_a_named_strategy() -> None:
    for slot in slots():
        for params in slot.grid:
            strategy = slot.build(dict(params))
            assert strategy.name and callable(getattr(strategy, "signals", None))


# ── folds ────────────────────────────────────────────────────────────────────


def _days(n: int) -> tuple[date, ...]:
    """n 个连续交易日（跳过周末，更像真的；对切分逻辑本身连续日也等价）。"""
    out: list[date] = []
    day = DAY0
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return tuple(out)


def test_test_segment_never_touches_the_selection_side() -> None:
    """§5.3"测试段只许用一次"的机器形态：选择段与测试段的日期集合不相交。"""
    days = _days(80)
    selection = fold_mod.split(days, folds=3)
    selection_set = set(fold_mod.selection_days(selection))
    assert selection_set.isdisjoint(set(selection.test))
    assert selection.test[-1] == days[-1], "测试段是最末尾那一截"


def test_train_always_precedes_valid_within_a_fold() -> None:
    days = _days(80)
    selection = fold_mod.split(days, folds=4)
    assert len(selection.folds) == 4
    for fold in selection.folds:
        assert max(fold.train) < min(fold.valid), "训练段必须整体在验证段之前"
    # 扩张窗：训练段逐折变长
    sizes = [len(fold.train) for fold in selection.folds]
    assert sizes == sorted(sizes) and len(set(sizes)) == len(sizes)


def test_split_refuses_too_few_days_or_one_fold() -> None:
    with pytest.raises(ValueError, match="个交易日"):
        fold_mod.split(_days(30), folds=4)
    with pytest.raises(ValueError, match="一折"):
        fold_mod.split(_days(80), folds=1)


def test_chunks_survive_a_remainder() -> None:
    chunks = fold_mod._chunks(_days(10), 3)  # 10 = 4+3+3，余数补给前面的段
    assert [len(c) for c in chunks] == [4, 3, 3]
    assert sum(len(c) for c in chunks) == 10


# ── stats ────────────────────────────────────────────────────────────────────


def test_binomial_tail_matches_known_values() -> None:
    """教科书值：n=100 时 59 胜单侧 p≈0.0443（<0.05），58 胜 p≈0.0666（>0.05）——
    这就是 07 §5.4-1 那句"n=100 要 ≥59 胜"的出处。"""
    assert stats.binom_tail_ge(59, 100) == pytest.approx(0.0443, abs=5e-4)
    assert stats.binom_tail_ge(58, 100) == pytest.approx(0.0666, abs=5e-4)
    assert stats.binom_tail_ge(50, 100) > 0.5
    assert stats.binom_tail_ge(100, 100) == pytest.approx(0.5**100)
    assert stats.binom_tail_ge(0, 5) == pytest.approx(1.0)
    assert stats.binom_tail_ge(1, 1) == pytest.approx(0.5)


def test_binomial_tail_refuses_out_of_range() -> None:
    with pytest.raises(ValueError, match="wins"):
        stats.binom_tail_ge(7, 5)


def test_gate_encodes_5_4_1_exactly() -> None:
    # 59/100、期望 +6bp：胜率显著、期望过线 → 过
    assert stats.gate(59, 100, 0.0006).ok
    # 胜率够但期望只有 +3bp（< 5bp 下限）→ 不过：7 §5.4-1 是合取
    assert not stats.gate(70, 100, 0.0003).ok
    # 期望够但胜率不显著 → 不过
    assert not stats.gate(55, 100, 0.001).ok
    with pytest.raises(ValueError, match="没有资格"):
        stats.gate(0, 0, 0.0)


# ── runner ───────────────────────────────────────────────────────────────────


def _bars(day: date, prices: list[float], *, symbol: str = "600519") -> list[Bar]:
    start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30)
    return [
        minute_span(
            start + timedelta(minutes=5 * (i + 1)),
            open_=p,
            high=p,
            low=p,
            close=p,
            volume=1e5,
            symbol=symbol,
        )
        for i, p in enumerate(prices)
    ]


def _score(trips: int, wins: int, streak: int = 0, slot: str = "C2") -> Score:
    from zhixing_quant.backtest.metrics import Metrics

    losses = trips - wins
    return Score(
        slot=slot,
        title="t",
        params={"p": 1},
        phase="valid",
        metrics=Metrics(
            trips=trips,
            wins=wins,
            losses=losses,
            even=0,
            win_sum=float(wins),
            loss_sum=float(-losses),
            trip_pnl=0.0,
            t_out_fills=trips,
            t_in_fills=trips,
            flew=0,
            added=0,
            max_loss_streak=streak,
            final_pnl=0.0,
            unrealized=0.0,
            base_pnl=0.0,
        ),
        expectancy_pct=(0.0001 * (wins - losses)) if trips else None,
        streak=streak,
        trips_by_code={"600519": trips},
    )


def test_merge_adds_counts_and_takes_max_streak() -> None:
    merged = runner.merge([_score(3, 2, streak=3), _score(2, 1, streak=2)])
    assert merged.metrics.trips == 5
    assert merged.metrics.wins == 3
    assert merged.streak == 3, "连败取最大，不是相加"
    assert merged.trips_by_code == {"600519": 5}


def test_merge_refuses_an_empty_list() -> None:
    with pytest.raises(ValueError, match="没有可合并"):
        runner.merge([])


def test_best_is_stable_on_ties() -> None:
    a, b = _score(4, 2, slot="C2"), _score(4, 2, slot="C2")
    assert runner.best([a, b]) is a, "平手留在先声明的参数上：不给后到的新鲜感加分"
    better = _score(4, 3)
    assert runner.best([a, better]) is better


def test_run_phase_reports_trips_by_code_and_pct() -> None:
    """一个真实的小场景走完 run_phase：期待至少一笔回合，且逐票计数落账。"""
    slot = Slot("C2", "测试", VWAPReversion, ({"lookback": 5, "z_enter": 1.5, "z_exit": 0.5},))
    # 价格全程留在 flat_bands(prev_close=10) 的 ±10% 板内——8.0 那种深跌会被判"跌停锁死"，
    # 卖单全拒、回合永远配不上（第一版就用 8.0，测出的是拒单簿不是竞争）。
    day1 = _bars(DAY0, [10.0, 10.4] * 10 + [9.2, 9.2, 9.2, 9.2])
    day2 = _bars(DAY0 + timedelta(days=1), [9.4, 9.6] * 8, symbol="600519")
    score = runner.run_phase(
        slot,
        {"lookback": 5, "z_enter": 1.5, "z_exit": 0.5},
        {"600519": day1 + day2},
        [DAY0, DAY0 + timedelta(days=1)],
        assumptions=assumptions(),
        bands=flat_bands(),
        phase="test",
    )
    assert score.metrics.trips >= 1
    assert score.trips_by_code.get("600519", 0) == score.metrics.trips
    assert score.expectancy_pct is not None


# ── cli 装配层 ───────────────────────────────────────────────────────────────


def test_range_parses_and_refuses_reversed_order() -> None:
    lo, hi = _range(date(2026, 1, 1), date(2026, 9, 1), None, None)
    assert (lo, hi) == (date(2026, 1, 1), date(2026, 9, 1))
    with pytest.raises(Exception, match="区间颠倒"):
        _range(date(2026, 1, 1), date(2026, 9, 1), "2026-09-01", "2026-01-01")


def test_gate_row_rejects_zero_trip_and_independent_gates() -> None:
    slot = Slot("C2", "t", VWAPReversion, ({},))
    row = _gate_row(slot, {"p": 1}, _score(0, 0), _score(0, 0))
    assert "无回合" in row[-1], "零回合直接出局，不算胜率"

    # 样本下限（5.4-4）：总回合够但单票不够 → 样本✗
    few_per_code = _score(100, 60)
    object.__setattr__(few_per_code, "trips_by_code", {"600519": 5})
    row = _gate_row(slot, {"p": 1}, few_per_code, few_per_code)
    assert "样本✗" in row[-1]

    # 连败上限（5.4-5）：其他全过但连败 9 → 连败✗
    streaky = _score(100, 60, streak=9)
    object.__setattr__(streaky, "trips_by_code", {"600519": 100})
    row = _gate_row(slot, {"p": 1}, streaky, streaky)
    assert "连败✗" in row[-1]

    # 全过 → 过
    good = _score(100, 60, streak=3)
    object.__setattr__(good, "trips_by_code", {"600519": 100})
    good = Score(
        slot=good.slot,
        title=good.title,
        params=good.params,
        phase=good.phase,
        metrics=good.metrics,
        expectancy_pct=0.001,
        streak=3,
        trips_by_code={"600519": 100},
    )
    row = _gate_row(slot, {"p": 1}, good, good)
    assert row[-1].startswith("过"), f"胜率 60%、期望 10bp、连败 3 应全过：{row[-1]}"


def test_score_helper_pct_is_negative_aware() -> None:
    """_score 的期望符号跟胜负差走：净胜 +、净负 −，0 回合 None。"""
    up = _score(2, 2).expectancy_pct
    down = _score(2, 0).expectancy_pct
    assert up is not None and up > 0
    assert down is not None and down < 0
    assert _score(0, 0).expectancy_pct is None
