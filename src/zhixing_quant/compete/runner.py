"""竞争编排的**纯一半**：引擎的批量调用与成绩聚合（07 §四协议的执行件）。

不碰盘：bars / assumptions / bands 全由调用方注入——03-4.1 的"跑 3 次逐位相等"对竞争
同样生效，而它的前提就是这一层看不见磁盘。读盘那一半在 `compete/cli.py`（它自己那个
组合根）。

聚合口径两条，都是"不许重新发明"：

- 指标一律从 `measure(result)` 来（决定 6 的分母已经钉死），这里只做跨折/跨票的**合并**：
  计数相加、连败取最大（各折内部的顺序在合并时已不可考，取最大是保守侧）。
- 期望的**百分比口径**另算：`单笔 pnl ÷ (买入价 × 股数)`——5.4-1 的"+0.05%"是相对量，
  `Metrics.expectancy` 的绝对金额跨票价不可比（茅台一回合的一手与京东方一手不是一个量级）。

成本敏感性（5.4-2）没有捷径：×1.5 的成绩必须换 `assumptions.with_cost_scaled(1.5)` 重跑
引擎，线性外推出来的数不是测量是外插。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from zhixing_quant.backtest.engine import BandLookup, Strategy, run
from zhixing_quant.backtest.metrics import Metrics, measure
from zhixing_quant.backtest.position import RoundTrip
from zhixing_quant.backtest.spec import Assumptions
from zhixing_quant.compete.grid import Slot
from zhixing_quant.domain.bar import Bar


@dataclass(frozen=True)
class Score:
    """一个 (槽位, 参数) 在某段日子上的成绩。`phase` 标这段是训练/验证/测试。

    `trips_by_code` 供 5.4-4 的样本下限用——"测试段 ≥100 回合且**每票** ≥10"要逐票数。
    """

    slot: str
    title: str
    params: Mapping[str, Any]
    phase: str
    metrics: Metrics
    expectancy_pct: float | None
    streak: int
    trips_by_code: Mapping[str, int]

    @property
    def win_rate(self) -> float | None:
        return self.metrics.win_rate


def _expectancy_pct(result_trips: Sequence[RoundTrip]) -> float | None:
    trips = list(result_trips)
    if not trips:
        return None
    total = sum(t.pnl / (t.buy_price * t.shares) for t in trips)
    return total / len(trips)


def run_phase(
    slot: Slot,
    params: Mapping[str, Any],
    bars_by_code: Mapping[str, Sequence[Bar]],
    days: Sequence[date],
    *,
    assumptions: Assumptions,
    bands: BandLookup,
    phase: str,
) -> Score:
    """一段日子上的一次完整运行：按日过滤 → 摊平 → 全新策略实例进引擎。

    每次调用 `slot.build` 造新实例——B1 的教训在竞争里放大了几十倍：网格几十组 ×
    折数几折，共用实例等于把上一组的记忆喂给下一组。
    """
    wanted = frozenset(days)
    flat = [
        bar
        for code in sorted(bars_by_code)
        for bar in bars_by_code[code]
        if bar.trade_date in wanted
    ]
    strategy: Strategy = slot.build(dict(params))
    result = run(flat, strategy=strategy, assumptions=assumptions, bands=bands)
    metrics = measure(result)
    by_code: dict[str, int] = {}
    for trip in result.round_trips:
        by_code[trip.code] = by_code.get(trip.code, 0) + 1
    return Score(
        slot=slot.name,
        title=slot.title,
        params=dict(params),
        phase=phase,
        metrics=metrics,
        expectancy_pct=_expectancy_pct(result.round_trips),
        streak=metrics.max_loss_streak,
        trips_by_code=by_code,
    )


def merge(scores: Sequence[Score]) -> Score:
    """跨折/跨票合并：计数相加，连败取各段最大（保守侧），期望百分比按全部回合重平均。

    期望百分比的"重平均"是按段的均值再平均——各段回合数不同时不严格等于全回合均值，
    但段间回合数在竞争量级下同数量级，而这个误差方向固定（不偏向任何候选），可接受；
    精确合并需要把每笔回合随身携带，报告的体积不值得。
    """
    if not scores:
        raise ValueError("没有可合并的成绩段")
    head = scores[0]
    metrics = head.metrics
    for other in scores[1:]:
        metrics = Metrics(
            trips=metrics.trips + other.metrics.trips,
            wins=metrics.wins + other.metrics.wins,
            losses=metrics.losses + other.metrics.losses,
            even=metrics.even + other.metrics.even,
            win_sum=metrics.win_sum + other.metrics.win_sum,
            loss_sum=metrics.loss_sum + other.metrics.loss_sum,
            trip_pnl=metrics.trip_pnl + other.metrics.trip_pnl,
            t_out_fills=metrics.t_out_fills + other.metrics.t_out_fills,
            t_in_fills=metrics.t_in_fills + other.metrics.t_in_fills,
            flew=metrics.flew + other.metrics.flew,
            added=metrics.added + other.metrics.added,
            max_loss_streak=max(metrics.max_loss_streak, other.metrics.max_loss_streak),
            final_pnl=metrics.final_pnl + other.metrics.final_pnl,
            unrealized=metrics.unrealized + other.metrics.unrealized,
            base_pnl=metrics.base_pnl + other.metrics.base_pnl,
        )
    pcts = [s.expectancy_pct for s in scores if s.expectancy_pct is not None]
    return Score(
        slot=head.slot,
        title=head.title,
        params=head.params,
        phase=head.phase,
        metrics=metrics,
        expectancy_pct=sum(pcts) / len(pcts) if pcts else None,
        streak=metrics.max_loss_streak,
        trips_by_code={
            code: sum(s.trips_by_code.get(code, 0) for s in scores)
            for code in sorted({c for s in scores for c in s.trips_by_code})
        },
    )


def best(scores: Sequence[Score]) -> Score:
    """一槽位内的参数选择（07 §四-2 初筛的"选高原"侧）：先看胜率，平手看期望。

    平手判据写成 `>` 严格比较 + 稳定排序输入：网格遍历顺序固定，平手时留在先声明的那组
    ——参数等价时不给后到的"新鲜感"加分。
    """
    ranked = sorted(
        scores,
        key=lambda s: (
            s.metrics.win_rate if s.metrics.win_rate is not None else -1.0,
            s.expectancy_pct if s.expectancy_pct is not None else float("-inf"),
        ),
        reverse=True,
    )
    return ranked[0]
