"""`zx-daily` 命令行入口（01 路线图 Step 2 第 4 项）。

CLI 唯一的价值是"装配对不对"，所以这里测的全是接线：数据根从环境变量来、抓取走真那一层、
退出码分得清"没判成"和"根本没开始"。规则本身在 `test_daily.py` 与 `test_quality_engine.py`。

退出码是定时任务的接口：`0` 才有人继续看日报，非 0 得能从数字上就分出错在哪一层——
抓不到票（今天源不行）和快照缺失（本地环境不行）要往不同的方向修。
"""

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from zhixing_quant.sources.akshare import fetch as akshare_fetch
from zhixing_quant.sources.akshare import master as am
from zhixing_quant.sources.jobs import cli
from zhixing_quant.sources.rows import Pair

CALENDAR_DAYS = ("2024-01-02", "2024-01-03", "2024-01-04")
DAY = date(2024, 1, 3)


def bar(day: date, close: float) -> dict[str, Any]:
    return {
        "date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000.0,
        "amount": close * 1000,
    }


def clean_pair(_code: str, _start: date, _end: date) -> Pair:
    rows = [bar(date(2024, 1, 2), 100.0), bar(DAY, 101.0)]
    return rows, rows


def failing(_code: str, _start: date, _end: date) -> Pair:
    raise ConnectionError("源不可用")


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个只装了快照的数据根：CLI 读什么、写什么，全都落在 tmp 里。"""
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))
    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "tool_trade_date_hist_sina.csv").write_text(
        "trade_date\n" + "\n".join(CALENDAR_DAYS) + "\n", encoding="utf-8"
    )
    (golden / f"{am.SNAPSHOT_NAMES[0]}.csv").write_text(
        "证券代码,证券简称,上市日期\n600519,贵州茅台,2001-08-27\n", encoding="utf-8"
    )
    (golden / f"{am.SNAPSHOT_NAMES[1]}.csv").write_text(
        "A股代码,A股简称,A股上市日期\n300750,宁德时代,2018-06-11\n", encoding="utf-8"
    )
    return tmp_path


def test_a_clean_run_exits_zero_and_prints_the_report(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--day", "2024-01-03"], fetcher=clean_pair) == 0
    out = capsys.readouterr().out
    assert "# 数据质量日报 2024-01-03" in out
    assert "| akshare_daily | 2 | 0 | 0 | 否 | 100.0 | A |" in out
    assert (data_root / "reports" / "2024-01-03.md").is_file()  # 归档走数据根，不落仓库
    assert (data_root / "reports" / "scores.csv").is_file()
    # Step 3b：真入口跑一次，干净区就要在盘上（根来自 config.parquet_dir，即 ZX_DATA_ROOT）。
    # 两票 × 两日 = 4 行，而日报的分母只有报告日那 2 行——两个数不同，才说明各报各的。
    assert (data_root / "data/daily/year=2024/symbol=600519.parquet").is_file()
    assert "- 进干净区：新增 4 行" in out


@pytest.mark.usefixtures("data_root")
def test_a_run_that_judges_nothing_exits_one() -> None:
    """一只票都没判成 = 这次运行没成：空报告不能对定时任务报"成功"。"""
    assert cli.main(["--day", "2024-01-03", "--attempts", "1"], fetcher=failing) == 1


def test_a_missing_snapshot_exits_two_not_one(
    data_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """快照缺失是本地环境问题，和"源今天不行"不同一条诊断，所以退出码也不同。"""
    (data_root / "golden" / "tool_trade_date_hist_sina.csv").unlink()
    assert cli.main(["--day", "2024-01-03"], fetcher=clean_pair) == 2
    assert "采集中止" in capsys.readouterr().err


@pytest.mark.usefixtures("data_root")
def test_a_calendar_that_stops_early_is_not_silently_replaced_by_stale_days(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """日历只到 01-04 却要判 01-31：宁可停，也不要去抓"最近一个判得出来的日子"。"""
    assert cli.main(["--day", "2024-01-31"], fetcher=clean_pair) == 2
    assert "只到 2024-01-04" in capsys.readouterr().err


@pytest.mark.usefixtures("data_root")
def test_symbols_and_limit_reach_the_fetcher() -> None:
    asked: list[str] = []

    def spy(code: str, _start: date, _end: date) -> Pair:
        asked.append(code)
        return clean_pair(code, _start, _end)

    assert cli.main(["--day", "2024-01-03", "--symbols", "600519, 300750"], fetcher=spy) == 0
    assert asked == ["600519", "300750"]

    asked.clear()
    assert cli.main(["--day", "2024-01-03", "--limit", "1"], fetcher=spy) == 0
    assert asked == ["600519"]  # 不传 --symbols 时，股票池就是主数据当天的在册名单


@pytest.mark.usefixtures("data_root")
def test_the_default_pool_is_the_whole_universe() -> None:
    asked: list[str] = []

    def spy(code: str, _start: date, _end: date) -> Pair:
        asked.append(code)
        return clean_pair(code, _start, _end)

    assert cli.main(["--day", "2024-01-03"], fetcher=spy) == 0
    assert asked == ["600519", "300750"]


@pytest.mark.usefixtures("data_root")
def test_previous_days_widens_the_fetch_window() -> None:
    windows: list[tuple[date, date]] = []

    def spy(code: str, start: date, end: date) -> Pair:
        windows.append((start, end))
        return clean_pair(code, start, end)

    cli.main(["--day", "2024-01-04", "--previous-days", "2"], fetcher=spy)
    assert windows == [(date(2024, 1, 2), date(2024, 1, 4))] * 2  # 两只票，同一窗口


@pytest.mark.usefixtures("data_root")
def test_without_an_injected_fetcher_it_grabs_from_akshare(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """默认那一条路必须被走一遍：接线接错的样子是"测试全绿、真跑起来抓了个空气"。"""
    asked: list[tuple[str, date, date]] = []

    def grab(code: str, start: date, end: date) -> Pair:
        asked.append((code, start, end))
        return [], []

    monkeypatch.setattr(akshare_fetch, "fetch_daily", grab)
    # 源给空表时引擎报 R006"本批无有效日期"，一个 FATAL 批次都没留下行——报 0 等于
    # 替一个宕掉的源说"今天一切正常"，这是定时任务最坏的一种谎言。
    assert cli.main(["--day", "2024-01-03", "--symbols", "600519"]) == 1
    assert asked == [("600519", date(2024, 1, 2), date(2024, 1, 3))]
    assert "今日无数据：2024-01-03" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--day", "2024-1-3"], ["--day", "今天"]])
def test_a_malformed_day_is_rejected_before_anything_is_fetched(argv: list[str]) -> None:
    """argparse 就拦下：一个手打的日期错不该变成一次全市场抓取。"""
    with pytest.raises(SystemExit) as excinfo:
        cli.main(argv, fetcher=clean_pair)
    assert excinfo.value.code == 2
