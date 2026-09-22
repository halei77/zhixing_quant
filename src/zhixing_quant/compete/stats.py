"""竞争统计：精确二项检验与 5.4-1 门槛判定（07 §5.4 数字细目的机器形态）。

数字在 07 定死的理由是"防竞争过程中自我放水"——本模块只做那条公式的忠实翻译，
改任何一个阈值都要先改 07（启用法改 ADR / 用户签字），不许在这里悄悄松。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 07 §5.4-1：净期望下限 +0.05%/回合；胜率对"p=0.5"零假设的单侧精确二项检验 p < 0.05。
EXPECTANCY_FLOOR = 0.0005
ALPHA = 0.05


def binom_tail_ge(wins: int, n: int) -> float:
    """P(X ≥ wins)，X ~ Bin(n, 0.5)，精确算术。

    从尾部那一项（`0.5**n`）往下递推 `t(w) = t(w+1) × (w+1)/(n−w)`，逐项累加——
    不走 `comb(n, w) / 2**n`：comb 到几百就溢 float，递推比值始终在 [0,1] 里。
    """
    if not 0 <= wins <= n:
        raise ValueError(f"wins = {wins} 不在 [0, {n}] 里")
    if n == 0:
        return 1.0
    term = 0.5**n
    total = 0.0
    for w in range(n, wins - 1, -1):
        total += term
        term *= (w) / (n - w + 1)
    return min(1.0, total)


@dataclass(frozen=True)
class Gate:
    """一次 5.4-1 判定的全部中间数——报告要印的不是"过/不过"，是怎么算出来的。"""

    wins: int
    trips: int
    win_rate: float
    binom_p: float
    expectancy_pct: float

    @property
    def ok(self) -> bool:
        return (
            self.win_rate > 0.5 and self.binom_p < ALPHA and self.expectancy_pct >= EXPECTANCY_FLOOR
        )


def gate(wins: int, trips: int, expectancy_pct: float) -> Gate:
    """5.4-1 的三条：胜率 > 50%、二项检验 p < 0.05、净期望 ≥ +0.05%。"""
    if trips <= 0:
        raise ValueError("trips ≤ 0 的成绩单没有资格进门槛：没有回合就直说无回合")
    rate = wins / trips
    return Gate(
        wins=wins,
        trips=trips,
        win_rate=rate,
        binom_p=binom_tail_ge(wins, trips),
        expectancy_pct=expectancy_pct,
    )
