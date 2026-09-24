"""基本面骨架 L1：stockshare / finacereport 的解析 → NamedTuple（合成响应全覆盖）。

真实响应的那份在 `test_golden.py`（数据根黄金样本）；这里用合成形状钉解析规则本身：
`%` 剥掉存百分点、`*origin` 数值孪生优先、`FiledName` 对齐禁下标、`--` 记 None。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from zhixing_quant.sources.ngw import fundamentals


def test_stock_share_parses_identity_valuation_and_units() -> None:
    body: dict[str, Any] = {
        "stockcode": "600519",
        "stockname": "贵州茅台",
        "innercode": "3143",
        "boardname": "主板",
        "platename": "白酒",
        "exchangeMarket": "SH",
        "nowv": "1237.00",
        "preclose": "1251.24",
        "updown": "-14.24",
        "updownrate": "-1.14%",
        "pe": "17.37",
        "pettm": "18.99",
        "pb": "6.16",
        "ps": "8.93",
        "epsttm": "35.5700",
        "roe": "16.75%",
        "dividendRatio": 4.1577,
        "turnoverrate": "0.25%",
        "totalstocknumorigin": 1250081601,
        "publicfloatshareqtyorigin": 1250081601,
        "totalstockvalueorigin": 1546350940437,
        "totalfloatsharevalorigin": 1546350940437,
        "totaltradevolume": "3123935",
        "totalvaluetrade": "3867310920",
        "suspend": "1",
        "openstatus": "1",
    }
    row = fundamentals.stock_share_of(body)
    assert (row.stockcode, row.stockname, row.industry, row.boardname, row.exchange) == (
        "600519",
        "贵州茅台",
        "白酒",
        "主板",
        "SH",
    )
    assert (row.price, row.preclose, row.updown) == (1237.0, 1251.24, -14.24)
    # 百分点不除 100：16.75 意思就是 16.75%——偷偷变比率，下游会当 1675% 用。
    assert (row.updown_rate_pct, row.roe_pct, row.turnover_pct) == (-1.14, 16.75, 0.25)
    assert (row.pe, row.pe_ttm, row.pb, row.ps, row.eps_ttm) == (17.37, 18.99, 6.16, 8.93, 35.57)
    assert row.dividend_yield_pct == 4.1577  # 源给的已是数字百分点，不带 %
    assert row.total_mv == 1546350940437.0  # `*origin` 存值，不解析「1.546万亿」显示串
    assert row.total_shares == 1250081601.0
    assert (row.volume, row.amount) == (3123935.0, 3867310920.0)
    assert row.suspended is True  # suspend == "1" 才算停牌


def test_stock_share_missing_keys_stay_none_rather_than_zero() -> None:
    """骨架不替源猜值：缺键给 None/空串，0 与「没给」是两条不同的结论（04 §五）。"""
    row = fundamentals.stock_share_of({})
    assert row.stockcode == ""
    assert row.price is None
    assert row.pe is None
    assert row.roe_pct is None
    assert row.suspended is False


def test_finacereport_cells_align_by_filed_name_and_skip_title_rows() -> None:
    body: dict[str, Any] = {
        "ColumnInfo": [
            {"Name": "关键指标", "FiledName": "", "IsTitle": 1},
            {"Name": "营业总收入", "FiledName": "TotalOperatingRevenue", "IsTitle": 0},
            {"Name": "净利润", "FiledName": "NetProfit", "IsTitle": 0},
        ],
        "ValueInfo": [
            {
                "EndDate": "2026-06-30",
                "Types": 2,
                "Datas": [
                    {"FiledName": "", "Value": "", "OriginValue": 0.0, "Rate": ""},
                    {
                        "FiledName": "TotalOperatingRevenue",
                        "Value": "922.78亿",
                        "OriginValue": 92278072083.21,
                        "Rate": "1.30%",
                    },
                    {
                        "FiledName": "NetProfit",
                        "Value": "460.33亿",
                        "OriginValue": 46033330566.78,
                        "Rate": "-2.03%",
                    },
                ],
            },
            {
                "EndDate": "1998-12-31",
                "Types": 4,
                "Datas": [
                    {
                        "FiledName": "TotalOperatingRevenue",
                        "Value": "--",
                        "OriginValue": 0.0,
                        "Rate": "--",
                    },
                ],
            },
        ],
    }
    cells = fundamentals.fina_report_rows(body)
    # 标题行（FiledName 空）不在结果里；`--` 记 None（0 与无数据是两件事）。
    assert [(cell.end_date, cell.filed_name) for cell in cells] == [
        (date(2026, 6, 30), "TotalOperatingRevenue"),
        (date(2026, 6, 30), "NetProfit"),
        (date(1998, 12, 31), "TotalOperatingRevenue"),
    ]
    assert cells[0].metric == "营业总收入"
    assert cells[0].value == 92278072083.21
    assert cells[0].rate_pct == 1.3
    assert cells[2].value is None and cells[2].rate_pct is None
    # 保源序（报告期倒序），骨架不排序。
    assert cells[0].end_date and cells[2].end_date and cells[0].end_date > cells[2].end_date


def test_report_types_names_match_the_four_measured_values() -> None:
    """四个 reporttype 的中文名是实测内容钉的（接口描述.txt 的行签易读反，响应不会）。"""
    assert fundamentals.REPORT_TYPES == {
        "1": "资产负债表",
        "2": "利润表",
        "3": "现金流",
        "4": "主要指标",
    }
