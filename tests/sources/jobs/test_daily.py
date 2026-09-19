"""每日盘后采集任务（01 路线图 Step 2 第 4 项，验收 4）。

重心是三件事，都不是"能不能跑通"：

1. 故障注入时**重试并最终告警**——断网不能表现为"今天没数据也算正常"。
2. 少掉的票必须可见。抓取失败的票若悄悄从分母里消失，健康分反而上升，那是最坏的失真。
3. 两日一批换来的 R007 要真的能开火，而日报的分母仍是报告日那天。

网络层不在这里测：`fetch` 是参数，所以全部测试离线可跑（03 §二 L2）。
"""

from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Listing, SecurityMaster
from zhixing_quant.quality.daily_report import DETAIL_TOP
from zhixing_quant.quality.engine import GateOutcome
from zhixing_quant.quality.facts import Violation
from zhixing_quant.sources.jobs import daily as job
from zhixing_quant.sources.rows import Pair, SourceSchemaError
from zhixing_quant.storage import query
from zhixing_quant.storage.write import WriteReport, store_bars

DAY = date(2024, 1, 3)
PREV = date(2024, 1, 2)

CALENDAR = TradingCalendar([PREV, DAY, date(2024, 1, 4), date(2024, 1, 5)])  # 周四、周五
#: 不关心落盘的测试用这个 store：它们问的是判定与告警，不是磁盘上有没有文件。
NO_LANDING = WriteReport(0, 0, 0, 0)
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


def two_days(previous: float, today: float) -> Pair:
    raw = bars(PREV, previous) + bars(DAY, today)
    return raw, raw


def _no_landing(*_rows: Any) -> WriteReport:
    return NO_LANDING


def landing(root: Path) -> job.Store:
    """把落盘指向 tmp 的真存储层：这些测试要验的是"进了盘"，不是"进了哪块盘"。"""
    return partial(store_bars, root=root)


def test_retry_succeeds_without_alerting() -> None:
    """限流后第二次就成：惊动人一次的告警，第二天就不会有人再看了。"""
    failures = [1]

    def flaky() -> Pair:
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

    def always_fail() -> Pair:
        raise TimeoutError("超时")

    got = job.with_retry(always_fail, "600519", attempts=3, backoff=2.0, sleep=slept.append)
    assert slept == [2.0, 4.0]
    assert isinstance(got, job.Skipped) and got.reason.startswith("TimeoutError:")


def test_the_last_attempt_alerts_once_and_names_the_symbol() -> None:
    """验收 4 的"最终告警"：重试次数、最后错误、后果都要在正文里，不是一句"失败了"。"""
    alerts: list[tuple[str, str]] = []

    def failing() -> Pair:
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

    def spy(symbol: str, start: date, end: date) -> Pair:
        asked.append((symbol, start, end))
        return two_days(100.0, 101.0)

    job.collect(DAY, ["600519"], fetch=spy, store=_no_landing, master=MASTER, calendar=CALENDAR)
    assert asked == [("600519", PREV, DAY)]


def test_r007_actually_fires_on_a_two_day_batch() -> None:
    """证明两日批不是纸面设计：昨收 100、今开 200 必须被 R007 拦下。"""
    fetch = lambda _s, _a, _b: (  # noqa: E731
        bars(PREV, 100.0) + bars(DAY, 200.0),
        bars(PREV, 100.0) + bars(DAY, 200.0),
    )
    outcomes, skipped, _ = job.collect(
        DAY, ["600519"], fetch=fetch, store=_no_landing, master=MASTER, calendar=CALENDAR
    )
    assert skipped == []
    quarantined = outcomes[0].quarantined
    assert len(quarantined) == 1
    assert "R007" in quarantined[0].rule_ids


def test_a_skipped_symbol_does_not_lose_the_others() -> None:
    """一只票的网络失败牵连其余几千只，是采集任务最不能有的性质。"""

    def fetch(symbol: str, _start: date, _end: date) -> Pair:
        if symbol == "bad":
            raise ConnectionError("断网")
        return two_days(100.0, 101.0)

    outcomes, skipped, _ = job.collect(
        DAY,
        ["bad", "600519"],
        fetch=fetch,
        store=_no_landing,
        master=MASTER,
        calendar=CALENDAR,
        attempts=1,
        sleep=lambda _s: None,
        alert=lambda _t, _b: None,
    )
    assert [(s.symbol, s.reason) for s in skipped] == [("bad", "ConnectionError: 断网")]
    assert [o.source for o in outcomes] == ["akshare_daily"]


def test_publish_writes_the_report_and_the_scores_log(tmp_path: Path) -> None:
    outcomes, _, landed = job.collect(
        DAY,
        ["600519"],
        fetch=lambda *_a: two_days(100.0, 101.0),
        store=landing(tmp_path),
        master=MASTER,
        calendar=CALENDAR,
    )
    report, body = job.publish(DAY, outcomes, landed=landed, directory=tmp_path)
    assert (tmp_path / "2024-01-03.md").read_text(encoding="utf-8") == body
    assert report.source("akshare_daily").total == 1  # 分母是报告日那天，两日批被裁过
    assert (tmp_path / "scores.csv").is_file()


def test_the_failure_list_is_capped_but_still_counts_everything(tmp_path: Path) -> None:
    """三千只票全列出来等于把日报撑爆；只列前十但报总数，才既看得清又不撒谎。"""
    skipped = [job.Skipped(symbol=f"{i:06d}", reason="断网") for i in range(DETAIL_TOP + 3)]
    _, body = job.publish(DAY, [], landed=NO_LANDING, skipped=skipped, directory=tmp_path)
    assert "重试后仍失败 13 只" in body
    assert body.count("：断网") == DETAIL_TOP
    assert "其余 3 只同因" in body


def test_run_uses_the_master_universe_and_honours_the_limit(tmp_path: Path) -> None:
    asked: list[str] = []

    def fetch(symbol: str, _start: date, _end: date) -> Pair:
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
        store=_no_landing,
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

    def fetch(_s: str, _a: date, _b: date) -> Pair:
        raise ConnectionError("源不可用")

    result = job.run(
        DAY,
        fetch=fetch,
        store=_no_landing,
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


def test_a_fatal_batch_alerts_even_when_the_day_otherwise_worked(
    tmp_path: Path,
) -> None:
    """源给了一只票空表：那天判成了，但这只票一条都没进干净区，必须单独响一声。

    混在"今日无数据"里不行——那只票出问题，和整个源出问题，是两条不同的处置。
    """
    alerts: list[tuple[str, str]] = []

    def half_broken(code: str, _start: date, _end: date) -> Pair:
        if code == "600520":
            return [], []  # 空表：R006 判"本批无有效日期"
        return two_days(100.0, 101.0)

    master = SecurityMaster(
        [
            Listing(code="600519", name="贵州茅台", listed_on=date(2001, 8, 27)),
            Listing(code="600520", name="测试二", listed_on=date(2001, 8, 27)),
        ]
    )
    result = job.run(
        DAY,
        fetch=half_broken,
        store=_no_landing,
        master=master,
        calendar=CALENDAR,
        directory=tmp_path,
        alert=lambda t, b: alerts.append((t, b)),
    )
    assert result.ok and result.fatal
    title, body = alerts[0]
    assert title == "整批拒收：2024-01-03"
    # 数量必须在正文里："一只出问题"和"全部出问题"差三个数量级，处置也完全不同
    assert body.startswith("1 / 2 只票的批次被 FATAL 拦下")


def test_a_broken_source_stops_instead_of_grinding_the_whole_pool() -> None:
    """连败到熔断线就收工：4430 只 × 3 次 × 指数退避等于用十几个小时撞一堵墙。

    免费源把这种撞法当作攻击，代价是用户的 IP。收工不等于掩盖——剩下的票照样进日报的
    失败清单，只是原因写的是"没再抓取"而不是"抓不到"。
    """
    asked: list[str] = []

    def dead(code: str, _start: date, _end: date) -> Pair:
        asked.append(code)
        raise ConnectionError("源不可用")

    symbols = [f"{600000 + i}" for i in range(10)]
    outcomes, skipped, _ = job.collect(
        DAY,
        symbols,
        fetch=dead,
        store=_no_landing,
        master=MASTER,
        calendar=CALENDAR,
        attempts=1,
        breaker=2,
        sleep=lambda _s: None,
        alert=lambda _t, _b: None,
    )
    assert asked == symbols[:2]
    assert outcomes == []
    assert [s.reason for s in skipped[:2]] == ["ConnectionError: 源不可用"] * 2
    # 熔断后没去抓的票也要在清单上，但原因必须写成"没试"而不是"抓不到"
    assert [s.symbol for s in skipped[2:]] == symbols[2:]
    assert all("熔断" in s.reason and "ConnectionError" not in s.reason for s in skipped[2:])


def test_a_success_resets_the_breaker_counter() -> None:
    """熔断数的是**连**败：一只只坏、隔一只好，那是零散故障，源还活着。

    不清零的话，"每三只里坏一只"的池子会在第 20 只停摆，而当天其实一只都没漏判。
    """

    def flaky(code: str, _start: date, _end: date) -> Pair:
        if code in ("600511", "600513"):
            raise ConnectionError("源不可用")
        return two_days(100.0, 101.0)

    outcomes, skipped, _ = job.collect(
        DAY,
        ["600511", "600512", "600513", "600514"],
        fetch=flaky,
        store=_no_landing,
        master=MASTER,
        calendar=CALENDAR,
        attempts=1,
        breaker=2,
        sleep=lambda _s: None,
        alert=lambda _t, _b: None,
    )
    assert len(outcomes) == 2 and len(skipped) == 2  # 四只全都试过


def test_the_failure_section_tells_grabbed_and_never_tried_apart(tmp_path: Path) -> None:
    """两种失败在日报上必须分开：4000 只"没试"混进 30 只"抓不到"，看的人只会修错地方。"""
    skipped = [
        job.Skipped(symbol="600519", reason="ConnectionError: 断网"),
        job.Skipped(symbol="600520", reason="熔断：源连续失败，未再抓取"),
    ]
    _, body = job.publish(DAY, [], landed=NO_LANDING, skipped=skipped, directory=tmp_path)
    assert "重试后仍失败 1 只，熔断后没再抓取 1 只" in body


# --- 干净区接线（Step 3b）-------------------------------------------------------


def test_the_rows_that_passed_the_gate_reach_the_clean_zone(tmp_path: Path) -> None:
    """判成 ≠ 落盘：这一条钉的是"门禁放行的那些行真的进了盘"，含上一日那一行。

    上一日的行也落：它是 R007 的证据，而断点续传时它少一次联网请求。日报的分母仍只有
    报告日（`for_day` 裁过），所以这里的行数是 2 而不是 1。
    """
    outcomes, skipped, landed = job.collect(
        DAY,
        ["600519"],
        fetch=lambda *_a: two_days(100.0, 101.0),
        store=landing(tmp_path),
        master=MASTER,
        calendar=CALENDAR,
    )
    assert not skipped and outcomes[0].total == 1
    assert (landed.added, landed.partitions) == (2, 1)
    stored = query.read_bars("600519", PREV, DAY, root=tmp_path)
    assert [b.trade_date for b in stored] == [PREV, DAY]


def test_a_rerun_of_the_same_day_leaves_the_disk_alone(tmp_path: Path) -> None:
    """断点续传在任务层的形状：重跑判成同样的行，落盘账上是 +0 且一个文件都没重写。

    存储层自己测过幂等，这里要的是"接线之后它仍然幂等"——中间多出来的那道 `clean_zone`
    筛选若哪天改成连隔离区的行一起落盘，第一条重跑就会报出 rewritten>0。
    """
    fetch = lambda *_a: two_days(100.0, 101.0)  # noqa: E731
    store = landing(tmp_path)
    job.collect(DAY, ["600519"], fetch=fetch, store=store, master=MASTER, calendar=CALENDAR)
    _, _, again = job.collect(
        DAY, ["600519"], fetch=fetch, store=store, master=MASTER, calendar=CALENDAR
    )
    assert again.added == 0 and again.repaired == 0 and again.rewritten == 0


def test_a_fatal_batch_reaches_nothing_but_the_report(tmp_path: Path) -> None:
    """整批被 FATAL 拦下的票一行都不进干净区：干净区的定义就是"判过了"。

    它同时是 04 §一 那条不变量的接线证明——`clean_zone` 在 FATAL 时为空，而日报照样
    记下这一笔，两处都不能假装今天判成过。
    """
    _, _, landed = job.collect(
        DAY,
        ["600519"],
        fetch=lambda *_a: ([], []),
        store=landing(tmp_path),
        master=MASTER,
        calendar=CALENDAR,
    )
    assert landed.partitions == 0
    assert not list((tmp_path / "data").rglob("*.parquet"))


def test_the_report_says_what_landed_and_says_it_again_on_a_rerun(tmp_path: Path) -> None:
    """日报里"判成多少行"与"进干净区多少行"是两个数，重跑时后者会让前者看起来矛盾。

    不写明"已是最新"，第二天的运维就会把 +0 读成落盘坏了。
    """
    fetch = lambda *_a: two_days(100.0, 101.0)  # noqa: E731
    first = job.run(
        DAY,
        fetch=fetch,
        store=landing(tmp_path),
        master=MASTER,
        calendar=CALENDAR,
        symbols=["600519"],
        directory=tmp_path / "reports",
    )
    assert "- 进干净区：新增 2 行、改写 0 行，重写 1/1 个分区文件" in first.markdown
    second = job.run(
        DAY,
        fetch=fetch,
        store=landing(tmp_path),
        master=MASTER,
        calendar=CALENDAR,
        symbols=["600519"],
        directory=tmp_path / "reports",
    )
    assert "行数 1" in second.markdown  # 判成的行数是报告日那一天的 1 行
    assert "进干净区：+0 行" in second.markdown and "已是最新" in second.markdown


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
