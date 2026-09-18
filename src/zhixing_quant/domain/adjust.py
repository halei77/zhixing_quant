"""复权因子与三口径换算（01 Step 1 核心模型；03 §二 L3 复权恒等式的对象）。

口径定义（与 akshare 一致，全项目只有这一处换算式）：

    后复权价 = 不复权价 × 当日因子
    前复权价 = 不复权价 × 当日因子 ÷ 基准因子（基准=区间最后一天，故前复权价尾日等于实价）

因子按"累计"存，不按"当日事件"存：一次分红送转影响其后所有日子，存增量就得在每个
查询里重跑累加，而累加顺序一旦不稳定（并行、字典序），同一只票两个口径就能差出分
——L4.1 确定性测试第一个抓的就是这种。

float 往返不闭合（乘除顺序影响末位），所以还原性质用相对容差判定而不是 ==。写死
容差的位置只有 `REL_TOL` 一处，属性测试也读它，两边不会漂成两个数。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

REL_TOL = 1e-12
"""往返还原的相对容差。03 §二 L1 的 1e-8 是对照外部库的口径，内部恒等式没理由放宽到那里。"""


class AdjustError(ValueError):
    pass


@dataclass(frozen=True)
class AdjustmentFactor:
    """某只票在某日的累计复权因子。"""

    code: str
    effective_on: date
    factor: float

    def __post_init__(self) -> None:
        # 因子必须是"有限的正数"。NaN 过不了 `<= 0`（任何比较都是假），inf 过得了 `> 0`，
        # 两者都能造出一个"合法的因子对象"，然后让复权价默默变成 NaN/inf。
        # NaN 的入口是 CSV 空值，这条判断不是理论防御。
        if not math.isfinite(self.factor) or self.factor <= 0:
            raise AdjustError(f"{self.code}@{self.effective_on} 因子必须为正，收到 {self.factor}")


def sorted_factors(factors: Sequence[AdjustmentFactor]) -> tuple[AdjustmentFactor, ...]:
    return tuple(sorted(factors, key=lambda f: f.effective_on))


def factor_on(factors: Sequence[AdjustmentFactor], on: date) -> float:
    """on 当天及之前最后一个有效因子；早于首个因子按 1.0（未发生过除权除息）。"""
    current = 1.0
    for item in sorted_factors(factors):
        if item.effective_on > on:
            break
        current = item.factor
    return current


def to_backward(raw: float, factor: float) -> float:
    """不复权 → 后复权。"""
    return raw * factor


def to_forward(raw: float, factor: float, base_factor: float) -> float:
    """不复权 → 前复权。base_factor 取区间末日的累计因子。"""
    if base_factor <= 0:
        raise AdjustError(f"基准因子必须为正，收到 {base_factor}")
    return raw * factor / base_factor


def from_forward(price: float, factor: float, base_factor: float) -> float:
    """前复权 → 不复权（还原方向，属性测试的另一半）。"""
    return price * base_factor / factor


def close_enough(a: float, b: float, rel_tol: float = REL_TOL) -> bool:
    """相对容差相等。相等短路在前，0 只与 0 配，不必为它另开分支。"""
    if a == b:
        return True
    scale = max(abs(a), abs(b))
    return scale > 0 and abs(a - b) / scale <= rel_tol
