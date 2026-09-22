"""候选名册与参数网格（07 §四的可执行形态）。

网格大小是**契约**不是实现细节：07 §四 写死"每策略 ≤ 18 组"，§5.3 要多重比较校正——
每多评一组参数，最优者的表观胜率就多膨胀一分。这张表改了，07 的校正前提就变了，
两处要一起改。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from zhixing_quant.backtest.engine import Strategy
from zhixing_quant.strategy.intraday import (
    RandomBaseline,
    RangeBreakout,
    VolumeSpike,
    VWAPReversion,
)


@dataclass(frozen=True)
class Slot:
    """一个候选槽位：名字 + 工厂 + 参数网格。`factory(**params)` 造一个全新实例。"""

    name: str
    title: str
    factory: Callable[..., Strategy]
    grid: tuple[dict[str, Any], ...]

    def build(self, params: dict[str, Any]) -> Strategy:
        return self.factory(**params)


def _product(**axes: tuple[Any, ...]) -> tuple[dict[str, Any], ...]:
    """笛卡尔积成参数组。axes 的顺序就是参数名声明顺序，网格的遍历顺序因此确定。"""
    keys = list(axes)
    out: list[dict[str, Any]] = [{}]
    for key in keys:
        out = [{**row, key: value} for row in out for value in axes[key]]
    return tuple(out)


def slots() -> tuple[Slot, ...]:
    """07 §四的竞争名册。C1 缺席（引擎 View 无跨票注入点，07 §四有说明）。"""
    return (
        Slot(
            "C2",
            "日内 VWAP 均值回归",
            VWAPReversion,
            _product(lookback=(10, 20, 40), z_enter=(1.5, 2.0, 2.5), z_exit=(0.3, 0.5)),
        ),
        Slot(
            "C3",
            "日内区间突破",
            RangeBreakout,
            _product(n_break=(5, 10, 15), confirm=(1, 2)),
        ),
        Slot(
            "C4",
            "量能异动",
            VolumeSpike,
            _product(k=(2.0, 3.0, 4.0), confirm=(1, 2)),
        ),
        Slot(
            "B0",
            "随机对照",
            RandomBaseline,
            _product(every_n=(12, 24, 48)),
        ),
    )
