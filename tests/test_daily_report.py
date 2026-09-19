"""每日质量日报（04 §四）。

日报是这个项目里唯一"人每天会看"的东西，所以测的重点是**它能不能替人做出判断**：
哪个源掉档了、掉在哪条规则上、那天的原始值是多少、和过去七天比是不是新常态。一份只
罗列数字、要人自己回去查的日报，等于没有——上一版项目死于"看起来一切正常"，日报就是
那件该先响的东西。
"""

from datetime import date
from pathlib import Path

import pytest

from zhixing_quant.domain.bar import BarDraft
from zhixing_quant.quality import daily_report as dr
from zhixing_quant.quality.engine import GateOutcome, QuarantinedRow
from zhixing_quant.quality.facts import Violation
from zhixing_quant.quality.report import DataQualityReport, HealthGrade

DAY = date(2024, 1, 19)


def _draft(**over: object) -> BarDraft:
    base: dict[str, object] = {
        "source": "akshare_daily",
        "symbol": "600519",
        "trade_date": DAY,
        "open": 1715.0,
        "high": 1718.19,
        "low": 1678.1,
        "close": 1685.01,
        "volume": 3215644.0,
        "amount": 5440082548.0,
        "adj_factor": 8.0718,
    }
    return BarDraft(**base, **over)  # type: ignore[arg-type]


def _row(*violations: Violation, **over: object) -> QuarantinedRow:
    default = (Violation("R001", "reject", "high 低于 open/close 较高者"),)
    return QuarantinedRow(draft=_draft(**over), violations=violations or default)


def _outcome(
    source: str = "akshare_daily",
    total: int = 100,
    *,
    quarantined: tuple[QuarantinedRow, ...] = (),
    warned: tuple[QuarantinedRow, ...] = (),
    fatal: tuple[Violation, ...] = (),
) -> GateOutcome:
    return GateOutcome(
        source=source,
        total=total,
        accepted=(),
        quarantined=quarantined,
        warned=warned,
        fatal=fatal,
    )


def _report_of(*outcomes: GateOutcome) -> DataQualityReport:
    return DataQualityReport.from_outcomes(DAY, outcomes)


# --- 明细聚合 -------------------------------------------------------------------


def test_warn_and_reject_of_the_same_rule_are_not_counted_together() -> None:
    """同一条规则配成 warn 与配成 reject 是两件事：混在一起数会把告警算进拒收明细。"""
    outcome = _outcome(
        quarantined=(_row(Violation("R004", "reject", "越界")),),
        warned=(_row(Violation("R004", "warn", "无法判定")),),
    )
    tallies = dr.tally([outcome])
    assert {(t.rule_id, t.level, t.count) for t in tallies} == {
        ("R004", "reject", 1),
        ("R004", "warn", 1),
    }


def test_tallies_are_sorted_by_hit_count_and_capped_at_ten() -> None:
    """04 §四 写的是 Top10：一天的真错可以有几千条，全列出来等于把要看的那几条埋掉。"""
    rows = tuple(
        _row(Violation(f"R{i:03d}", "reject", f"原因 {i}"))
        for i, weight in enumerate([5, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1])
        for _ in range(weight)
    )
    tallies = dr.tally([_outcome(quarantined=rows)])
    assert len(tallies) == dr.DETAIL_TOP
    assert tallies[0].rule_id == "R000" and tallies[0].count == 5
    assert [t.count for t in tallies] == sorted((t.count for t in tallies), reverse=True)


def test_the_top_ten_is_per_source_not_global() -> None:
    """明细按源各取十条：新源不能因为老源报错多就从日报上消失。"""

    def hits() -> tuple[QuarantinedRow, ...]:
        return tuple(_row(Violation(f"R{i:03d}", "reject", f"原因 {i}")) for i in range(15))

    tallies = dr.tally(
        [
            _outcome("akshare_daily", quarantined=hits()),
            _outcome("tencent_daily", quarantined=hits()),
        ]
    )
    sources = [t.source for t in tallies]
    cap = dr.DETAIL_TOP
    assert (sources.count("akshare_daily"), sources.count("tencent_daily")) == (cap, cap)


def test_the_first_reason_of_a_rule_is_kept_as_the_sample_text() -> None:
    """明细要带一句真实原因，否则"R004 × 12"还得人去猜是哪天哪只票。"""
    tallies = dr.tally(
        [_outcome(quarantined=(_row(Violation("R007", "reject", "昨收不一致 -12.3%")),))]
    )
    assert tallies[0].sample == "昨收不一致 -12.3%"


def test_a_batch_fatal_appears_in_the_detail_once() -> None:
    """FATAL 是整批一条，不是每行一条：按行数的话日报会说"5000 次缺交易日"。"""
    tallies = dr.tally([_outcome(fatal=(Violation("R006", "fatal", "缺 1 个交易日"),))])
    assert [(t.rule_id, t.level, t.count) for t in tallies] == [("R006", "fatal", 1)]


# --- 曲线 -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scores", "expected"),
    [
        ([], ""),
        ([100.0], "█"),
        ([0.0], "▁"),
        ([100.0, 100.0, 100.0], "███"),  # 全满分是一条平线，不是"剧烈波动"
        ([50.0], "▅"),
    ],
)
def test_sparkline_normalizes_over_the_whole_score_range(
    scores: list[float], expected: str
) -> None:
    """按 0..100 全量程归一：窗口内 min-max 会把"全员 99 分"画成大起大落，那是误导。"""
    assert dr.sparkline(scores) == expected


def test_sparkline_clamps_instead_of_crashing_on_an_outlier() -> None:
    assert dr.sparkline([140.0, -20.0]) == "█▁"


# --- 正文 -----------------------------------------------------------------------


def test_the_report_shows_every_source_the_gate_ran() -> None:
    """源名必须出现在日报上：04 §四 的第一件事是"今天到底有哪些源跑过"。"""
    outcomes = [
        _outcome("akshare_daily", 5000, quarantined=(_row(),)),
        _outcome("tencent_daily", 5000),
    ]
    body = dr.render(_report_of(*outcomes), outcomes, {})
    for name in ("akshare_daily", "tencent_daily"):
        assert f"| {name} |" in body
    assert "10,000" in body  # 行数千分位：五万行写成 50000 数不快
    assert "降级或停用 0 个" in body


def test_a_clean_day_says_so_instead_of_printing_empty_tables() -> None:
    """没有命中就明说"没有任何规则命中"。空表在日报上和"忘了判"长得一模一样。"""
    body = dr.render(_report_of(_outcome()), [_outcome()], {})
    assert "（今天没有任何规则命中）" in body
    assert "（隔离区今天没有新条目）" in body


def test_a_quarantine_sample_carries_rule_ids_and_raw_values() -> None:
    """04 §四 "附原始值与违规规则编号"：只有原因文案的抽样要人回去查那天的四个数。"""
    row = _row(
        Violation("R001", "reject", "low 高于 open/close 较低者"),
        Violation("R004", "reject", "涨跌幅 +33.00% 越界"),
    )
    outcome = _outcome(quarantined=(row,))
    body = dr.render(_report_of(outcome), [outcome], {})
    assert "600519 2024-01-19 R001/R004" in body
    assert "low 高于 open/close 较低者；涨跌幅 +33.00% 越界" in body
    assert "close=1685.01" in body and "adj_factor=8.0718" in body


def test_samples_are_capped_at_five_rows() -> None:
    outcome = _outcome(quarantined=tuple(_row() for _ in range(9)))
    body = dr.render(_report_of(outcome), [outcome], {})
    assert body.count("\n- 600519 ") == dr.SAMPLE_ROWS


def test_the_trend_section_names_its_source_and_window() -> None:
    outcomes = [_outcome()]
    trend = (
        (date(2024, 1, 17), 92.0, HealthGrade.B),
        (date(2024, 1, 18), 100.0, HealthGrade.A),
    )
    body = dr.render(_report_of(*outcomes), outcomes, {"akshare_daily": trend})
    assert "## 趋势 · akshare_daily" in body
    assert "近 2 日" in body
    assert "01-17 01-18" in body  # 日期只留 MM-DD：七天全写年号会把曲线挤到下一行
    assert "▇█" in body


def test_the_trend_section_shows_the_last_run_not_yesterday() -> None:
    """04 §四 要"等级变化"：拿流水里最近一条比，并把那一条的日期和等级一起打出来。

    写成"较昨日"在周末和采集失败后就是一个假事实——日报上不能出现没人验证过的日期。
    """
    outcomes = [_outcome(total=100, quarantined=tuple(_row() for _ in range(20)))]
    trend = ((date(2024, 1, 12), 80.0, HealthGrade.C),)
    body = dr.render(_report_of(*outcomes), outcomes, {"akshare_daily": trend})
    assert "- 01-12 80.0（C） → 今天 92.0（B）" in body  # 20/100 拒收 = 8 分，B 档


def test_only_a_source_with_history_gets_a_trend_section() -> None:
    """一个源的历史不能顶替另一个源的：拿 akshare 的曲线去说 tencent 的趋势，是编一个假事实。"""
    outcomes = [_outcome("akshare_daily"), _outcome("tencent_daily")]
    trend = ((date(2024, 1, 18), 100.0, HealthGrade.A),)
    body = dr.render(_report_of(*outcomes), outcomes, {"akshare_daily": trend})
    assert "## 趋势 · akshare_daily" in body
    assert "## 趋势 · tencent_daily" not in body


def test_a_source_without_history_gets_no_trend_section() -> None:
    outcomes = [_outcome()]
    body = dr.render(_report_of(*outcomes), outcomes, {"akshare_daily": ()})
    assert "## 趋势" not in body


# --- 分数流水与归档 -------------------------------------------------------------


def test_archiving_uses_the_04_path_and_overwrites_the_same_day(tmp_path: Path) -> None:
    path = dr.archive("第一版", DAY, tmp_path)
    assert path == tmp_path / "2024-01-19.md"
    dr.archive("第二版", DAY, tmp_path)
    assert path.read_text(encoding="utf-8") == "第二版"  # 重跑覆盖，不并排留两份
    assert [p.name for p in tmp_path.iterdir()] == ["2024-01-19.md"]


def test_scores_replace_the_same_day_instead_of_appending(tmp_path: Path) -> None:
    """修完口径重跑是同一天：追加会让曲线上出现两个 01-19，看着像"那天波动大"。"""
    log = tmp_path / "scores.csv"
    dr.append_scores(_report_of(_outcome(total=100, quarantined=(_row(),))), log)
    dr.append_scores(_report_of(_outcome(total=100)), log)
    rows = dr.read_scores(log)
    assert [(r["day"], r["source"], r["score"]) for r in rows] == [
        ("2024-01-19", "akshare_daily", "100.00")
    ]  # 第二次跑干净了，分数就得是新的
    assert rows[0]["total"] == "100"


def test_scores_keep_other_days_and_other_sources(tmp_path: Path) -> None:
    log = tmp_path / "scores.csv"
    dr.append_scores(DataQualityReport.from_outcomes(date(2024, 1, 18), [_outcome("a", 10)]), log)
    dr.append_scores(
        DataQualityReport.from_outcomes(DAY, [_outcome("a", 10), _outcome("b", 10)]), log
    )
    assert [(r["day"], r["source"]) for r in dr.read_scores(log)] == [
        ("2024-01-18", "a"),
        ("2024-01-19", "a"),
        ("2024-01-19", "b"),
    ]


def test_history_reads_the_last_seven_days_but_not_today(tmp_path: Path) -> None:
    """今天不能进自己的基线：否则曲线最后一天同时是基线和本值，掉档就看不出来了。"""
    log = tmp_path / "scores.csv"
    days = [date(2024, 1, d) for d in range(10, 20)]
    for day in days:
        dr.append_scores(DataQualityReport.from_outcomes(day, [_outcome("a", 10)]), log)
    history = dr.history_for(log, "a", DAY, days=7)
    assert [d for d, _, _ in history] == days[-8:-1]  # 01-19 被排除，往前数 7 天
    assert len(history) == 7


def test_history_carries_the_grade_that_was_recorded_that_day(tmp_path: Path) -> None:
    """等级是流水里的一个字段，不是从分数回推的：分档阈值改了，历史不该跟着变。"""
    log = tmp_path / "scores.csv"
    rejects = tuple(_row() for _ in range(20))
    dr.append_scores(_report_of(_outcome(total=100, quarantined=rejects)), log)
    assert dr.history_for(log, "akshare_daily", date(2024, 1, 20)) == ((DAY, 92.0, HealthGrade.B),)


def test_history_of_an_unknown_source_or_a_missing_file_is_empty(tmp_path: Path) -> None:
    """第一天没有历史，是正常状态而不是错误：这里返回空，正文里就不画趋势段。"""
    log = tmp_path / "scores.csv"
    assert dr.history_for(log, "a", DAY) == ()
    dr.append_scores(DataQualityReport.from_outcomes(DAY, [_outcome("a", 10)]), log)
    assert dr.history_for(log, "没有这个源", DAY) == ()
