"""ngw L2 黄金样本：真响应 → 期望 `BarDraft`，外加与 sina 1m 的锚点对账（04 §五）。

样本按 05 Q1-③ 的边界落在**数据根** `golden/`（`$ZX_DATA_ROOT/golden/`），不进
`tests/golden/`——真实快照进仓要用户批准，所以这里缺文件就 skip 并把该跑的工具说清楚；
本机（门禁运行的机器）上样本在场，测试必须真跑真红。

文件：
- `ngw_kline__sh600519__1min.json` + `.expected.json`：K线真响应与**独立按规范生成**的
  期望行（ts 映射断言就在样本里）；
- `ngw_stockshare__sh600519.json`、`ngw_finacereport__sh600519__rt4.json`：基本面两响应；
- `stock_zh_a_minute__sh600519__1min.csv`：sina 1m 独立锚点（对账用）。
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from typing import Any

import pytest

from zhixing_quant import config
from zhixing_quant.sources.ngw import fundamentals, minute

#: T-001 的跨源容差：0.5% 相对 + 0.02 元绝对，取宽者（`close_pct`/`close_abs`）。
TOLERANCE_PCT = 0.005
TOLERANCE_ABS = 0.02


def _load(name: str) -> Any:
    path = config.golden_dir() / name
    if not path.exists():
        pytest.skip(
            f"黄金样本不在 {path}：先落数据根（03 §二 L2 的样本按 05 Q1-③ 留在 "
            "$ZX_DATA_ROOT/golden/，采纳进 tests/golden/ 要用户批准）"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _kline_payload() -> dict[str, Any]:
    payload: dict[str, Any] = _load("ngw_kline__sh600519__1min.json")
    assert payload["request"]["type"] == "11"
    assert "ex" not in payload["request"], "样本必须是不复权口径（ex 不传，ADR-0009 决定 4）"
    return payload


def test_kline_golden_parses_to_the_expected_drafts() -> None:
    """真响应 → 适配器输出与**独立生成**的期望逐字段相等。期望文件不 import 适配器：
    映射改坏了这里红，而不是两边一起漂。"""
    payload = _kline_payload()
    expected = _load("ngw_kline__sh600519__1min.expected.json")
    rows = payload["response"]["timedata"]
    drafts = minute.minute_drafts(rows, symbol="600519")
    assert len(drafts) == len(expected)
    for draft, want in zip(drafts, expected, strict=True):
        assert draft.ts is not None and draft.trade_date is not None
        assert draft.ts.isoformat() == want["ts"], want["times"]
        assert draft.trade_date.isoformat() == want["trade_date"], want["times"]
        for field in ("open", "high", "low", "close", "volume", "amount"):
            assert getattr(draft, field) == want[field], (want["times"], field)
        assert draft.source == "ngw_minute_1"
        assert draft.adj_factor is None


def test_kline_golden_pins_the_session_boundaries_and_the_241_bar_day() -> None:
    """样本里把边界钉死：完整交易日 241 根（实测），四条边界 ts 逐字 == 标签（identity）。

    「241 根、含 15:00」正是推翻设计合成 §2.3「240 根、缺 15:00」的实测证据——合成那组数
    是 start 截止参数排他边界裁出来的（start=…150000 → 240 根末根 14:59）。
    """
    drafts = minute.minute_drafts(_kline_payload()["response"]["timedata"], symbol="600519")
    per_day: dict[str, list[Any]] = {}
    for draft in drafts:
        assert draft.trade_date is not None and draft.ts is not None
        per_day.setdefault(draft.trade_date.isoformat(), []).append(draft)
    full_days = {day: bars for day, bars in per_day.items() if len(bars) > 200}
    assert full_days, "样本里应当有完整交易日"
    for day, bars in full_days.items():
        assert len(bars) == 241, f"{day} 实测完整日应为 241 根（121 上午 + 120 下午）"
    day, bars = sorted(full_days.items())[-1]
    stamps = {bar.ts for bar in bars}
    year, month, int_day = (int(part) for part in day.split("-"))
    for hour, minute_, expect_present in (
        (9, 30, True),  # 开盘竞价栏
        (11, 30, True),  # 午末右端点（+1min 会把它推进午休的那种边界）
        (13, 1, True),  # 午后首根
        (14, 59, True),
        (15, 0, True),  # 末根：合成说缺，实测在
    ):
        stamp = datetime(year, month, int_day, hour, minute_)
        assert (stamp in stamps) is expect_present, stamp
    # identity 的反面：没有任何 ts 落在午休缝里。
    lunch = [
        ts
        for ts in stamps
        if datetime(year, month, int_day, 11, 30) < ts < datetime(year, month, int_day, 13, 1)
    ]
    assert lunch == []


def test_stockshare_golden_maps_measured_fields() -> None:
    """真响应 → 字段映射。断言全部对着响应原文自洽（重采样本后仍应成立）。"""
    payload = _load("ngw_stockshare__sh600519.json")
    body = payload["response"]
    row = fundamentals.stock_share_of(body)
    assert (row.stockcode, row.stockname) == ("600519", "贵州茅台")
    assert row.price == float(body["nowv"])
    assert row.pe == float(body["pe"])
    assert row.roe_pct == float(str(body["roe"]).rstrip("%"))
    assert row.total_mv == float(body["totalstockvalueorigin"])
    assert row.industry == body["platename"]


def test_finacereport_golden_has_periods_cells_and_no_ann_date() -> None:
    """真响应 → 格子序列。**只有 EndDate（报告期），没有发布日**——ngw 供给不了点时可见性，
    Forward PE 的正源因此仍是 rds `report_rc`（其 `report_date` 是研报发布日，见探测报告）。"""
    payload = _load("ngw_finacereport__sh600519__rt4.json")
    body = payload["response"]
    cells = fundamentals.fina_report_rows(body)
    periods = body["ValueInfo"]
    assert cells, "主要指标 103 期（1998→2026）不应当解析出 0 行"
    assert len({cell.end_date for cell in cells}) == len(periods)
    first = periods[0]
    revenue = next(cell for cell in cells if cell.filed_name == "TotalOperatingRevenue")
    assert revenue.end_date is not None and revenue.end_date.isoformat() == first["EndDate"]
    raw = next(data for data in first["Datas"] if data.get("FiledName") == "TotalOperatingRevenue")
    assert revenue.value == float(raw["OriginValue"])
    assert revenue.metric == "营业总收入"
    # 源键清单里没有 ann_date / 发布日——这一条就是「做不了点时可见性」的机器证据。
    column_keys = {key for column in body["ColumnInfo"] for key in column}
    assert not {"ann_date", "ann_dt", "publish_date"} & column_keys
    assert "EndDate" in first and "ann_date" not in first


def test_ngw_vs_sina_close_within_the_t001_tolerance() -> None:
    """L1 锚点对账：identity 对齐后抽 20 组，close 偏差按 T-001「0.5% + 0.02 元」判
    （04 §五 通用清单第 4 条：与主源抽样交叉对账）。

    对账窗实测（2026-09-24，本样本对：ngw 1400 根 vs sina 1970 根，identity 全重叠
    1383 对）：|Δclose| **mean = 0.0761 元、max = 2.42 元**；其中 **446/1383 根超过 0.02 元
    绝对容差**（最差 `20260923094100` 差 2.42 元、`20260923093100` 差 1.52 元——开盘首分钟
    两源分桶差异最甚），但在合成容差 0.5%+0.02 元（≈6.3 元 @1250）下 **0 根超限**。
    调研那轮（设计合成引用）报 mean 0.071 / max 0.70（qfq 口径、重叠 61 根 exact-ts）——
    口径与样本窗不同，本轮实测以 0.0761 / 2.42 为准。**只按 0.02 元绝对容差判会在这 446 根上
    全部误报**：那些根要逐根查的就是「分桶差异 vs 真错价」，判据必须是合成容差。
    """
    ngw_payload = _kline_payload()
    sina_path = config.golden_dir() / "stock_zh_a_minute__sh600519__1min.csv"
    if not sina_path.exists():
        pytest.skip(f"sina 1m 锚点样本不在 {sina_path}：先落数据根 golden/")
    ngw_close = {
        str(row["times"]): int(row["nowv"]) / 100.0 for row in ngw_payload["response"]["timedata"]
    }
    with sina_path.open(encoding="utf-8") as handle:
        sina_rows = list(csv.DictReader(handle))
    pairs: list[tuple[str, float, float]] = []
    for row in sina_rows:
        key = row["day"].replace("-", "").replace(" ", "").replace(":", "")
        if key in ngw_close:
            pairs.append((key, ngw_close[key], float(row["close"])))
    assert len(pairs) >= 20, f"重叠组数不足：{len(pairs)}"
    pairs.sort()
    step = len(pairs) // 20
    sampled = pairs[::step][:20]
    for key, ours, theirs in sampled:
        price = max(ours, theirs)
        assert abs(ours - theirs) <= TOLERANCE_PCT * price + TOLERANCE_ABS, (
            key,
            ours,
            theirs,
        )
