"""`zx-backtest` 判的是"装对了没有"（ADR-0010 决定 1、决定 9、决定 10；07 §七 验收 4）。

引擎的算术在 `test_backtest_engine/metrics/report.py` 判过了，这里判的是那些纯模块**接上盘与
台账之后**还成不成立：喂给引擎的是后复权价吗、产物落在哪个目录、台账那一格存了什么、范围没给全
时是不是拒绝启动而不是猜一个。数字一律只断言到"能看出接错线"的程度——再细就是在重复纯层的测试。

盘上的道具与纯层同一套（`tests/fakes.py`）：同一份输入，引擎测里跑出的回合与这里登记的回合
必须是同一个，否则"接对了"这句话根本没被验证过。
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tests.fakes import Scripted, bar, minutes, snapshot_root
from zhixing_quant import config
from zhixing_quant.backtest import cli
from zhixing_quant.backtest.limits import PriceBand
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Listing, SecurityMaster
from zhixing_quant.storage import layout
from zhixing_quant.storage.query import read_bars
from zhixing_quant.storage.write import store_bars
from zhixing_quant.tasks import db
from zhixing_quant.tasks.store import Backtest, Store

#: 快照主数据里 600519 在册，日历覆盖 01-02/03/04 三天；昨收要早于区间一天。
PREV = date(2023, 12, 29)
DAY1 = date(2024, 1, 2)
DAY2 = date(2024, 1, 3)
#: 日历在册、日线却没落到这里的一天：分钟K 有它，昨收没有——板判不出来的那个形状。
DAY3 = date(2024, 1, 4)
#: 注入的运行时刻：目录名由它定，所以产物路径是可以逐字断言的（决定 10 的修订）。
WHEN = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)
LABEL = "20260919-080000"
#: 日线因子（`tests.fakes.bar` 默认 8.0）：后复权价 = 盘上的原始价 × 8，一眼看得出走的哪条路。
FACTOR = 8.0

BASE = [
    "--strategy",
    f"{__name__}:Toggler",
    "--version",
    "0.1.0",
    "--codes",
    "600519",
    "--start",
    DAY1.isoformat(),
    "--end",
    DAY2.isoformat(),
]


class Toggler(Scripted):
    """无参构造的策略：`--strategy` 只能加载这样的（`load_strategy` 那条规矩）。

    剧本是"第一根卖、第三根接回"——一笔 T出 配一笔 T入，正好一个回合，且卖价低于买价，
    于是期望为负：这一跑的台账状态该是 `fail` 而不是"没跑成"。
    """

    name = "toggler"

    def __init__(self) -> None:
        super().__init__({("600519", 0): "sell", ("600519", 2): "buy"})


class NotAStrategy:
    """零参数、但没有任何接口：`load_strategy` 要在这里响，而不是把引擎喂给一个空对象。"""

    name = "not_a_strategy"


def zx(*extra: str, now: datetime = WHEN) -> int:
    """跑一次命令行。`*extra` 追加在标准参数之后，argparse 取后者，所以覆盖用它最省事。"""
    return cli.main([*BASE, *extra], now=lambda: now)


def ledger() -> list[Backtest]:
    con = db.connect(config.taskdb_file())
    try:
        return Store(con).list_backtests()
    finally:
        con.close()


def artifacts() -> list[Path]:
    return sorted((config.backtests_dir()).rglob("*"))


def page() -> str:
    row = ledger()[-1]
    return (Path(row.report_path) / "report.md").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """三天日线 + 两天分钟线（各三根），全部落在只属于本测试的数据根里。"""
    base = snapshot_root(tmp_path, monkeypatch)
    data = base / "data"
    store_bars(
        [bar(PREV, 9.8), bar(DAY1, 10.0), bar(DAY2, 10.5)],
        dataset=layout.DAILY,
        root=data,
    )
    store_bars(
        minutes(DAY1, [10.0, 10.2, 10.4]) + minutes(DAY2, [10.3, 10.5, 10.7]),
        dataset=layout.MINUTE_5,
        root=data,
    )
    return base


def test_a_run_leaves_two_artifacts_and_exactly_one_ledger_row(root: Path) -> None:
    """一次运行 = 一个目录两份文件 + 台账一行。多一份少一份都是装配出了问题。"""
    assert zx() == 0
    rows = ledger()
    assert len(rows) == 1
    out = Path(rows[0].report_path)
    assert out == root / "backtests" / LABEL
    assert [p.name for p in sorted(out.iterdir())] == ["equity.csv", "report.md"]


def test_the_directory_is_named_by_the_clock_not_by_the_ledger_id() -> None:
    """目录名是时间戳（决定 10 的修订）：编号要等插入之后才拿得到，而报告路径插入时就得填。"""
    zx(now=WHEN)
    zx(now=WHEN.replace(minute=1))
    names = [row.report_path.rsplit("/", 1)[-1] for row in ledger()]
    assert names == [LABEL, "20260919-080100"]


def test_two_runs_in_the_same_second_do_not_overwrite_each_other() -> None:
    """同一秒跑两次是可能的（脚本连着调）。覆盖上一次运行的报告等于抹掉一条留痕。"""
    zx()
    zx()
    dirs = sorted(p for p in artifacts() if p.is_dir())
    assert [d.name for d in dirs] == [LABEL, f"{LABEL}-2"]
    assert "第 1 次回测" in (dirs[0] / "report.md").read_text(encoding="utf-8")
    assert "第 2 次回测" in (dirs[1] / "report.md").read_text(encoding="utf-8")
    assert len(ledger()) == 2


def test_the_page_reports_the_same_run_the_ledger_recorded() -> None:
    """两份产物说的是同一次：编号、参数哈希、区间、dataset 都得对得上。

    这是 `status()` 与报告共用一个判据之外，另一半防线——登记与渲染之间一旦接错线，
    台账里的 #1 就会配上一份写着 #2 的报告。
    """
    zx()
    row = ledger()[-1]
    text = page()
    assert f"#{row.id}" in text
    assert row.params_hash in text
    assert f"{DAY1} → {DAY2}" in text
    assert f"`{layout.MINUTE_5}`" in text
    assert f"第 {row.run_no} 次回测" in text


def test_the_equity_curve_has_one_row_per_mark() -> None:
    """两天各三根、一只票 = 六个点。净值落点少一根就是时钟接错了（决定 2）。"""
    zx()
    lines = Path(ledger()[-1].report_path).joinpath("equity.csv").read_text(encoding="utf-8")
    rows = lines.rstrip("\n").split("\n")
    assert rows[0] == "day,time,code,cash,held,market_value,pnl"
    assert len(rows) == 1 + 6
    assert {row.split(",")[2] for row in rows[1:]} == {"600519"}


def test_the_assumptions_come_from_the_config_file_and_not_from_code() -> None:
    """决定 8：`config/backtest.toml` 是唯一真源，代码里不留兜底默认。

    这几个数字抄在这里就是判据：表被改了而测试没改，红的应该是这里——因为台账里那一格
    答的是"那次跑的是哪张费率表"，答错了整条留痕就不可复核。
    """
    zx()
    cost = json.loads(ledger()[-1].cost_assumption)
    assert cost["cost"] == {
        "commission_pct": 0.025,
        "commission_min": 5.0,
        "stamp_pct": 0.05,
        "transfer_pct": 0.001,
        "slippage_pct": 0.02,
    }
    assert cost["position"] == {"lot_size": 100, "base_lots": 3}
    assert cost["execution"]["delay_bars"] == 1


def test_a_losing_run_is_recorded_as_fail_and_still_exits_zero() -> None:
    """ "这套信号不赚钱"是一次成功的实验（07 §一）。退出码管运行成不成，状态管赚不赚。"""
    assert zx() == 0
    row = ledger()[-1]
    assert row.status == "fail"
    assert row.status_label == "失败"
    assert json.loads(row.metrics_in)["trips"] == 1


def test_a_void_run_exits_one_while_a_losing_one_still_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """坑 #29 那一晚烧掉的正是"一次退出码 0 的作废跑"：`&&` 链读它和读一次成功一模一样。

    与上一条测试同一份道具：`600000` 在分钟盘上一根都没有 → 三档全无回合 → 台账 `void`。
    判两件事：退出码 1，和那句话得说清 1 指的是运行不成立而不是结论是负的——把"负结论也退 0"
    这条豁免顺手扩到 void，下一个读产物的人就只能在两种"1"之间猜。
    """
    assert zx("--codes", "600000") == 1
    out = capsys.readouterr().out
    assert "这一跑**不成立**（作废）" in out
    assert "负结论照样退出 0" in out
    assert ledger()[-1].status == "void"


def test_a_number_that_does_not_exist_is_stored_as_null_not_zero() -> None:
    """票池里没有分钟行的那只票：胜率不存在，台账要存 null。

    0 与"没做"在 04 §四 里从来是两件事，进台账之后更难发现——报表上 `0.0%` 的胜率读起来
    像"全输了"，而真相是"一单没成"。
    """
    zx("--codes", "600000")
    row = ledger()[-1]
    metrics = json.loads(row.metrics_in)
    assert metrics["trips"] == 0
    assert metrics["win_rate"] is None
    assert metrics["expectancy"] is None
    assert row.status == "void"
    assert "600000" in page()  # 那句幸存者偏差声明要点出是哪只票缺行


def test_the_out_of_sample_column_stays_empty_because_there_was_no_second_segment() -> None:
    """07-5.3 的三段切分是 Step 7 的事。填一个"看起来像样本外"的数比留空危险得多。"""
    zx()
    assert ledger()[-1].metrics_out == ""
    assert "没有样本外段" in page()


def test_the_same_parameters_count_up_and_a_changed_range_starts_over() -> None:
    """`第 N 次`数的是"同一份参数又跑了一遍"（03-4.5）。换区间是另一个实验。"""
    zx()
    zx()
    zx("--end", DAY1.isoformat())
    first_two, last = ledger()[:2], ledger()[-1]
    assert [r.run_no for r in first_two] == [1, 2]
    assert len({r.params_hash for r in first_two}) == 1
    assert (last.run_no, last.params_hash != first_two[0].params_hash) == (1, True)


def test_writing_the_same_pool_in_a_different_order_is_not_a_second_experiment() -> None:
    """`--codes` 排序进哈希：同一批票写成两种顺序不是两次实验，台账要把它们认成同一条。"""
    zx("--codes", "300750,600519")
    zx("--codes", "600519,300750")
    rows = ledger()
    assert rows[0].params_hash == rows[1].params_hash
    assert [r.run_no for r in rows] == [1, 2]


def test_a_task_id_is_recorded_so_the_row_can_be_read_back_from_the_task() -> None:
    """09 §五：汇报必须附台账编号，而编号要能顺着 `--task` 回到那条任务上。"""
    con = db.connect(config.taskdb_file())
    try:
        task = Store(con).create_task("Step 6c 装配层", "feat", "6")
    finally:
        con.close()
    assert zx("--task", str(task.id)) == 0
    assert ledger()[-1].task_id == task.id


def test_a_vector_dataset_is_refused_before_anything_is_read() -> None:
    """07 §5.1 第一条禁令：日内引擎不吃日线。退出码 2，且一个字节都不许留下。"""
    assert zx("--dataset", layout.DAILY) == 2
    assert ledger() == []
    assert not config.backtests_dir().exists()


def test_a_flipped_range_is_refused_by_the_arguments_not_by_a_traceback() -> None:
    """区间颠倒在参数层就该响：让它逃到查询层去抛，用户看到的是一段栈而不是一句话。"""
    assert zx("--start", DAY2.isoformat(), "--end", DAY1.isoformat()) == 2
    assert ledger() == []


def test_an_unknown_task_id_fails_differently_from_a_bad_range() -> None:
    """3 = 参数没错、盘上也没错，是那个任务号不存在。与 2 混成一个码就没法在 CI 里分辨。"""
    assert zx("--task", "999") == 3
    assert ledger() == []
    assert not config.backtests_dir().exists()


@pytest.mark.parametrize(
    "dotted",
    [
        f"{__name__}:Nope",  # 模块里没有这个名字
        "no.such.module:Toggler",  # 连模块都没有
        f"{__name__}",  # 少了":类名"
        "tests.fakes:Scripted",  # 有类，但要点参数才能构造
        f"{__name__}:NotAStrategy",  # 能构造，可它没有 signals
    ],
)
def test_a_strategy_that_cannot_be_loaded_stops_the_run(dotted: str) -> None:
    """没有默认策略，也没有"先跑起来再说"：策略装不上就是这次实验根本没开始。"""
    assert cli.main([*BASE[:1], dotted, *BASE[2:]]) == 2
    assert ledger() == []


def test_missing_adjustment_factors_stop_the_run_instead_of_silently_using_raw_prices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """分钟线在、日线不在 → 后复权价给不出来（`query` 抛 `Unadjustable`）。

    退回 raw 是这条链上最坏的一种"能跑"：除权日会被算成一笔真亏损，而报告上没有任何一处
    会说口径换过了（ADR-0009 决定 4、ADR-0010 决定 9）。
    """
    # 另一个数据根：只有分钟线、没有日线，也就没有任何复权因子可问。
    elsewhere = tmp_path / "no_daily"
    elsewhere.mkdir()
    base = snapshot_root(elsewhere, monkeypatch)
    store_bars(minutes(DAY1, [10.0, 10.2, 10.4]), dataset=layout.MINUTE_5, root=base / "data")
    assert zx() == 2
    assert ledger() == []
    assert not (base / "backtests").exists()


def test_the_missing_factor_refusal_names_the_whole_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺口要一次报全。撞一只报一只在 3 只票的池子里无所谓，在 Step 7 的几百只里就是"补数据
    得先跑几百遍才看得见全貌"——那句"先确认日线落盘了"也就成了永远补不完的话。
    """
    elsewhere = tmp_path / "two_missing"
    elsewhere.mkdir()
    base = snapshot_root(elsewhere, monkeypatch)
    store_bars(
        minutes(DAY1, [10.0, 10.2]) + minutes(DAY1, [20.0, 20.2], symbol="300750"),
        dataset=layout.MINUTE_5,
        root=base / "data",
    )
    assert zx("--codes", "300750,600519") == 2
    err = capsys.readouterr().err
    assert "2 只票没有可用复权因子" in err
    assert "300750" in err and "600519" in err
    assert ledger() == []


def test_the_command_line_refuses_to_guess_the_pool() -> None:
    """`--codes` 是 required：票池多大是等用户裁决的事（ADR-0009 决定 8），给默认值就是替人裁决。"""
    with pytest.raises(SystemExit) as gone:
        cli.main(BASE[:4])
    assert gone.value.code == 2
    assert ledger() == []


@pytest.mark.parametrize(("flag", "value"), [("--start", "去年"), ("--codes", " , ")])
def test_a_malformed_argument_is_refused_by_the_parser(flag: str, value: str) -> None:
    """日期与票池都是人敲的：形状不对就在 parser 层响，而不是带着半个参数往下走到读盘。

    `--codes " , "` 这一步就该死——它到了下游会变成"票池为空"，而空票池与"盘上恰好没数据"
    在报告上长得太像了。
    """
    with pytest.raises(SystemExit) as gone:
        zx(flag, value)
    assert gone.value.code == 2
    assert ledger() == []


def test_the_engine_is_fed_backward_prices() -> None:
    """决定 9 的接线判据：同一个查询，`backward` 是原始价的 8 倍。

    判在装配层是因为口径是这里选的（`read_minute` 写死 `adjust="backward"`）：查询层自己
    测过三种口径的换算，但"引擎吃哪一种"只有这一处能答。
    """
    backward = cli.read_minute(("600519",), DAY1, DAY2, layout.MINUTE_5)["600519"]
    raw = read_bars(
        "600519", DAY1, DAY2, adjust="raw", dataset=layout.MINUTE_5, root=config.parquet_dir()
    )
    assert len(backward) == len(raw) == 6
    assert [b.open for b in backward] == pytest.approx([r.open * FACTOR for r in raw])
    assert backward[0].open == pytest.approx(10.0 * FACTOR)


def test_the_first_day_of_the_range_has_a_previous_close() -> None:
    """往回多读 30 天是为了区间首日也有昨收：首日的板判不出来，第一天就一单都成不了。"""
    closes = cli.previous_closes(("600519",), DAY1, DAY2)["600519"]
    assert closes[DAY1] == pytest.approx(9.8 * FACTOR)
    assert closes[DAY2] == pytest.approx(10.0 * FACTOR)


def test_depth_of_an_empty_dataset_is_reported_as_such() -> None:
    """盘上没这个 dataset 与"有但没覆盖区间"是两句话，前者该让人去跑 `zx-minute`。"""
    zx("--dataset", layout.MINUTE_30)
    assert f"盘上一个 `{layout.MINUTE_30}` 分区都没有" in page()


def test_a_range_older_than_the_master_snapshot_says_the_st_cap_never_applied() -> None:
    """快照抓取日之后才有 ST 判定；整段区间都在它之前时，那句"只有部分日期"要改成全段。"""
    zx("--start", DAY1.isoformat(), "--end", DAY1.isoformat())
    assert "整个区间都早于 ST 判定生效日" in page()


def test_a_minute_day_without_previous_close_is_named_with_its_code_and_days(
    root: Path,
) -> None:
    """坑 #31 的正面写法：日线深度不够时，报告自己说出缺哪几只票、缺几天、怎么补。

    判的是"点名"而不是"有个数"：第四节那张拒单表只会说 `band_unknown` 485 根，读完还是不知道
    该跑哪条命令。这一句把原因和补法一起给出去，才拦得住"把作废读成策略不行"。
    """
    store_bars(minutes(DAY3, [10.6, 10.8, 11.0]), dataset=layout.MINUTE_5, root=root / "data")
    assert zx("--end", DAY3.isoformat()) == 0
    text = page()
    assert "600519 缺 1/3 天" in text
    assert "`zx-daily --day 2024-01-04 --previous-days N`" in text


def test_a_run_whose_daily_depth_covers_the_minutes_says_nothing_about_it() -> None:
    """没缺口就不许道歉：这一句是报缺陷的，写成常态就等于每天都有一堆"不能保证"混在里面。"""
    assert zx() == 0
    assert "日线昨收不覆盖" not in page()
    assert "zx-daily" not in page()


def test_the_gap_is_counted_per_code_per_day_not_by_the_orders_it_blocked() -> None:
    """缺口统计与策略无关：一只票一次都没被交易，它的日线缺口也必须在列。

    这是 `blind_days` 与第四节拒单表的分工——拒单表按笔计（要策略真想交易才响），这里按天计
    （数据到没到盘上，与这次做不做无关）。剧本只动 600519 的 DAY1 两根，000001 一笔都不成交；
    按拒单数统计的话，000001 那两天整个隐身。
    """
    bars = {
        "600519": [bar(DAY1, 10.0), bar(DAY2, 10.0)],
        "000001": [bar(DAY1, 8.0, symbol="000001"), bar(DAY2, 8.2, symbol="000001")],
    }
    prev = {"600519": {DAY1: 9.8, DAY2: 10.0}}
    assert cli.blind_days(bars, prev) == {"000001": (2, 2)}


#: 板块档位与无涨跌幅窗口天数，与 `config/gate.toml` 的 `[defaults]`/R004 同一组数。
LIMITS = {"main": 10.0, "st": 5.0, "gem": 20.0, "star": 20.0, "bse": 30.0}
NO_LIMIT = {"main": 5, "gem": 5, "star": 5, "bse": 1}


def test_the_new_listing_window_is_asked_before_the_previous_close() -> None:
    """判序在装配层的落地（决定 4）：新股首日**根本没有**昨收，先查昨收就会把"今天不设限"
    报成"我判不出来"——一笔能成交的单子被一个不存在的数据缺口挡掉。
    """
    master = SecurityMaster(
        [
            Listing(code="600001", name="新股", listed_on=DAY1),
            Listing(code="600002", name="旧股", listed_on=date(2000, 1, 4)),
        ]
    )
    bands = cli.build_bands(
        master,
        TradingCalendar([DAY1, DAY2]),
        limits_pct=LIMITS,
        no_limit_days=NO_LIMIT,
        prev_closes={"600002": {DAY1: 100.0}},
    )
    assert bands("600001", DAY1).state == "unlimited"
    assert bands("600002", DAY1) == PriceBand.bound(10.0, 100.0)
    assert bands("600002", DAY2).state == "unknown"  # 昨收只给到 DAY1
    assert bands("600003", DAY1).state == "unknown"  # 主数据里没有这只票


def test_a_code_that_is_not_on_the_master_is_judged_unknown_rather_than_given_a_board() -> None:
    """认不出板块就不许假设一个上限：假设 10% 会让一只科创板的票在 15% 处被误判成封死。"""
    bands = cli.build_bands(
        SecurityMaster(),
        TradingCalendar([DAY1]),
        limits_pct=LIMITS,
        no_limit_days=NO_LIMIT,
        prev_closes={},
    )
    band = bands("600519", DAY1)
    assert band.state == "unknown"
    assert not band.judgeable
    assert "600519" in band.note


def test_the_summary_line_does_not_turn_a_missing_number_into_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """命令行那一行是很多人唯一会看的东西：无回合时它必须印 `—`，不能印 +0.0000。"""
    zx("--codes", "600000")
    out = capsys.readouterr().out
    assert "期望 —" in out
    assert "+0.0000" not in out
