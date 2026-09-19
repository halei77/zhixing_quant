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

网络层不在这里测：`fetch` 是参数，全部测试离线可跑（03 §二 L2）。
"""

from collections.abc import Sequence
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import daily_span, snapshot_root
from zhixing_quant import config
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Listing, SecurityMaster
from zhixing_quant.quality.reconcile import Finding, Recon
from zhixing_quant.sources.jobs import minute as job
from zhixing_quant.sources.jobs import minute_cli
from zhixing_quant.sources.jobs.daily import REASON_BREAKER, REASON_NO_ROWS, Store
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


def frame_today(*_args: object) -> Rows:
    return [bar(MORNING), bar(AFTERNOON, 101.0)]


def frame_of(*rows: dict[str, Any]) -> job.MinuteFetcher:
    def frame(*_args: object) -> Rows:
        return list(rows)

    return frame


def empty_frame(*_args: object) -> Rows:
    return []


def failing(*_args: object) -> Rows:
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

    def frame(_code: str, period: str) -> Rows:
        return [garbled] if period == "5" else frame_today()

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

    未跑的周期报 None 而不是 0 天：`minute_60` 从没落过盘，与"落过但今天没落"在日报上必须是
    两句话，否则第一次跑 60 分钟的那天，旧账会被写成今天没干活。
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
    """一个文件都没有：报"从没落过盘"，不给区间。

    拿报告日当起点会编出一段看起来能回测的区间；那段区间在盘上不存在，回测读它得到的是空表，
    而空表在回测里表现为"这只票那阵子没行情"。
    """
    result = run_job(tmp_path, reconcile=lambda *_a: Recon(1, (), {"minute_60": None}))
    assert "- minute_60：盘上一个文件都没有" in result.markdown
    assert "2024-01-03 .." not in result.markdown


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
