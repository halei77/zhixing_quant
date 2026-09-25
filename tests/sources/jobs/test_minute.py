"""分钟线采集任务（01 Step 4；ADR-0009 决定 6/8）。

这里问的全是"接线的性质"，不是"能不能跑通"：

1. **一个周期一个批、一个 dataset**。5min 与 30min 都有 10:00 那一根，混批会让 R008 把
   后到的判成重复——那是"两条都真的数据存成一条"，盘上看不出来。
2. **范围不猜**。没给 `symbols` 也没给 `limit` 就拒绝启动（决定 8）：全市场三周期是 13290
   请求 / 2–3 小时，替用户按那个钮不在这条管道的职责里。
3. **尾巴的形状**。源没有窗口参数，交回来的永远是一段历史。报告日裁得掉日报的分母，
   裁不掉落盘——尾巴上的每一天都是今天抓到的证据，而它明天就可能滑出窗口（代价三）。
4. **失败要说清是哪一天停的**。`newest` 是唯一能把"源停更了"与"跑早了"分开的东西。
5. **对账读的就是刚落的那块盘**。dataset 名、列序、不复权口径这三件事只有走真存储才测得到，
   而 R011 报的每一句都建立在"它们没错"之上。判据本身在 `test_reconcile.py`，这里不重复。
6. **周期 → 主源的分派 + 降级不许漂**。1 分走 ngw（`ngw_minute_1`）、5/30/60 走 sina
   （`akshare_minute_*`），sina 抓空/抓错降级 ngw（`ngw_minute_*`，#64）：一批一源是 quality
   engine 的强制，源标识混了健康分就说不清扣的是谁；行解析跟着 `Grabbed.via` 走而不是跟着
   周期表走，抓谁解析谁；日报/告警的窗口口径（1970 根 vs 单页 + start 翻页）按同一条分流。

网络层不在这里测：`fetch` 是参数，全部测试离线可跑（03 §二 L2）。
"""

import json
from collections.abc import Sequence
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any, NoReturn

import pytest

from tests.fakes import daily_span, snapshot_root
from zhixing_quant import config
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Listing, SecurityMaster
from zhixing_quant.quality.reconcile import Finding, Recon
from zhixing_quant.sources.akshare import fetch as akshare_fetch
from zhixing_quant.sources.jobs import minute as job
from zhixing_quant.sources.jobs import minute_cli
from zhixing_quant.sources.jobs.daily import REASON_BREAKER, REASON_NO_ROWS, Store
from zhixing_quant.sources.ngw.client import MAX_KLINE_COUNT, NgwClient
from zhixing_quant.sources.rows import Rows
from zhixing_quant.storage import layout, query
from zhixing_quant.storage.quarantine import entries_on, store_quarantined
from zhixing_quant.storage.write import store_bars

PREV = date(2024, 1, 2)
DAY = date(2024, 1, 3)
CALENDAR = TradingCalendar([PREV, DAY, date(2024, 1, 4)])
MASTER = SecurityMaster([Listing(code="600519", name="贵州茅台", listed_on=date(2001, 8, 27))])

MORNING = datetime(2024, 1, 3, 9, 35)
AFTERNOON = datetime(2024, 1, 3, 15, 0)
YESTERDAY = datetime(2024, 1, 2, 15, 0)


def bar(when: datetime, close: float = 100.0) -> dict[str, Any]:
    """一根干净的分钟K线，源侧原样（实测 sina 把每个值都给成字符串）。"""
    return {
        "day": f"{when:%Y-%m-%d %H:%M:%S}",
        "open": str(close),
        "high": str(close * 1.01),
        "low": str(close * 0.99),
        "close": str(close),
        "volume": "1000",
        "amount": str(close * 1000),
    }


def ngw_bar(when: datetime, close_yuan: float = 100.0) -> dict[str, Any]:
    """一根 ngw 侧原样 `timedata` 行（B 刀实测形状）：OHLC 是字符串·分，`curvalue` 是元。"""
    fen = round(close_yuan * 100)
    return {
        "times": f"{when:%Y%m%d%H%M%S}",
        "openp": str(fen),
        "highp": str(round(fen * 1.01)),
        "lowp": str(round(fen * 0.99)),
        "nowv": str(fen),
        "curvol": "1000",
        "curvalue": str(round(close_yuan * 1000)),  # 元 = 价(元) × 量(股)，不除 100
    }


def grabbed(*rows: dict[str, Any], via: str = "akshare") -> job.Grabbed:
    """一批 sina 形状（默认）的源行 + 它由谁交来。`via` 是分派键（#64 起解析只看它）。"""
    return job.Grabbed(list(rows), via)


def frame_today(*_args: object) -> job.Grabbed:
    return grabbed(bar(MORNING), bar(AFTERNOON, 101.0))


def frame_of(*rows: dict[str, Any], via: str = "akshare") -> job.MinuteFetcher:
    def frame(*_args: object) -> job.Grabbed:
        return grabbed(*rows, via=via)

    return frame


def empty_frame(*_args: object) -> job.Grabbed:
    return grabbed(via="akshare")


def failing(*_args: object) -> NoReturn:
    raise ConnectionError("源不可用")


def stores_for(root: Path, *periods: str) -> dict[str, Store]:
    """周期 → 落盘边界。与 `minute_cli` 同一装配：dataset 名只由 `layout` 说一次。"""
    return {
        period: partial(store_bars, dataset=layout.minute_dataset(period), root=root)
        for period in (periods or ("5",))
    }


def run_job(
    tmp_path: Path,
    *,
    fetch: job.MinuteFetcher = frame_today,
    periods: tuple[str, ...] = ("5",),
    symbols: Sequence[str] | None = ("600519",),
    limit: int | None = None,
    breaker: int = 20,
    alerts: list[tuple[str, str]] | None = None,
    reconcile: job.Reconciler | None = None,
) -> job.MinuteResult:
    sink = alerts if alerts is not None else []

    def alert(title: str, body: str) -> None:
        sink.append((title, body))

    return job.run(
        DAY,
        master=MASTER,
        calendar=CALENDAR,
        fetch=fetch,
        stores=stores_for(tmp_path, *periods),
        quarantine=partial(store_quarantined, root=tmp_path),
        directory=tmp_path / "reports" / job.MINUTE_REPORTS,
        symbols=symbols,
        limit=limit,
        reconcile=reconcile,
        breaker=breaker,
        sleep=lambda _s: None,
        alert=alert,
    )


def minute_file(root: Path, period: str) -> bool:
    dataset = layout.minute_dataset(period)
    return layout.partition_path("600519", DAY.year, dataset=dataset, root=root).is_file()


def test_a_clean_batch_lands_in_its_period_dataset_and_reads_back(tmp_path: Path) -> None:
    """采 → 判 → 存 → 查整条链：分钟任务存在的意义就是让尾巴真的进盘、真的查得回来。"""
    result = run_job(tmp_path)
    assert result.ok and not result.fatal
    assert minute_file(tmp_path, "5")
    back = query.read_bars("600519", DAY, DAY, dataset=layout.MINUTE_5, root=tmp_path)
    assert [b.ts for b in back] == [MORNING, AFTERNOON]
    assert [b.source for b in back] == ["akshare_minute_5"] * 2
    assert not list(tmp_path.rglob(f"{layout.DAILY}/*.parquet"))  # 日线目录一格未动


def test_two_periods_neither_share_a_batch_nor_a_file(tmp_path: Path) -> None:
    """同一个收盘时刻在 5min 与 30min 里都存在：混批会把其中一根判成 R008 重复。

    两根都真的数据存成一条，在盘上的表现是"那天少了一根K线"，而回测把它当成那天少成交了一半。
    """
    same = frame_of(bar(MORNING))
    result = run_job(tmp_path, fetch=same, periods=("5", "30"))
    assert [len(o.clean_zone) for o in result.tally.outcomes] == [1, 1]
    for period in ("5", "30"):
        back = query.read_bars(
            "600519", DAY, DAY, dataset=layout.minute_dataset(period), root=tmp_path
        )
        assert [(b.ts, b.source) for b in back] == [(MORNING, f"akshare_minute_{period}")]


def test_the_report_counts_the_day_while_the_whole_tail_is_stored(tmp_path: Path) -> None:
    """尾巴跨两天：落盘两天，报告日只有今天那一根。

    裁反了会错两次：落盘裁成报告日 → 昨天那根滑出窗口就永久没了；分母不裁 → 04 §四 的"那天"
    变成"那几天"，健康分被稀释一倍。
    """
    result = run_job(tmp_path, fetch=frame_of(bar(YESTERDAY), bar(MORNING)))
    assert [o.total for o in result.tally.outcomes] == [1]
    assert result.tally.landed.added == 2
    back = query.read_bars("600519", PREV, DAY, dataset=layout.MINUTE_5, root=tmp_path)
    assert [b.trade_date for b in back] == [PREV, DAY]


def test_a_rejected_row_keeps_its_moment_in_the_quarantine(tmp_path: Path) -> None:
    """拒收的一条要答得出"这一天里的哪一根"：一天 48 根在文件里否则长得一模一样。

    罪名用 R009 而不是"缺字段"：R010 是**批级** FATAL（源改列名时的整批失效长那样），一行缺
    字段会让整批既不进干净区也不进隔离区，于是这一条要测的"逐条证据"根本不会出现。
    """
    result = run_job(tmp_path, fetch=frame_of(bar(AFTERNOON), bar(MORNING)))
    assert [(e.ts, e.rules) for e in entries_on(DAY, root=tmp_path)] == [(MORNING, ("R009",))]
    assert [b.ts for b in result.tally.outcomes[0].clean_zone] == [AFTERNOON]


def test_a_failure_is_labeled_with_its_period(tmp_path: Path) -> None:
    """一只票的两个周期失败 = 清单上两行不同的名字，而不是一行重复两遍。

    日报按条数算"重试后仍失败几只"，不带周期的话一只票会被数成两只，而缺的那只其实好好的。
    """
    result = run_job(tmp_path, fetch=failing, periods=("5", "30"))
    assert [item.symbol for item in result.tally.skipped] == ["600519/5min", "600519/30min"]
    assert all(item.reason.startswith("ConnectionError") for item in result.tally.skipped)
    assert not result.ok
    assert "重试后仍失败 2 只" in result.markdown


def test_an_empty_frame_is_a_skip_not_a_source_verdict(tmp_path: Path) -> None:
    """请求成功、帧为空：进失败清单，不去扣整源的健康分（与日线任务同一条理由）。"""
    result = run_job(tmp_path, fetch=empty_frame)
    assert [(i.symbol, i.reason) for i in result.tally.skipped] == [("600519/5min", REASON_NO_ROWS)]
    assert not result.ok and not result.fatal


def test_the_breaker_counts_requests(tmp_path: Path) -> None:
    """熔断线数的是请求：一只票两个周期，`breaker=2` 就该在第二只票之前收工。

    按票数的话，多周期任务会把"连败 20"理解成 40 个请求，而免费源会把反复撞它当攻击。
    """
    result = run_job(
        tmp_path,
        fetch=failing,
        periods=("5", "30"),
        symbols=("600519", "600825", "300750"),
        breaker=2,
    )
    untouched = [i.symbol for i in result.tally.skipped if i.reason == REASON_BREAKER]
    assert untouched == ["600825/5min", "600825/30min", "300750/5min", "300750/30min"]


def test_running_without_a_scope_is_refused(tmp_path: Path) -> None:
    """决定 8 的落点：不给范围就拒绝启动，而不是"默认跑全市场"，也不是"默认先跑 10 只"。"""
    with pytest.raises(job.ScopeNotConfigured, match="ADR-0009 决定 8"):
        run_job(tmp_path, symbols=None)
    assert list(tmp_path.rglob("*.parquet")) == []  # 响在第一个请求之前


def test_limit_still_takes_the_pool_from_master(tmp_path: Path) -> None:
    """`--limit` 是"主数据当天在册名单的前 N 只"，与 `zx-daily` 同一个语义。"""
    result = run_job(tmp_path, symbols=None, limit=1)
    assert (result.pool, result.ok) == (1, True)


def test_the_alert_says_which_day_the_source_last_spoke(tmp_path: Path) -> None:
    """报告日没数据时，尾巴右端是"源停更了"与"跑早了"的唯一分界。

    分钟线没有回填（决定 6）：这句话决定的是要不要立刻换源，而不是明天再看一眼日报。
    """
    alerts: list[tuple[str, str]] = []
    run_job(tmp_path, fetch=frame_of(bar(YESTERDAY)), alerts=alerts)
    assert alerts and alerts[0][0] == f"分钟线今日无数据：{DAY}"
    assert f"最后一根K线属于 {PREV}" in alerts[0][1]
    # 落盘照旧：报告日没数据，不等于尾巴不该存
    assert query.read_bars("600519", PREV, DAY, dataset=layout.MINUTE_5, root=tmp_path)


def test_a_fatal_batch_alerts_without_voiding_the_other_periods(tmp_path: Path) -> None:
    """一个周期的批次被 FATAL 整批拦下要说出来，但不牵连其余周期：那只票今天没数据，别的票有。

    日期读不出（源改日期格式）就是这种批次：R010 是**批级** FATAL，整批既不进干净区也不进
    隔离区，所以少了这一声响，"5 分钟这一列今天全空"会安静地滑进明天的回测里。
    """
    garbled = {**bar(MORNING), "day": "源改口了"}

    def frame(_code: str, period: str) -> job.Grabbed:
        return grabbed(garbled) if period == "5" else frame_today()

    alerts: list[tuple[str, str]] = []
    result = run_job(tmp_path, fetch=frame, periods=("5", "30"), alerts=alerts)
    assert result.ok and result.fatal
    assert alerts and alerts[0][0] == f"分钟线整批拒收：{DAY}"
    assert "1 / 2" in alerts[0][1]
    assert minute_file(tmp_path, "30") and not minute_file(tmp_path, "5")


def test_the_minute_report_is_archived_beside_the_daily_one(tmp_path: Path) -> None:
    """两个入口写同一天的日报，必须落在不同目录：同目录等于每天互相覆盖一份。"""
    daily = tmp_path / "reports" / f"{DAY.isoformat()}.md"
    daily.parent.mkdir(parents=True)
    daily.write_text("日线日报", encoding="utf-8")
    result = run_job(tmp_path)
    assert daily.read_text(encoding="utf-8") == "日线日报"
    archived = tmp_path / "reports" / job.MINUTE_REPORTS / f"{DAY.isoformat()}.md"
    assert result.markdown in archived.read_text(encoding="utf-8")
    assert (tmp_path / "reports" / job.MINUTE_REPORTS / "scores.csv").is_file()


# --- R011：对账要读的就是刚落的那块盘 --------------------------------------------------


def land_daily(root: Path, **override: Any) -> None:
    """把与 `frame_today` 那两根对得上的一天日线落到日线 dataset。

    高低两个数用 `close * 1.01` 现算而不是写常数：合成侧的极值是这么来的，日线侧写 102.01 会在
    二进制里差出最后一位，于是 `extremes` 每天报一条谁也查不出的假案。
    """
    fields: dict[str, Any] = {
        "open_": 100.0,
        "high": 101.0 * 1.01,
        "low": 100.0 * 0.99,
        "close": 101.0,
        "volume": 2004.0,  # 比合成的 2000 多 0.2%：实测尾差的量级
        "amount": 201400.0,
    }
    fields.update(override)
    store_bars([daily_span(DAY, **fields)], dataset=layout.DAILY, root=root)


def test_the_two_books_on_disk_agree(tmp_path: Path) -> None:
    """采完再对：这条测的是"对账读的确实是刚落的那块盘"，判据本身在 `test_reconcile.py`。

    真存储、真 dataset 名、真列序。用假 reader 测的话，"把 minute_5 写成 minute_30"这种错
    会一路绿到生产——而对账的全部意义就是说真话。
    """
    run_job(tmp_path)
    land_daily(tmp_path)
    recon = job.reconcile_pool(DAY, ["600519"], ["5"], root=tmp_path)
    assert (recon.groups, recon.findings) == (1, ())


def test_reconciliation_compares_raw_prices(tmp_path: Path) -> None:
    """两边都取不复权价。日线带因子 8 也不动它：换一次口径就等于把同一个因子只乘到一本账上。

    这条要防的是"顺手写成 adjust='backward'"——那种改动让 8 元的票看起来对得上 80 元的账吗？
    不会，它让每天的价差栏全是报警，而真断档藏在抽样之下。
    """
    run_job(tmp_path)
    land_daily(tmp_path, factor=8.0)
    assert job.reconcile_pool(DAY, ["600519"], ["5"], root=tmp_path).findings == ()


def test_minutes_without_the_daily_line_are_reported(tmp_path: Path) -> None:
    """日线那天没抓到（分钟线落了两根）：这是日线任务的漏票，报出来才知道两本账少了一本。"""
    run_job(tmp_path)
    recon = job.reconcile_pool(DAY, ["600519"], ["5"], root=tmp_path)
    assert [(f.kind, f.symbol, f.dataset) for f in recon.findings] == [
        ("no_daily", "600519", "minute_5")
    ]


def test_each_period_is_a_group_of_its_own(tmp_path: Path) -> None:
    """组数 = 票 × 周期：日报那句"查了 N 组"的分母要和股票池口径对得上，否则覆盖率无从判读。"""
    run_job(tmp_path, periods=("5", "30", "60"))
    land_daily(tmp_path)
    recon = job.reconcile_pool(DAY, ["600519"], ["5", "30", "60"], root=tmp_path)
    assert recon.groups == 3
    assert recon.findings == ()


def test_the_daily_report_carries_the_reconciliation(tmp_path: Path) -> None:
    """验收 2 的落点：覆盖率与偏差进日报，不是只进返回值。"""
    found = Finding("no_minutes", "600519", DAY, "minute_5", "日线有成交，分钟线一行都没有")
    result = run_job(tmp_path, reconcile=lambda *_a: Recon(2, (found,)))
    assert result.recon is not None and result.recon.groups == 2
    assert "## 分钟 ↔ 日线对账（R011）" in result.markdown
    assert "查了 2 组（票 × 5 分钟）：no_minutes 1" in result.markdown
    assert "- 600519 2024-01-03 minute_5：日线有成交" in result.markdown
    # 这个假边界只回了账、没回深度：那一节照实说"没读到"，不留一个光秃秃的空标题。
    assert "这次没读到" in result.markdown


def test_a_run_without_a_reconcile_boundary_says_so(tmp_path: Path) -> None:
    """没查就写"这次没查"。写成"五种坏法全 0"是把没做报成做完，那是日报最坏的一种假。"""
    result = run_job(tmp_path)
    assert result.recon is None
    assert "这次没查" in result.markdown
    assert "这次没读盘" in result.markdown  # 深度与对账共用同一次读盘，没读就两节都没有


def test_the_sample_is_per_kind_so_a_flood_cannot_hide_a_second_kind(tmp_path: Path) -> None:
    """一类再来一百条也只列三条，另外四种各留自己的三条：全局截断会让断档把价差挤出日报。"""
    many = [Finding("no_minutes", f"60000{i}", DAY, "minute_5", f"断档{i}") for i in range(4)]
    many.append(Finding("price", "600519", DAY, "minute_5", "开盘不相等"))
    result = run_job(tmp_path, reconcile=lambda *_a: Recon(5, tuple(many)))
    assert result.markdown.count("断档") == job.KIND_SAMPLE
    assert "其余 1 条 no_minutes 未列" in result.markdown
    assert "开盘不相等" in result.markdown  # 另一种坏法没有被前一种挤出清单


# --- 盘上深度（07 §5.1 的可回测区间）----------------------------------------------------


def test_the_depth_read_is_the_run_that_just_landed(tmp_path: Path) -> None:
    """深度包含今天落的行：它排在落盘之后，报的才是"明天能回测到哪一天"。

    没跑的周期拿到的是 None，日报上那句"盘上没有一行分钟线"同时 cover 了"从没落过"与"文件在
    而行是零"两种形状——人要分辨就去看一眼目录，报告不必替他分。
    """
    run_job(tmp_path, periods=("5", "30"))
    recon = job.reconcile_pool(DAY, ["600519"], ["5", "60"], root=tmp_path)
    assert recon.covers == {
        layout.minute_dataset("5"): query.Cover(first=DAY, last=DAY, days=1, symbols=1),
        layout.minute_dataset("60"): None,
    }


def test_the_daily_report_carries_the_depth(tmp_path: Path) -> None:
    """区间进日报而不是只进返回值：07 §5.1 说的是"可回测区间要能被查到"，而日报是它的归档处。

    首末之间跨两天、有行一天：`days` 必须是 1（按日历数会把中间那个没跑的日子算成可用样本）。
    """
    cover = query.Cover(first=PREV, last=DAY, days=1, symbols=200)
    result = run_job(tmp_path, reconcile=lambda *_a: Recon(2, (), {"minute_5": cover}))
    assert "## 盘上深度" in result.markdown
    assert "- minute_5：2024-01-02 .. 2024-01-03，1 个有行交易日 × 200 只票" in result.markdown
    assert "起点那天" in result.markdown  # 1970 根窗口切出来的第一天不足全天


def test_an_unlanded_dataset_says_so_instead_of_inventing_a_start(tmp_path: Path) -> None:
    """盘上没有一行可读：报"报不出区间"，不给起止日。

    拿报告日当起点会编出一段看起来能回测的区间；那段区间在盘上不存在，回测读它得到的是空表，
    而空表在回测里表现为"这只票那阵子没行情"。
    """
    result = run_job(tmp_path, reconcile=lambda *_a: Recon(1, (), {"minute_60": None}))
    assert "- minute_60：盘上没有一行分钟线，报不出区间" in result.markdown
    assert "2024-01-03 .." not in result.markdown


# --- 周期 → 源分派：1 → ngw，5/30/60 → sina（一批一源、口径按源分流）---------------------


def test_period_one_lands_as_ngw_minute_1(tmp_path: Path) -> None:
    """`--periods 1` 的行进 minute_1、源标识 `ngw_minute_1`——不是 `akshare_minute_1`。

    单位顺带钉死（B 刀硬约束 4）：分→元后 close=100.0，`curvalue` 不除 100 后 amount=100000.0。
    源标识错了的表现是同一个 minute_1 dataset 里两种 source 混着，健康分从此说不清扣的是谁。
    """
    result = run_job(
        tmp_path,
        fetch=frame_of(ngw_bar(MORNING), ngw_bar(AFTERNOON, 101.0), via=job.VIA_NGW),
        periods=("1",),
    )
    assert result.ok and not result.fatal
    back = query.read_bars("600519", DAY, DAY, dataset=layout.MINUTE_1, root=tmp_path)
    assert [b.ts for b in back] == [MORNING, AFTERNOON]
    assert [b.source for b in back] == ["ngw_minute_1"] * 2
    assert back[0].close == 100.0
    assert back[0].amount == 100000.0  # curvalue 原样是元；除 100 会得到 1000.0
    # 日报的「各源」表给 ngw_minute_1 自己的一行健康分，且不冒出 akshare_minute_1
    assert "| ngw_minute_1 |" in result.markdown
    assert "akshare_minute_1" not in result.markdown
    assert result.report.source("ngw_minute_1").total == 2


def test_a_mixed_run_keeps_one_source_per_dataset(tmp_path: Path) -> None:
    """混周期运行下一批一源（quality engine 的强制）：1 分全 ngw、60 分全 sina，互不串。

    每个 (票 × 周期) 是一个批次，分派按周期走——两种源混进同一批会被 engine 直接拒，而
    分派错了的表现正是混批：门禁响的是"混源"，病根却在接线。
    """

    def fetch(_symbol: str, period: str) -> job.Grabbed:
        return (
            grabbed(ngw_bar(MORNING), via=job.VIA_NGW) if period == "1" else grabbed(bar(MORNING))
        )

    result = run_job(tmp_path, fetch=fetch, periods=("1", "60"))
    one = query.read_bars("600519", DAY, DAY, dataset=layout.MINUTE_1, root=tmp_path)
    sixty = query.read_bars("600519", DAY, DAY, dataset=layout.MINUTE_60, root=tmp_path)
    assert [b.source for b in one] == ["ngw_minute_1"]
    assert [b.source for b in sixty] == ["akshare_minute_60"]
    assert {m.source for m in result.report.metrics} == {"ngw_minute_1", "akshare_minute_60"}
    assert "| ngw_minute_1 |" in result.markdown and "| akshare_minute_60 |" in result.markdown
    assert "akshare_minute_1" not in result.markdown


def test_sina_periods_keep_their_source_id(tmp_path: Path) -> None:
    """回归钉：5/30/60 仍走 sina、源标识一字不改（ADR-0009 决定 1、ADR-0021 决定 2）。"""
    result = run_job(tmp_path, periods=("5", "30", "60"))
    for period in ("5", "30", "60"):
        back = query.read_bars(
            "600519", DAY, DAY, dataset=layout.minute_dataset(period), root=tmp_path
        )
        assert {b.source for b in back} == {f"akshare_minute_{period}"}
    assert "ngw_minute_1" not in result.markdown


def test_the_depth_note_splits_the_window_by_source(tmp_path: Path) -> None:
    """「起点那天不足全天」的窗口口径按源分流：1970 根是 sina 的话，ngw 说的是单页截断。"""
    cover = query.Cover(first=PREV, last=DAY, days=1, symbols=200)
    ngw_only = run_job(
        tmp_path / "ngw",
        periods=("1",),
        fetch=frame_of(ngw_bar(MORNING), via=job.VIA_NGW),
        reconcile=lambda *_a: Recon(1, (), {layout.MINUTE_1: cover}),
    )
    assert "ngw 单页" in ngw_only.markdown
    assert "1970" not in ngw_only.markdown  # sina 的窗口句不许挂到 ngw 的报告上
    sina_only = run_job(
        tmp_path / "sina",
        periods=("60",),
        reconcile=lambda *_a: Recon(1, (), {layout.MINUTE_60: cover}),
    )
    assert "1970 根窗口" in sina_only.markdown
    assert "ngw 单页" not in sina_only.markdown
    both = run_job(
        tmp_path / "both",
        periods=("1", "60"),
        fetch=lambda _s, p: (
            grabbed(ngw_bar(MORNING), via=job.VIA_NGW) if p == "1" else grabbed(bar(MORNING))
        ),
        reconcile=lambda *_a: Recon(2, (), {layout.MINUTE_1: cover, layout.MINUTE_60: cover}),
    )
    assert "1970 根窗口" in both.markdown and "ngw 单页" in both.markdown


def test_the_no_data_alert_splits_the_urgency_by_source(tmp_path: Path) -> None:
    """无数据告警的"漏了有多急"按源说：sina 是"主源滑窗、要人重跑补"，ngw 是"翻页可补、先查源"。

    #64 起两边都"补得回来"，急法却仍不同——区别不在能不能，在要不要人动手、窗口还剩几天。
    """

    def verdict(alerts: list[tuple[str, str]]) -> str:
        # 全失败时第 0 条是 with_retry 的逐票告警，"今日无数据"是收尾那条：按标题取。
        return next(body for title, body in alerts if title.startswith("分钟线今日无数据"))

    ngw_alerts: list[tuple[str, str]] = []
    run_job(tmp_path / "ngw", periods=("1",), fetch=failing, alerts=ngw_alerts)
    ngw_body = verdict(ngw_alerts)
    assert "补得回来" in ngw_body
    assert "1970" not in ngw_body
    sina_alerts: list[tuple[str, str]] = []
    run_job(tmp_path / "sina", periods=("5",), fetch=failing, alerts=sina_alerts)
    sina_body = verdict(sina_alerts)
    assert "1970 根" in sina_body
    assert "主源" in sina_body  # sina 的话要说清这是主源，兜底另有一家
    assert "单页" not in sina_body  # ngw 的翻页句不许挂到 sina 的告警上


# --- live_fetcher：真网络装配的分派（transport 注入，零外发）-----------------------------


def _stub_ngw_client(*responses: dict[str, Any]) -> tuple[NgwClient, list[str]]:
    """可注入 transport 的真 `NgwClient` + 记下每次完整 URL 的账本（03 §二 L2 离线重放）。"""
    script = list(responses)
    calls: list[str] = []

    def transport(url: str, _headers: Any, _timeout: float) -> tuple[int, bytes]:
        calls.append(url)
        assert script, f"脚本用完了还来一发：{url}"
        return 200, json.dumps(script.pop(0)).encode("utf-8")

    client = NgwClient(
        token_path=Path("/nonexistent/ngw.token"),
        interval=0.0,
        sleep=lambda _s: None,
        transport=transport,
    )
    return client, calls


SEARCH_HIT = {
    "stocks": [
        {
            "innercode": "3143",
            "stockcode": "600519",
            "stockname": "贵州茅台",
            "market": "1",
            "boardName": "主板",
            "tagDisplay": "白酒",
        }
    ]
}


def test_live_fetcher_wires_period_one_through_ngw() -> None:
    """1 分路径的真装配：innercode → kline(count=MAX_KLINE_COUNT) → ngw 行，全程零网络。

    分派错了的两种病这里各钉一条：走了 sina（会发 `stock_zh_a_minute`——脚本没有那个应答，
    第一发就炸）和 count 发错（`count=1500` 源侧静默 0 行，硬约束 3）。
    """
    client, calls = _stub_ngw_client(SEARCH_HIT, {"timedata": [ngw_bar(MORNING)]})
    fetch = job.live_fetcher(("1",), client=client)
    grabbed_1 = fetch("600519", "1")
    assert grabbed_1.via == job.VIA_NGW
    assert len(calls) == 2
    assert "homesearch" in calls[0] and "q=600519" in calls[0]
    assert "kline" in calls[1] and f"count={MAX_KLINE_COUNT}" in calls[1]
    drafts = job.drafts_for(grabbed_1, symbol="600519", period="1")
    assert drafts and all(d.source == "ngw_minute_1" for d in drafts)
    assert drafts[0].ts == MORNING


def test_live_fetcher_splits_periods_between_the_two_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """混周期一轮：1 分走注入的 ngw client，60 分走 akshare——两条路各自认领各自的周期。"""
    seen: list[tuple[str, str]] = []

    def fake_minute_frame(code: str, period: str = "5") -> Rows:
        seen.append((code, period))
        return [bar(MORNING)]

    monkeypatch.setattr(akshare_fetch, "minute_frame", fake_minute_frame)
    client, calls = _stub_ngw_client(SEARCH_HIT, {"timedata": [ngw_bar(MORNING)]})
    fetch = job.live_fetcher(("1", "60"), client=client)
    assert fetch("600519", "1").rows
    sixty = fetch("600519", "60")
    assert sixty.via == job.VIA_AKSHARE and sixty.rows == [bar(MORNING)]
    assert seen == [("600519", "60")]  # 60 分一次都没往 ngw 发
    # 1 分那轮只打了两发：innercode 检索 + kline 一页；60 分走的是 akshare，不在这本账上
    assert len(calls) == 2
    assert "homesearch" in calls[0] and "kline" in calls[1]


def test_live_fetcher_never_touches_ngw_while_akshare_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归钉（#64 的"老路径一个字节不变"）：5/30/60 全程有数时，连 NgwClient 都不许构造。

    构造即摸 token 路径、即建锁——纯 sina 的轮次不该有这些副作用，而懒构造坏了的最便宜测法
    就是把构造函数换成炸的：不降级就永远炸不到。
    """

    def boom(**_kw: object) -> NgwClient:
        raise AssertionError("client 不该被构造")

    monkeypatch.setattr(job, "NgwClient", boom)
    monkeypatch.setattr(akshare_fetch, "minute_frame", lambda *_a: [bar(MORNING)])
    fetch = job.live_fetcher(("5", "30", "60"))
    for period in ("5", "30", "60"):
        assert fetch("600519", period).via == job.VIA_AKSHARE


def test_live_fetcher_refuses_period_one_when_its_client_was_never_built() -> None:
    """装配 bug 的兜底：建轮时没给 1 分排 client，抓 1 分要在这里响，不是把行送错解析。"""
    fetch = job.live_fetcher(("5",))
    with pytest.raises(ValueError, match="不含它"):
        fetch("600519", "1")


# --- akshare→ngw 降级（#64）：空帧与异常都兜底，预算封顶，退回主源语义 ---------------------


def _fallback_setup(
    monkeypatch: pytest.MonkeyPatch, *, ak: Rows | Exception | None
) -> tuple[job.MinuteFetcher, list[str]]:
    """装一个「sina 按剧本、ngw 走脚本应答」的 live_fetcher，返回 (fetch, ngw 请求账本)。"""

    def fake_minute_frame(_code: str, _period: str = "5") -> Rows:
        if isinstance(ak, Exception):
            raise ak
        assert ak is not None
        return list(ak)

    monkeypatch.setattr(akshare_fetch, "minute_frame", fake_minute_frame)
    client, calls = _stub_ngw_client(SEARCH_HIT, {"timedata": [ngw_bar(MORNING)]})
    return job.live_fetcher(("5",), client=client), calls


def test_live_fetcher_falls_back_to_ngw_on_an_empty_akshare_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sina 交回空帧（它真会这么干）：同周期降级 ngw，`type=1` 的那页、标 `via=ngw`。"""
    fetch, calls = _fallback_setup(monkeypatch, ak=[])
    got = fetch("600519", "5")
    assert got.via == job.VIA_NGW and len(got.rows) == 1
    assert "homesearch" in calls[0]
    assert "kline" in calls[1] and "type=1" in calls[1]  # 5 分的 ngw type，不是 11
    drafts = job.drafts_for(got, symbol="600519", period="5")
    assert [d.source for d in drafts] == ["ngw_minute_5"]


def test_live_fetcher_falls_back_to_ngw_when_akshare_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sina 抛错（限流/超时/半截 JSON）：一样降级，且错不吞——ngw 也错时错要冒到 with_retry。"""
    fetch, calls = _fallback_setup(monkeypatch, ak=ConnectionError("sina 不理人"))
    got = fetch("600519", "5")
    assert got.via == job.VIA_NGW and len(calls) == 2


def test_live_fetcher_stops_falling_back_at_the_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预算是护栏不是开关：触顶后 sina 的错原样响、空帧原样交——不许 ngw 无声扛整源。"""
    alerts: list[tuple[str, str]] = []

    def fake_minute_frame(_code: str, _period: str = "5") -> Rows:
        raise ConnectionError("sina 整源倒了")

    monkeypatch.setattr(akshare_fetch, "minute_frame", fake_minute_frame)
    client, calls = _stub_ngw_client(SEARCH_HIT, {"timedata": [ngw_bar(MORNING)]})
    fetch = job.live_fetcher(
        ("5",), client=client, fallback_budget=1, alert=lambda *a: alerts.append(a)
    )
    assert fetch("600519", "5").via == job.VIA_NGW  # 第 1 发：预算内的最后一次
    assert [title for title, _b in alerts] == ["分钟线降级预算耗尽"]
    with pytest.raises(ConnectionError):  # 第 2 发：预算耗尽，主源的错交出去
        fetch("600519", "30")
    assert len(calls) == 2  # ngw 只被打了第一发的那两枪


def test_downgrades_are_counted_into_the_tally_and_the_daily_report(tmp_path: Path) -> None:
    """降级要留名：`via=ngw` 的 5 分行落 `minute_5`、源标识 `ngw_minute_5`、日报单列一节。

    1 分的主源本来就是 ngw——它不进这本账，否则每天全池都是"降级"，那节就废了。
    """
    result = run_job(
        tmp_path,
        fetch=frame_of(ngw_bar(MORNING), via=job.VIA_NGW),
        periods=("5",),
    )
    assert result.tally.downgrades == ("600519/5min",)
    back = query.read_bars("600519", DAY, DAY, dataset=layout.MINUTE_5, root=tmp_path)
    assert [b.source for b in back] == ["ngw_minute_5"]
    assert "## 源降级（akshare→ngw）" in result.markdown
    assert "600519/5min" in result.markdown


# --- 命令行入口：只剩"装配对不对"可测 --------------------------------------------------


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个只装了快照的数据根：CLI 读什么、写什么，全都落在 tmp 里。"""
    return snapshot_root(tmp_path, monkeypatch)


def test_the_cli_wires_each_period_to_its_own_dataset(data_root: Path) -> None:
    code = minute_cli.main(
        ["--day", "2024-01-03", "--symbols", "600519", "--periods", "5,60"], fetcher=frame_today
    )
    assert code == 0
    # 默认数据根来自 `ZX_DATA_ROOT`，而分区在它下面再进一层 `data/`：这一条钉的就是 CLI 没有
    # 把分钟线写进日线目录、也没有写到仓库里
    parquet = config.parquet_dir()
    assert minute_file(parquet, "5") and minute_file(parquet, "60")
    assert not list(parquet.rglob(f"{layout.DAILY}/*.parquet"))
    archived = (data_root / "reports" / job.MINUTE_REPORTS / f"{DAY.isoformat()}.md").read_text(
        encoding="utf-8"
    )
    # CLI 真的把对账边界接上了：读的是刚落的这两个 dataset，而盘上没有日线那一本账
    assert "查了 2 组（票 × 5/60 分钟）：no_minutes 0、no_daily 2" in archived


def test_the_cli_refuses_a_period_it_cannot_store() -> None:
    """`--periods 15` 要在校验期就响：源真的会回 15 分钟的数据，而盘上没有对应的 dataset。"""
    with pytest.raises(SystemExit) as stop:
        minute_cli.build_parser().parse_args(["--periods", "15"])
    assert stop.value.code == 2


def test_the_cli_accepts_period_one() -> None:
    """`--periods 1` 是合法周期（minute_1 已注册）：在参数期放行，源分派在 job 层接住。"""
    parsed = minute_cli.build_parser().parse_args(["--periods", "1,60"])
    assert parsed.periods == ("1", "60")


def test_the_cli_refuses_an_empty_period_list() -> None:
    """`--periods ,` 不是"三个都不采"，是打错了：空集合会让这次运行一个请求都不发还报 0。"""
    with pytest.raises(SystemExit) as stop:
        minute_cli.build_parser().parse_args(["--periods", " , "])
    assert stop.value.code == 2


def test_the_cli_exits_two_when_nobody_said_how_big(data_root: Path) -> None:
    """退出码分清"没开始"与"开始了一只没判成"：前者要去问范围，后者要去查网络。"""
    assert minute_cli.main(["--day", "2024-01-03"], fetcher=frame_today) == 2
    assert not list(data_root.rglob("*.parquet"))  # 拒绝得干净：一个字节都没落


def test_the_cli_exits_one_when_nothing_was_judged(data_root: Path) -> None:
    """全失败照样出报告、照样是 1：日报上"今天 0 行"与"今天没跑"必须是两个退出码。

    `--attempts 1` 是不想真等退避：重试路径本身在 `test_daily.py` 测过，这里测的是接线。
    """
    assert (
        minute_cli.main(
            ["--day", "2024-01-03", "--symbols", "600519", "--attempts", "1"], fetcher=failing
        )
        == 1
    )
    archived = data_root / "reports" / job.MINUTE_REPORTS / f"{DAY.isoformat()}.md"
    assert "重试后仍失败 3 只" in archived.read_text(encoding="utf-8")  # 三个周期各一只
