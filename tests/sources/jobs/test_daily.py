"""每日盘后采集任务（01 路线图 Step 2 第 4 项，验收 4）。

重心是三件事，都不是"能不能跑通"：

1. 故障注入时**重试并最终告警**——断网不能表现为"今天没数据也算正常"。
2. 少掉的票必须可见。抓取失败的票若悄悄从分母里消失，健康分反而上升，那是最坏的失真。
3. 两日一批换来的 R007 要真的能开火，而日报的分母仍是报告日那天。

网络层不在这里测：`fetch` 是参数，所以全部测试离线可跑（03 §二 L2）。
"""

from datetime import date
from pathlib import Path

import pytest

from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Listing, SecurityMaster
from zhixing_quant.quality.daily_report import DETAIL_TOP
from zhixing_quant.quality.engine import GateOutcome
from zhixing_quant.quality.facts import Violation
from zhixing_quant.sources.jobs import daily as job
from zhixing_quant.sources.rows import SourceSchemaError

DAY = date(2024, 1, 3)
PREV = date(2024, 1, 2)

CALENDAR = TradingCalendar([PREV, DAY, date(2024, 1, 4), date(2024, 1, 5)])  # 周四、周五
MASTER = SecurityMaster([Listing(code="600519", name="贵州茅台", listed_on=date(2001, 8, 27))])


def bars(day: date, close: float) -> list[dict[str, object]]:
    """两天一根干净的K线：OHLC 全等，只有收盘在动，好让 R007 单独成为变量。"""
    return [
        {
            "date": day.isoformat(),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": 1000.0,
            "amount": close * 1000,
        }
    ]


def two_days(previous: float, today: float) -> job.Pair:
    raw = bars(PREV, previous) + bars(DAY, today)
    return raw, raw


def test_retry_succeeds_without_alerting() -> None:
    """限流后第二次就成：惊动人一次的告警，第二天就不会有人再看了。"""
    failures = [1]

    def flaky() -> job.Pair:
        if failures:
            failures.pop()
            raise ConnectionResetError("连接重置")
        return two_days(100.0, 101.0)

    slept: list[float] = []
    alerts: list[str] = []
    got = job.with_retry(
        flaky, "600519", sleep=slept.append, alert=lambda title, _b: alerts.append(title)
    )
    assert not isinstance(got, job.Skipped)
    assert slept == [5.0]  # 只等一次，退避基数
    assert alerts == []


def test_backoff_doubles_between_attempts() -> None:
    """免费源的失败大多是限流：等固定秒数等于用同样的节奏再撞三次。"""
    slept: list[float] = []

    def always_fail() -> job.Pair:
        raise TimeoutError("超时")

    got = job.with_retry(always_fail, "600519", attempts=3, backoff=2.0, sleep=slept.append)
    assert slept == [2.0, 4.0]
    assert isinstance(got, job.Skipped) and got.reason.startswith("TimeoutError:")


def test_the_last_attempt_alerts_once_and_names_the_symbol() -> None:
    """验收 4 的"最终告警"：重试次数、最后错误、后果都要在正文里，不是一句"失败了"。"""
    alerts: list[tuple[str, str]] = []

    def failing() -> job.Pair:
        raise ConnectionError("断网")

    got = job.with_retry(
        failing,
        "600519",
        attempts=2,
        sleep=lambda _s: None,
        alert=lambda t, b: alerts.append((t, b)),
    )
    assert isinstance(got, job.Skipped) and got.symbol == "600519"
    assert len(alerts) == 1
    title, body = alerts[0]
    assert title == "抓取失败：600519"
    assert "重试 2 次" in body and "ConnectionError: 断网" in body


@pytest.mark.parametrize(
    ("today", "expected"),
    [(DAY, DAY), (date(2024, 1, 4), date(2024, 1, 4)), (date(2024, 1, 6), date(2024, 1, 5))],
)
def test_the_target_day_is_the_latest_trading_day(today: date, expected: date) -> None:
    """17:00 在周末也要抓周五的数据：抓不到"今天的K线"不是不干的理由。"""
    assert job.last_trading_day(CALENDAR, today) == expected


def test_a_calendar_that_cannot_see_recently_is_an_error_not_a_stale_fetch() -> None:
    """判不出就抛：否则任务会"成功地"抓三个月前，而日报看起来一切正常。"""
    with pytest.raises(SourceSchemaError, match="先重抓交易日历快照"):
        job.last_trading_day(TradingCalendar([date(2023, 1, 3)]), date(2024, 1, 3))


def test_the_window_carries_the_previous_trading_day_for_r007() -> None:
    """两日一批是 R007 能开火的前提：昨收只能由批次里上一日的行现算。"""
    asked: list[tuple[str, date, date]] = []

    def spy(symbol: str, start: date, end: date) -> job.Pair:
        asked.append((symbol, start, end))
        return two_days(100.0, 101.0)

    job.collect(DAY, ["600519"], fetch=spy, master=MASTER, calendar=CALENDAR)
    assert asked == [("600519", PREV, DAY)]


def test_r007_actually_fires_on_a_two_day_batch() -> None:
    """证明两日批不是纸面设计：昨收 100、今开 200 必须被 R007 拦下。"""
    fetch = lambda _s, _a, _b: (  # noqa: E731
        bars(PREV, 100.0) + bars(DAY, 200.0),
        bars(PREV, 100.0) + bars(DAY, 200.0),
    )
    outcomes, skipped = job.collect(DAY, ["600519"], fetch=fetch, master=MASTER, calendar=CALENDAR)
    assert skipped == []
    quarantined = outcomes[0].quarantined
    assert len(quarantined) == 1
    assert "R007" in quarantined[0].rule_ids


def test_a_skipped_symbol_does_not_lose_the_others() -> None:
    """一只票的网络失败牵连其余几千只，是采集任务最不能有的性质。"""

    def fetch(symbol: str, _start: date, _end: date) -> job.Pair:
        if symbol == "bad":
            raise ConnectionError("断网")
        return two_days(100.0, 101.0)

    outcomes, skipped = job.collect(
        DAY,
        ["bad", "600519"],
        fetch=fetch,
        master=MASTER,
        calendar=CALENDAR,
        attempts=1,
        sleep=lambda _s: None,
        alert=lambda _t, _b: None,
    )
    assert [(s.symbol, s.reason) for s in skipped] == [("bad", "ConnectionError: 断网")]
    assert [o.source for o in outcomes] == ["akshare_daily"]


def test_publish_writes_the_report_and_the_scores_log(tmp_path: Path) -> None:
    outcomes, _ = job.collect(
        DAY, ["600519"], fetch=lambda *_a: two_days(100.0, 101.0), master=MASTER, calendar=CALENDAR
    )
    report, body = job.publish(DAY, outcomes, directory=tmp_path)
    assert (tmp_path / "2024-01-03.md").read_text(encoding="utf-8") == body
    assert report.source("akshare_daily").total == 1  # 分母是报告日那天，两日批被裁过
    assert (tmp_path / "scores.csv").is_file()


def test_the_failure_list_is_capped_but_still_counts_everything(tmp_path: Path) -> None:
    """三千只票全列出来等于把日报撑爆；只列前十但报总数，才既看得清又不撒谎。"""
    skipped = [job.Skipped(symbol=f"{i:06d}", reason="断网") for i in range(DETAIL_TOP + 3)]
    _, body = job.publish(DAY, [], skipped=skipped, directory=tmp_path)
    assert "重试后仍失败 13 只" in body
    assert body.count("：断网") == DETAIL_TOP
    assert "其余 3 只同因" in body


def test_run_uses_the_master_universe_and_honours_the_limit(tmp_path: Path) -> None:
    asked: list[str] = []

    def fetch(symbol: str, _start: date, _end: date) -> job.Pair:
        asked.append(symbol)
        return two_days(100.0, 101.0)

    master = SecurityMaster(
        [
            Listing(code="600519", name="贵州茅台", listed_on=date(2001, 8, 27)),
            Listing(code="600520", name="测试二", listed_on=date(2001, 8, 27)),
        ]
    )
    result = job.run(
        DAY,
        fetch=fetch,
        master=master,
        calendar=CALENDAR,
        limit=1,
        directory=tmp_path,
        alert=lambda _t, _b: None,
    )
    assert asked == ["600519"] and result.pool == 1
    assert result.ok and result.day == DAY


def test_run_that_judges_nothing_alerts_and_is_not_ok(tmp_path: Path) -> None:
    """空报告照样落盘，但必须另响一声：定时任务里"写了文件"和"今天没问题"是两件事。"""
    alerts: list[str] = []

    def fetch(_s: str, _a: date, _b: date) -> job.Pair:
        raise ConnectionError("源不可用")

    result = job.run(
        DAY,
        fetch=fetch,
        master=MASTER,
        calendar=CALENDAR,
        symbols=["600519"],
        directory=tmp_path,
        attempts=1,
        sleep=lambda _s: None,
        alert=lambda t, _b: alerts.append(t),
    )
    assert result.ok is False
    assert "今日无数据：2024-01-03" in alerts
    assert (tmp_path / "2024-01-03.md").is_file()


# --- GateOutcome.for_day --------------------------------------------------------


def _outcome(*, fatal: bool = False) -> GateOutcome:
    return GateOutcome(
        source="akshare_daily",
        total=2,
        accepted=(),
        quarantined=(),
        warned=(),
        fatal=(_violation(),) if fatal else (),
    )


def _violation() -> Violation:
    return Violation("R010", "fatal", "缺字段")


def test_for_day_keeps_a_fatal_batch_whole() -> None:
    """FATAL 时三个桶都是空的，total 是唯一证据：裁成 0 反而像"那天没数据"。"""
    fatal = _outcome(fatal=True)
    assert fatal.for_day(DAY) is fatal
