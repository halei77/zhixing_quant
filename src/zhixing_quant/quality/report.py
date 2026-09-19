"""数据源健康分与质量报告模型（04 §三、§四）。

放在 `quality/` 而不是 `domain/`：这份数据是门禁的**产出**，只有门禁会写它；
把它算进通用契约层，就等于承认别的模块也能造健康报告——04 §三 的分数就只有唯一
来源这一个前提。

分档阈值（95/85/70）与扣分权重（40/20/40）都是 04 §三 表里的原文，改动等于改口径，
要连 04 一起改，不在这里单方面调。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from enum import Enum

from zhixing_quant.quality.engine import GateOutcome

REJECT_WEIGHT = 40.0
WARN_WEIGHT = 20.0
FATAL_PENALTY = 40.0


class HealthGrade(Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


#: 分数 → 处置（04 §三 表格第二列）。文案照抄契约，不在代码里重编一套说法。
GRADE_ACTIONS = {
    HealthGrade.A: "正常使用",
    HealthGrade.B: "关注，周报复盘",
    HealthGrade.C: "降级：仅作交叉校验源，不作主数据源",
    HealthGrade.D: "停用，人工介入排查",
}


@dataclass(frozen=True)
class SourceMetrics:
    source: str
    total: int
    rejected: int
    warned: int
    fatal: bool

    @classmethod
    def from_outcome(cls, outcome: GateOutcome) -> SourceMetrics:
        return cls(
            source=outcome.source,
            total=outcome.total,
            rejected=outcome.rejected_count,
            warned=len(outcome.warned),
            fatal=outcome.has_fatal,
        )

    @classmethod
    def from_group(cls, source: str, outcomes: Iterable[GateOutcome]) -> SourceMetrics:
        """同源多批并成一行：计数相加、FATAL 取或。

        先各自 `from_outcome` 再求和，而不是直接读 `outcome.*`：把"哪些字段算 REJECT/WARN"
        的口径写第二遍，就是给下一次改 `from_outcome` 留一个漏改的地方。
        健康分按合并后的比率重算，不是两个分数取平均——取平均会把小批次的错误放大成
        "这个源不行了"。
        """
        parts = [cls.from_outcome(outcome) for outcome in outcomes]
        return cls(
            source=source,
            total=sum(p.total for p in parts),
            rejected=sum(p.rejected for p in parts),
            warned=sum(p.warned for p in parts),
            fatal=any(p.fatal for p in parts),
        )

    @property
    def reject_rate(self) -> float:
        return self.rejected / self.total if self.total else 0.0

    @property
    def warn_rate(self) -> float:
        return self.warned / self.total if self.total else 0.0

    @property
    def score(self) -> float:
        out = (
            100.0
            - REJECT_WEIGHT * self.reject_rate
            - WARN_WEIGHT * self.warn_rate
            - (FATAL_PENALTY if self.fatal else 0.0)
        )
        return max(0.0, out)

    @property
    def grade(self) -> HealthGrade:
        # 04 §三 的 85-95 / 70-85 是闭区间重叠写法；这里按"≥95 A、≥85 B、≥70 C、余 D"
        # 落地，即边界值归入更好的一档。95.0 分判 A 而不是 B，是因为契约写的是"≥ 95"。
        if self.score >= 95:
            return HealthGrade.A
        if self.score >= 85:
            return HealthGrade.B
        if self.score >= 70:
            return HealthGrade.C
        return HealthGrade.D

    @property
    def action(self) -> str:
        return GRADE_ACTIONS[self.grade]


@dataclass(frozen=True)
class DataQualityReport:
    """一日一份（04 §四）。渲染成 Markdown 与归档路径在 Step 2 的日报里做。"""

    day: date
    metrics: tuple[SourceMetrics, ...]

    @classmethod
    def from_outcomes(cls, day: date, outcomes: Iterable[GateOutcome]) -> DataQualityReport:
        """**一个源一行**。同一源分批过门禁（按票分组、失败后重跑半批）要并起来，两个理由：

        - `source()` 按名字返回第一条，并列两行时那个源的"总条数"就成了半个批次的数；
        - `reports/scores.csv` 按 (日期, 源) 覆盖，两行同键等于其中一行的分数凭空消失。

        并的是原始计数，健康分按合并后的比率重算（见 `SourceMetrics.from_group`）。
        """
        groups: dict[str, list[GateOutcome]] = {}
        for outcome in outcomes:
            groups.setdefault(outcome.source, []).append(outcome)
        return cls(
            day=day,
            metrics=tuple(
                SourceMetrics.from_group(source, group) for source, group in groups.items()
            ),
        )

    def source(self, name: str) -> SourceMetrics:
        """按源名取指标。缺源抛 KeyError：让 StopIteration 冒出去的话，
        调用方只看到"没有这条"，看不到"我拼错了源名"。
        """
        for metrics in self.metrics:
            if metrics.source == name:
                return metrics
        raise KeyError(f"报告里没有源 {name}：日报有它才能证明这个源当天真的跑过")

    @property
    def worst(self) -> SourceMetrics | None:
        """分最低的那个源：日报第一屏要先说谁不能用了，而不是罗列一串数字。"""
        return min(self.metrics, key=lambda m: m.score, default=None)

    @property
    def blocked(self) -> Sequence[SourceMetrics]:
        """C 档及以下的源（04 §三 的"降级/停用"线）。"""
        return tuple(m for m in self.metrics if m.grade in (HealthGrade.C, HealthGrade.D))

    @property
    def total_rows(self) -> int:
        return sum(m.total for m in self.metrics)
