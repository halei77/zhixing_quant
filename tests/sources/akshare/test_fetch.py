"""akshare 抓取层：只测"请求参数拼对了没有"，不测网络。

这层薄到不值得写业务断言，但它错了整条链都错：日期格式少一位、`adjust` 传成 `"qfq"`、
市场前缀搞反沪深，源都不会报错，只会**安静地返回别的东西**。日报上表现为"这只票今天零行"
或"因子全空"，那是最难查的一类故障。所以每个函数测的都是发出去的 kwargs。

`call` 参数是注入点：假函数返回假帧，本模块的转换与参数拼装照样被测到，CI 不联网。
"""

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fakes import FakeFrame, Recorder
from zhixing_quant.domain.symbol import UnknownCode
from zhixing_quant.sources.akshare import fetch

DAY1 = date(2024, 1, 2)
DAY2 = date(2024, 1, 31)


# --- 代码 → 源的写法 ----------------------------------------------------------------
# --- 代码 → 源的写法 ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "market"),
    [
        ("600519", "sh"),  # 沪市主板
        ("000001", "sz"),  # 深市主板
        ("002415", "sz"),  # 原中小板，并入主板后仍在深市
        ("300750", "sz"),  # 创业板
        ("688981", "sh"),  # 科创板
        ("430047", "bj"),  # 北交所三段前缀
        ("832000", "bj"),
        ("920819", "bj"),  # 2024 年新启的 920 段：按首位判会掉进"9 开头是沪市"
    ],
)
def test_market_follows_the_board_table(code: str, market: str) -> None:
    assert fetch.market_of(code) == market


@pytest.mark.parametrize("code", ["900901", "510300", "128019"])
def test_a_code_r004_cannot_judge_has_no_market_either(code: str) -> None:
    """B 股 / ETF / 可转债：板块表判不了它们，这里也不给前缀。

    按首位硬判会把 920 段认成沪市、把 900 段认成"合法"，两种都是把范围外的东西请进管道。
    """
    with pytest.raises(UnknownCode):
        fetch.market_of(code)


@pytest.mark.parametrize("written", ["600519", "sh600519", "600519.SH", " 600519 "])
def test_any_spelling_of_the_code_becomes_the_sources_shape(written: str) -> None:
    """源只认 `sh600519`；仓库内部只认 6 位。两边都归一到同一个入口形状。"""
    assert fetch.sina_symbol(written) == "sh600519"


@pytest.mark.parametrize("code", ["510300", "900901", "128019", "abcdef"])
def test_a_code_the_gate_cannot_judge_never_reaches_the_network(code: str) -> None:
    """ETF / B 股 / 可转债 / 手打错的代码长得都像代码，但 R004 对它们没有判据。

    放它们过去，源会返回一个空 DataFrame 或一份不相干的行情，日报上只是"少了这只票"——
    比在这里抛 UnknownCode 难查一个数量级。
    """
    with pytest.raises(UnknownCode):
        fetch.sina_symbol(code)


# --- 请求参数 -----------------------------------------------------------------------


def test_daily_frame_sends_the_window_as_compact_ymd() -> None:
    """`start_date` 要 `20240102`：传 ISO 带连字符的写法，sina 接口照样返回——但是空表。"""
    call = Recorder([{"date": DAY1, "close": 1685.01}])
    rows = fetch.daily_frame("600519", DAY1, DAY2, call=call)
    assert call.kwargs == [
        {"symbol": "sh600519", "start_date": "20240102", "end_date": "20240131", "adjust": ""}
    ]
    assert rows == [{"date": DAY1, "close": 1685.01}]


def test_daily_frame_passes_adjust_through_untouched() -> None:
    """`qfq` 不在本项目用：口径是"只存不复权 + 因子"，但这一层不许偷偷改写参数。"""
    call = Recorder([])
    fetch.daily_frame("600519", DAY1, DAY2, adjust="hfq", call=call)
    assert call.kwargs[0]["adjust"] == "hfq"


def test_daily_pulls_both_frames_in_one_call() -> None:
    """两帧的顺序就是 `Fetcher` 契约的顺序：(不复权, 后复权)。装反了因子会算成倒数。"""
    call = Recorder([{"close": 1685.01}], [{"close": 13601.13}])
    raw, hfq = fetch.fetch_daily("600519", DAY1, DAY2, call=call)
    assert [k["adjust"] for k in call.kwargs] == ["", "hfq"]
    assert raw == [{"close": 1685.01}] and hfq == [{"close": 13601.13}]


@pytest.mark.parametrize("period", ["5", "30", "60"])
def test_minute_frame_sends_symbol_period_and_no_adjust(period: str) -> None:
    """分钟线只有三个参数：源没有窗口参数，`adjust` 恒为空（两种复权口径不许混进一个 dataset）。"""
    call = Recorder([{"day": "2024-01-02 09:35:00", "close": "1685.01"}])
    rows = fetch.minute_frame("600519", period, call=call)
    assert call.kwargs == [{"symbol": "sh600519", "period": period, "adjust": ""}]
    assert rows == [{"day": "2024-01-02 09:35:00", "close": "1685.01"}]


def test_minute_frame_refuses_a_period_with_no_dataset() -> None:
    """源真的会回 1 分钟的数据。让它走完再发现没有 `minute_1` 这个目录，是几十万请求之后才响。"""
    call = Recorder([{"day": "2024-01-02 09:31:00"}])
    with pytest.raises(ValueError, match="不支持的分钟周期"):
        fetch.minute_frame("600519", "1", call=call)
    assert call.kwargs == []


def test_calendar_is_fetched_without_a_window() -> None:
    """源本身是"从开市到今天"的一整张表：给它传日期参数不会报错，只会静默少一批日子。"""
    call = Recorder([{"trade_date": DAY1}])
    assert fetch.fetch_calendar(call=call) == [{"trade_date": DAY1}]
    assert call.kwargs == [{}]


def test_listings_ask_every_board_akshare_covers() -> None:
    """akshare 侧三份名单各一次。漏一个板块 = 股票池凭空少一块，而源不会报任何错。

    北交所那份不在这条路径上（akshare 没有 BSE 名单接口，走 relay），所以这里断的是三份。
    """
    call = Recorder([{"证券代码": "600519"}], [{"证券代码": "000001"}], [{"证券代码": "688001"}])
    rows = fetch.fetch_listings(call=call)
    assert [k["symbol"] for k in call.kwargs] == ["主板A股", "A股列表", "科创板"]
    assert [r["证券代码"] for r in rows] == ["600519", "000001", "688001"]


# --- 真 akshare 的入口 ---------------------------------------------------------------


def test_the_real_module_is_imported_only_when_no_call_is_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`import akshare` 待在这条分支里：离线重放与 CI 不该为一个几百 MB 依赖的导入付代价。"""
    imported: list[str] = []

    def fake_import_module(name: str) -> Any:
        imported.append(name)
        return SimpleNamespace(tool_trade_date_hist_sina=lambda: FakeFrame([{"d": 1}]))

    monkeypatch.setattr("importlib.import_module", fake_import_module)
    assert fetch.fetch_calendar() == [{"d": 1}]
    assert imported == ["akshare"]
