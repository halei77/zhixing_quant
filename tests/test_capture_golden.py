"""黄金样本抓取工具 tools/capture_golden.py（03 §二 L2；Step 2a）。

真联网的那几个函数在这里不被调用：抓取边界是注入的，测试用假 fetcher 落 tmp 目录，
测的是**样本格式**——它一旦进仓就成了断言的一部分，格式错了整批样本都白落。

最要紧的一条是重放保真：源当时给 NaN，样本读回来还得让适配器判出 NaN。落成空串会把
"源给了个不成立的数"变成"字段没给"，日报上那是两条不同的诊断（见 sources/rows）。
"""

import math
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import capture_golden as cg  # noqa: E402  # tools/ 不在包内，按脚本路径导入

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


def test_fetcher_keys_encode_the_window_and_the_symbol() -> None:
    """key 就是文件名，所以参数必须写在 key 里：换窗口=换 key，不覆盖已批准的样本。"""
    fetchers = cg.build_fetchers(("20240102", "20240131"), ("sh600519", "sz300750"))
    assert "stock_zh_a_daily__sh600519__20240102_20240131__hfq" in fetchers
    assert "tool_trade_date_hist_sina" in fetchers


def test_each_symbol_binds_its_own_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """闭包捕获循环变量的经典事故：所有 key 都抓最后一只票，样本名字却全都对得上。"""
    asked: list[dict[str, object]] = []

    def fake(function: str, **kwargs: object) -> list[dict[str, Any]]:
        if function == "stock_zh_a_daily":
            asked.append(kwargs)
        return []

    monkeypatch.setattr(cg, "_fetch_akshare", fake)
    fetchers = cg.build_fetchers(("20240102", "20240131"), ("sh600519", "sz300750"))
    for key, fetcher in fetchers.items():
        assert fetcher() == [], key
    assert len(asked) == 4  # 两只票 × raw/hfq
    assert {(str(k["symbol"]), str(k["adjust"])) for k in asked} == {
        ("sh600519", ""),
        ("sh600519", "hfq"),
        ("sz300750", ""),
        ("sz300750", "hfq"),
    }
    assert {str(k["start_date"]) for k in asked} == {"20240102"}


def test_the_command_line_reports_a_failed_capture_by_its_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """定时任务与人都只看退出码：源挂了却 exit 0，等于把缺样本写进"成功"那一栏。"""
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))

    def boom(function: str, **_kwargs: object) -> list[dict[str, Any]]:
        raise RuntimeError(f"{function} 不可达")

    monkeypatch.setattr(cg, "_fetch_akshare", boom)
    assert cg.main(["20240102", "20240131", "sh600519"]) == 1
    assert "没抓到" in capsys.readouterr().err
    assert (tmp_path / "golden" / cg.MANIFEST_NAME).is_file()  # 失败也要留下可查的清单


def test_a_full_capture_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """成功路径也要有人走一遍：退出码写反了，日报上"抓到"和"没抓到"就反了。"""
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(cg, "_fetch_akshare", lambda *_args, **_kwargs: RAW)
    assert cg.main(["20240102", "20240131", "sh600519"]) == 0
    assert "需用户批准" in capsys.readouterr().out
