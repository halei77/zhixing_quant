"""数据源健康分与质量报告（04 §三、§四）。

分数是"停哪个源"的依据，所以这里测的重点不是算得出来，而是**算得照契约**：
扣分权重、分档边界、边界值归哪一档，全部对着 04 §三 的表格逐格核。
一条 REJECT 只扣 0.4 分和扣 40 分，看起来都是"分数低了一点"，实际处置天差地别。
"""

from datetime import date

import pytest

from zhixing_quant.domain.bar import BarDraft
from zhixing_quant.quality.engine import GateOutcome, QuarantinedRow
from zhixing_quant.quality.facts import Violation
from zhixing_quant.quality.report import (
    FATAL_PENALTY,
    REJECT_WEIGHT,
    WARN_WEIGHT,
    DataQualityReport,
    HealthGrade,
    SourceMetrics,
)


def _metrics(
    source: str = "ak",
    total: int = 100,
    rejected: int = 0,
    warned: int = 0,
    fatal: bool = False,
) -> SourceMetrics:
    return SourceMetrics(source=source, total=total, rejected=rejected, warned=warned, fatal=fatal)


def test_weights_are_the_04_numbers() -> None:
    """04 §三：`100 - 40×REJECT率 - 20×WARN率 - 40×(FATAL?1:0)`，权重写死在契约里。"""
    assert (REJECT_WEIGHT, WARN_WEIGHT, FATAL_PENALTY) == (40.0, 20.0, 40.0)


@pytest.mark.parametrize(
    ("rejected", "warned", "fatal", "score", "grade"),
    [
        (0, 0, False, 100.0, HealthGrade.A),
        (10, 0, False, 96.0, HealthGrade.A),  # 100 - 40×0.1
        (5, 0, False, 98.0, HealthGrade.A),  # ≥95 仍是 A
        (0, 50, False, 90.0, HealthGrade.B),  # 100 - 20×0.5
        (15, 0, False, 94.0, HealthGrade.B),  # 差 1 分到 A 档
        (0, 0, True, 60.0, HealthGrade.D),  # 一次 FATAL 直接掉出 C 档
        (30, 20, True, 44.0, HealthGrade.D),  # 04 §三 三项叠加
        (100, 0, True, 20.0, HealthGrade.D),  # 全批拒收 + FATAL：还剩 20 分
        (100, 100, True, 0.0, HealthGrade.D),  # 下限 0，不出负分
    ],
)
def test_score_and_grade_follow_the_contract(
    rejected: int, warned: int, fatal: bool, score: float, grade: HealthGrade
) -> None:
    m = _metrics(rejected=rejected, warned=warned, fatal=fatal)
    assert m.score == pytest.approx(score)
    assert m.grade is grade


#: 分母取一百万：边界值要精确落在线上。分母小了，94.999 那档会被整数取整抹回 95。
TOTAL = 1_000_000


@pytest.mark.parametrize(
    ("score", "grade"),
    [(95.0, HealthGrade.A), (94.999, HealthGrade.B), (85.0, HealthGrade.B), (70.0, HealthGrade.C)],
)
def test_grade_boundaries_favour_the_better_grade(score: float, grade: HealthGrade) -> None:
    """契约写"≥ 95"和"85-95"，重叠的边界值只能归更好的一档，否则同一分数两个说法。"""
    rejected = round((100.0 - score) * TOTAL / REJECT_WEIGHT)
    m = _metrics(total=TOTAL, rejected=rejected)
    assert m.score == pytest.approx(score)
    assert m.grade is grade


def test_a_source_that_delivered_nothing_would_score_perfect() -> None:
    """total=0 时两个率都是 0 → 满分 A。分数本身救不了"源今天一片空白"。

    所以那条线由引擎守：空批判 FATAL（见 test_quality_engine 的 empty batch 用例），
    不给健康分"没有数据可扣"的机会。这里把这个坑钉住，免得以后有人来"修"分数。
    """
    m = _metrics(total=0)
    assert (m.reject_rate, m.warn_rate) == (0.0, 0.0)
    assert m.grade is HealthGrade.A


def test_actions_quote_the_04_table() -> None:
    assert _metrics().action == "正常使用"
    assert _metrics(fatal=True).action == "停用，人工介入排查"
    assert _metrics(rejected=20).action == "关注，周报复盘"
    assert _metrics(rejected=30, warned=20).action == "降级：仅作交叉校验源，不作主数据源"


def test_metrics_are_derived_from_a_gate_outcome() -> None:
    outcome = GateOutcome(
        source="xianyu",
        total=4,
        accepted=(),
        quarantined=(
            QuarantinedRow(_draft_of(), (Violation("R001", "reject", "OHLC 逆序"),)),
            QuarantinedRow(_draft_of(), (Violation("R004", "reject", "越界"),)),
        ),
        warned=(QuarantinedRow(_draft_of(), (Violation("R003", "warn", "零成交"),)),),
        fatal=(),
    )
    m = SourceMetrics.from_outcome(outcome)
    assert (m.source, m.total, m.rejected, m.warned, m.fatal) == (
        "xianyu",
        4,
        2,
        1,
        False,
    )
    assert m.score == pytest.approx(100 - 40 * 0.5 - 20 * 0.25)


def _draft_of() -> BarDraft:
    return BarDraft(symbol="600519")


def _outcome(
    source: str, total: int, *, rejected: int = 0, warned: int = 0, fatal: bool = False
) -> GateOutcome:
    """按计数造一批结果：健康分只读源名、分母和三个桶的长度，不读桶里的具体内容。"""
    return GateOutcome(
        source=source,
        total=total,
        accepted=(),
        quarantined=tuple(
            QuarantinedRow(_draft_of(), (Violation("R001", "reject", "脏"),))
            for _ in range(rejected)
        ),
        warned=tuple(
            QuarantinedRow(_draft_of(), (Violation("R003", "warn", "待核"),)) for _ in range(warned)
        ),
        fatal=(Violation("R010", "fatal", "缺字段"),) if fatal else (),
    )


# --- 一日一份报告（04 §四）--------------------------------------------------------


DAY = date(2024, 1, 3)


def test_the_day_report_is_assembled_from_every_sources_outcome() -> None:
    """04 §四：日报一日一份、按源分行。`from_outcomes` 是 Step 2 采集唯一的入口，
    它没有测试就等于"多源汇总"这条路径从来没人验过。
    """
    report = DataQualityReport.from_outcomes(
        DAY, [_outcome("ak", 100, warned=2), _outcome("xianyu", 100, rejected=40)]
    )
    assert report.day == DAY
    assert [m.source for m in report.metrics] == ["ak", "xianyu"]
    assert [m.score for m in report.metrics] == pytest.approx([99.6, 84.0])
    assert report.total_rows == 200
    assert report.worst is not None and report.worst.source == "xianyu"
    assert [m.source for m in report.blocked] == ["xianyu"]


def test_the_same_source_in_several_batches_is_one_row() -> None:
    """按票分批跑门禁是真会发生的（分片抓、失败重跑半批）。并列两行时 `source()` 只返回
    第一条，那个源的"总条数"就成了半个批次的数；分数流水按 (日期, 源) 覆盖，两行同键
    等于其中一行的分数凭空消失。真数据第一次跑就撞上了这个。
    """
    report = DataQualityReport.from_outcomes(
        DAY, [_outcome("ak", 100, rejected=50), _outcome("ak", 10000, rejected=50)]
    )
    assert [m.source for m in report.metrics] == ["ak"]
    metrics = report.source("ak")
    assert (metrics.total, metrics.rejected, metrics.warned) == (10100, 100, 0)
    # 比率合并后重算 = 99.6；两个分数（80.0 / 99.8）取平均会得出 89.9，那是假故障。
    assert metrics.score == pytest.approx(99.604)
    assert metrics.grade is HealthGrade.A


def test_a_fatal_in_any_batch_of_a_source_marks_the_whole_source() -> None:
    report = DataQualityReport.from_outcomes(
        DAY, [_outcome("ak", 10), _outcome("ak", 10, fatal=True)]
    )
    assert report.source("ak").fatal is True
    """调度器挂了、一个源都没跑：报告得是空的，不能凭空给个"今天满分"。

    空报告的分母为 0，`worst is None`；04 §三 的"当天没数据"这条线由引擎的空批
    FATAL 守（见 test_quality_engine），这里只保证报告不替它圆场。
    """
    report = DataQualityReport.from_outcomes(DAY, [])
    assert report.metrics == ()
    assert report.total_rows == 0
    assert report.worst is None
    assert report.blocked == ()


@pytest.fixture
def report() -> DataQualityReport:
    return DataQualityReport(
        day=DAY,
        metrics=(
            _metrics("ak", total=100, warned=2),
            _metrics("xianyu", total=100, rejected=40),
            _metrics("push2", total=100, rejected=90, warned=5),
        ),
    )


def test_report_indexes_by_source(report: DataQualityReport) -> None:
    assert report.source("ak").grade is HealthGrade.A
    assert report.total_rows == 300


def test_unknown_source_raises_keyerror_not_stopiteration(report: DataQualityReport) -> None:
    """拼错源名要报"没这个源"，而不是让 `next()` 的 StopIteration 冒成"值为空"。"""
    with pytest.raises(KeyError, match="nope"):
        report.source("nope")


def test_worst_names_the_source_to_act_on(report: DataQualityReport) -> None:
    assert report.worst is not None
    assert report.worst.source == "push2"


def test_worst_of_nothing_is_nothing() -> None:
    assert DataQualityReport(day=DAY, metrics=()).worst is None


def test_blocked_lists_degraded_and_stopped_sources(report: DataQualityReport) -> None:
    """04 §三 的处置线：C 档"仅作交叉校验"、D 档"停用"，日报要先点名。"""
    assert [(m.source, m.grade) for m in report.blocked] == [
        ("xianyu", HealthGrade.C),
        ("push2", HealthGrade.D),
    ]
