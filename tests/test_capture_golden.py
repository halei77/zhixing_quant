"""黄金样本抓取工具 tools/capture_golden.py（03 §二 L2；Step 2a）。

真联网的那几个函数在这里不被调用：抓取边界是注入的，测试用假 fetcher 落 tmp 目录，
测的是**样本格式**——它一旦进仓就成了断言的一部分，格式错了整批样本都白落。

最要紧的一条是重放保真：源当时给 NaN，样本读回来还得让适配器判出 NaN。落成空串会把
"源给了个不成立的数"变成"字段没给"，日报上那是两条不同的诊断（见 sources/rows）。
"""

import math
import sys
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import capture_golden as cg  # noqa: E402  # tools/ 不在包内，按脚本路径导入

from tests.fakes import FakeFrame, Recorder  # noqa: E402  # tests 是包，假帧在几个源测试间共用
from zhixing_quant.sources.akshare.daily import daily_drafts  # noqa: E402
from zhixing_quant.sources.rows import to_date  # noqa: E402

RAW: list[dict[str, Any]] = [
    {
        "date": date(2024, 1, 2),
        "open": 1715.00,
        "high": 1718.19,
        "low": 1678.10,
        "close": 1685.01,
        "volume": 3215644.0,
        "amount": 5440082548.0,
    }
]
#: 源给坏值的那一批：NaN 与缺列各自的形状。
BAD_RAW: list[dict[str, Any]] = [{**RAW[0], "volume": float("nan"), "amount": None}]
HFQ: list[dict[str, Any]] = [{"date": "2024-01-02", "close": 13601.13}]


def _capture(tmp_path: Path, **fetchers: cg.Fetcher) -> list[cg.Record]:
    return cg.capture(fetchers, tmp_path / "golden", captured_at=datetime(2026, 9, 19, 8, 30, 5))


def test_every_key_becomes_a_csv_and_one_manifest(tmp_path: Path) -> None:
    records = _capture(tmp_path, b_daily=lambda: RAW, a_hfq=lambda: HFQ)
    out = tmp_path / "golden"
    assert [p.name for p in sorted(out.iterdir())] == ["a_hfq.csv", "b_daily.csv", "manifest.csv"]
    assert [r["key"] for r in records] == ["a_hfq", "b_daily"]  # 排序落盘，diff 才稳定
    assert [r["status"] for r in records] == ["ok", "ok"]
    assert [r["rows"] for r in records] == [1, 1]
    assert records[1]["columns"] == "date|open|high|low|close|volume|amount"


def test_the_manifest_is_a_table_any_tool_can_read(tmp_path: Path) -> None:
    _capture(tmp_path, daily=lambda: RAW)
    rows = cg.read_csv(tmp_path / "golden" / cg.MANIFEST_NAME)
    assert list(rows[0]) == list(cg.MANIFEST_COLUMNS)
    assert rows[0]["captured_at"] == "2026-09-19T08:30:05"  # 记到秒：同一天重抓也分得出先后


def test_a_snapshot_survives_the_csv_round_trip_with_the_same_verdicts(tmp_path: Path) -> None:
    """重放保真：落盘再读回来的行，必须让适配器产出一模一样的判定输入。

    这是黄金样本机制的地基——不成立的话 CI 里重放的是另一份数据，测绿说明不了真源那天
    那批数据会被怎么判。
    """
    _capture(tmp_path, daily=lambda: RAW, hfq=lambda: HFQ)
    out = tmp_path / "golden"
    live = daily_drafts(RAW, HFQ, symbol="600519")[0]
    replayed = daily_drafts(
        cg.read_csv(out / "daily.csv"), cg.read_csv(out / "hfq.csv"), symbol="600519"
    )
    assert replayed[0].model_dump() == live.model_dump()
    assert live.trade_date == date(2024, 1, 2)  # date 对象落成 ISO 串也读得回来


def test_a_nan_in_the_source_is_not_recorded_as_a_missing_field(tmp_path: Path) -> None:
    """NaN 落成 `nan` 而不是空串：空串读回来是 None，诊断就从"源给了不成立的数"变成"没给"。

    不复用上一条的等值断言，是因为 NaN 不等于自己——两份 model_dump 里的 NaN 无论怎么
    落盘都比不出相等，等值测试在这里必然假失败。
    """
    _capture(tmp_path, daily=lambda: BAD_RAW)
    (replayed,) = daily_drafts(cg.read_csv(tmp_path / "golden" / "daily.csv"), symbol="600519")
    assert replayed.volume is not None and math.isnan(replayed.volume)
    assert replayed.amount is None


def test_cells_are_written_in_a_shape_the_adapters_read_back(tmp_path: Path) -> None:
    """落盘格式两头都钉住：人能 diff，`to_date` 也认得回来。"""
    path = tmp_path / "cells.csv"
    cg.write_csv([{"d": date(2024, 1, 2), "t": datetime(2024, 1, 2, 15, 0), "n": None}], path)
    body = path.read_text(encoding="utf-8").splitlines()[1]
    assert body == "2024-01-02,2024-01-02 15:00:00,"
    (row,) = cg.read_csv(path)
    assert to_date(row["d"]) == date(2024, 1, 2)
    assert to_date(row["t"]) == date(2024, 1, 2)  # 带时分秒的串：截前 10 位，主键才只有一个日期
    assert row["n"] is None


def test_a_broken_source_is_recorded_not_swallowed(tmp_path: Path) -> None:
    """一个源挂了，其余照落，挂的那个带原因进清单。

    静默少一个文件比多一个错误更糟：那正是"样本还在、内容已过期"的由来。
    """

    def boom() -> list[dict[str, Any]]:
        raise ConnectionResetError("connection reset by peer")

    records = _capture(tmp_path, broken=boom, fine=lambda: RAW)
    assert [r["status"] for r in records] == ["failed", "ok"]
    assert "ConnectionResetError" in str(records[0]["detail"])
    assert not (tmp_path / "golden" / "broken.csv").exists()
    assert (tmp_path / "golden" / "fine.csv").is_file()  # 别的一个字都不受影响


def test_an_empty_snapshot_is_not_reported_as_success(tmp_path: Path) -> None:
    """0 行的样本重放不出任何断言：报成 ok 就是"源什么都没给"看起来像"一切正常"。"""
    (record,) = _capture(tmp_path, nothing=list)
    assert record["status"] == "empty"
    assert record["rows"] == 0


def test_ragged_rows_share_one_column_union_in_source_order(tmp_path: Path) -> None:
    """源给了残缺行：列序照源的样子留，缺的格子读回来是 None。

    列序不排序是故意的——排过序就看不出"源加了哪一列"，而列漂移正是要靠样本发现的事。
    """
    _capture(tmp_path, ragged=lambda: [{"a": 1, "c": 3}, {"b": 2}])
    rows = cg.read_csv(tmp_path / "golden" / "ragged.csv")
    assert list(rows[0]) == ["a", "c", "b"]
    assert rows[0]["b"] is None and rows[1]["a"] is None


WINDOW = (date(2024, 1, 2), date(2024, 1, 31))
SYMBOLS = ("sh600519", "sz300750")


def _bse_relay(*rows: list[str], count: int | None = None, has_more: bool = False) -> cg.RelayCall:
    """北交所名单那一份的假 relay。不给它一个 seam，这些测试就会去碰真网络与真 key。"""
    body: dict[str, Any] = {
        "data": {
            "fields": ["ts_code", "name", "list_date"],
            "items": list(rows),
            "count": len(rows) if count is None else count,
            "has_more": has_more,
        }
    }

    def fetch(_api: str, _params: Mapping[str, object]) -> tuple[str, dict[str, Any]]:
        return "rds", body

    return fetch


def test_the_capture_tool_covers_every_snapshot_the_master_needs() -> None:
    """主数据要的**全部**快照（名单组四 + 停牌组两），抓取工具必须一份不少——这是本文件
    最贵的一条不变量。

    重抓会**覆盖** manifest（`capture()` 的写法），少抓一份就是把那份的 captured_at 抹掉，
    `master.captured_on` 随即拒收，zx-daily / zx-site / zx-backtest 全线起不来。2026-09-23
    实测过一次：扩板加了科创板与北交所两份名单，工具却还只抓原来的两份，`read_master` 直接
    `SourceSchemaError`。注释里写"两处同名"挡不住这件事，只有这条断言能。断言的是**并集**
    ——名单组与停牌组各自漏了哪份都能从差集里读出来（任务 #57 扩到六份）。
    """
    from zhixing_quant.sources.akshare import master

    keys = set(cg.build_fetchers(WINDOW, SYMBOLS))
    needed = set(master.LISTING_SNAPSHOT_NAMES) | set(master.SUSPENSION_SNAPSHOT_NAMES)
    assert needed == set(master.SNAPSHOT_NAMES)
    assert needed <= keys, sorted(needed - keys)


def test_the_relay_listing_frame_keeps_the_source_columns() -> None:
    """北交所名单落的是源那天的原样，不剪成主数据要的三列（剪过就看不出列漂移）。"""
    rows = cg.relay_listing_frame(relay_call=_bse_relay(["920229.BJ", "N世纪", "20260922"]))
    assert rows == [{"ts_code": "920229.BJ", "name": "N世纪", "list_date": "20260922"}]


def test_the_bse_snapshot_parses_into_the_bse_board() -> None:
    """样本落下来是为了重放：这几行的形状必须真能过主数据那一层（`.BJ` 后缀归一化）。"""
    from zhixing_quant.domain.symbol import Board, board_of
    from zhixing_quant.sources.akshare.master import listings_from_rows

    rows = cg.relay_listing_frame(relay_call=_bse_relay(["920229.BJ", "N世纪", "20260922"]))
    (listing,) = listings_from_rows(rows).listings
    assert listing.code == "920229"
    assert board_of(listing.code) is Board.BSE


def test_a_truncated_relay_listing_is_refused_not_sampled() -> None:
    """源声称的总行数大于这一页就是截断（ADR-0015 决定 3）：样本落一半比不落更危险。"""
    truncated = _bse_relay(["920229.BJ", "N世纪", "20260922"], count=5644, has_more=True)
    with pytest.raises(RuntimeError, match="截断"):
        cg.relay_listing_frame(relay_call=truncated)


def test_fetcher_keys_encode_the_window_and_the_symbol() -> None:
    """key 就是文件名，所以参数必须写在 key 里：换窗口=换 key，不覆盖已批准的样本。"""
    keys = cg.build_fetchers(WINDOW, SYMBOLS)
    assert "stock_zh_a_daily__sh600519__20240102_20240131__hfq" in keys
    assert "tool_trade_date_hist_sina" in keys
    # 每份名单一个 key：合并成一份样本就重放不出"某个板块今天什么都没给"
    assert "stock_info_sh_name_code__主板A股" in keys
    assert "stock_info_sz_name_code__A股列表" in keys
    assert "stock_info_sh_name_code__科创板" in keys
    # 北交所名单（relay）：四份上市名单里唯一不走 akshare 的一份
    assert cg.BSE_LISTING_KEY in keys
    # 停牌两份（任务 #57）：地板写死在 fetch 里，key 不带窗口——同地板重抓永远含新增
    assert cg.EM_SUSPEND_KEY in keys
    assert cg.BAIDU_SUSPEND_KEY in keys
    # 分钟线的 key 不带窗口：源没有窗口参数（ADR-0009 决定 6），能带的参数只有周期。
    assert "stock_zh_a_minute__sh600519__5min" in keys
    assert "stock_zh_a_minute__sz300750__60min" in keys


def _write_calendar(tmp_path: Path, *days: str) -> Path:
    """数据根里放一份日历：百度回填**读盘上已有的**日历（`news_…` 排在 `tool_…` 前面，
    本次刚抓的那份还不存在）。"""
    golden = tmp_path / "golden"
    golden.mkdir(parents=True, exist_ok=True)
    path = golden / "tool_trade_date_hist_sina.csv"
    path.write_text("trade_date\n" + "\n".join(days) + "\n", encoding="utf-8")
    return path


def test_each_symbol_binds_its_own_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """闭包捕获循环变量的经典事故：所有 key 都抓最后一只票，样本名字却全都对得上。"""
    # 日历只给地板**之前**的日子 → 百度回填 0 天、不发请求（否则它内部循环会把
    # Recorder 的帧序列耗尽——那是回填编排，不是"每 key 一次"的绑定判据）。
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))
    _write_calendar(tmp_path, "2024-01-02", "2024-01-03")
    call = Recorder(*[[] for _ in cg.build_fetchers(WINDOW, SYMBOLS)])
    fetchers = cg.build_fetchers(WINDOW, SYMBOLS, call=call, relay_call=_bse_relay())
    for key, fetcher in fetchers.items():
        assert fetcher() == [], key
    daily = [k for k in call.kwargs if "start_date" in k]
    assert len(daily) == 4  # 两只票 × raw/hfq
    assert {(str(k["symbol"]), str(k["adjust"])) for k in daily} == {
        ("sh600519", ""),
        ("sh600519", "hfq"),
        ("sz300750", ""),
        ("sz300750", "hfq"),
    }
    assert {str(k["start_date"]) for k in daily} == {"20240102"}
    # 分钟线：两只票 × 三个周期，`adjust` 恒为空（源的复权口径不进本方管道，ADR-0009 决定 4）
    minute_calls = [k for k in call.kwargs if "period" in k]
    assert {(str(k["symbol"]), str(k["period"])) for k in minute_calls} == {
        (symbol, period) for symbol in SYMBOLS for period in ("5", "30", "60")
    }
    assert {k["adjust"] for k in minute_calls} == {""}
    assert {"start_date" not in k for k in minute_calls} == {True}


def test_the_command_line_reports_a_failed_capture_by_its_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """定时任务与人都只看退出码：源挂了却 exit 0，等于把缺样本写进"成功"那一栏。"""
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))

    def boom(**_kwargs: object) -> FakeFrame:
        raise RuntimeError("源不可达")

    assert cg.main(["20240102", "20240131", "sh600519"], call=boom, relay_call=_bse_relay()) == 1
    assert "没抓到" in capsys.readouterr().err
    assert (tmp_path / "golden" / cg.MANIFEST_NAME).is_file()  # 失败也要留下可查的清单


def test_a_full_capture_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """成功路径也要有人走一遍：退出码写反了，日报上"抓到"和"没抓到"就反了。"""
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))
    # 地板（20240830）**之后**至少一个交易日：百度回填才有得抓，0 天会被判 empty。
    _write_calendar(tmp_path, "2024-01-02", "2024-09-02")
    one = _bse_relay(["920229.BJ", "N世纪", "20260922"])
    rc = cg.main(
        ["20240102", "20240131", "sh600519"], call=lambda **_k: FakeFrame(RAW), relay_call=one
    )
    assert rc == 0
    assert "需用户批准" in capsys.readouterr().out
    # 停牌两份真的落了盘（覆盖检查的运行时对偶：key 在 ≠ 文件在）
    out = tmp_path / "golden"
    assert (out / f"{cg.EM_SUSPEND_KEY}.csv").is_file()
    assert (out / f"{cg.BAIDU_SUSPEND_KEY}.csv").is_file()
