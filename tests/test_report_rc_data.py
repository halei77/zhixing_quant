"""`report_rc` 的数据侧验收（04 §五 转接专项；读真实数据根，缺样本/缺盘则 skip 点名）。

1. **黄金样本重放**（L2）：真实响应快照落 `${ZX_DATA_ROOT}/golden/relay/`（05 Q1-③：
   采纳进 `tests/golden/` 需用户批准，所以样本留在数据根）。源改列、改脏值形状 → 解析
   计数立刻红。缺样本时 skip 并点名重放命令——CI 没数据根是常态，本机门禁有样本就是全量验。
2. **独立锚点抽样对账 ≥20 组**（ADR-0015 决定 3 / 04 §五「独立锚点照常，不因付费源免」）：
   `report_rc` 的 `pe × eps` 是**研报引用价的隐含值**，与干净区日线（akshare 主源）的收盘
   互核——两个不同上游（Tushare 谱系 vs akshare）在同一量纲（价格，元）上对表，不是同谱系
   自证（ADR-0014 后果）。

   **判据是校准出来的，两轮实测都留档**（2026-09-25，三票 20,900 行真跑后）：
   - 第一版「vs 前一交易日收盘、5% 内 ≥90%」——只用 600519 单票样本定的（该票 98.7% 过），
     三票 pooled 实测 **89.4%** 直接测红。红得对：单票样本当全体用是坑 #40 的老病。
   - 诊断（逐行看）：`pe` 是研报**引用价**口径——同一券商同日各行 pe×eps 自洽（相对极差
     均值 0.4%），不同券商同日各引各的价（2024-03-14 中信建投引 10.8、国投引 10.21，
     同日干净区收盘 10.23），引用价通常同日或前一日、暴涨票可滞后数日。eps/主键/日期没有
     问题——歪的是"拿哪天的价"这个自由度。
   - 定版：隐含价对「同日收盘或前一交易日收盘」取更近者、**10% 内**；分票 ≥90% 且
     pooled ≥95%、总组数 ≥20。三票实测：pooled ≤10% **98.17%**、分票 98.9/94.7/99.8
     （min 口径中位偏差 0.04–1.66%）。10% 抓的是错票/错单位/错日期那一族（那会掉到 0%
     附近），不假装能判"引用价差了两天"——后者逐行硬闸会把真数据永远挡在门外（源自带
     陈旧 pe 最大偏 88.8%）。
3. **offset≥5000 实测结论的机器钉**：守卫存在性与"这一族不许 offset 翻页"由代码事实断言。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from zhixing_quant import config
from zhixing_quant.sources.relay.tables import parse_report_rc
from zhixing_quant.storage import layout, tables
from zhixing_quant.storage.query import read_bars

GOLDEN = "report_rc-600519-rds-20260925.json"
#: 抽样对账的下限（04 §五：随机 ≥20 个组）与两道通过率——校准过程见模块 docstring。
MIN_GROUPS = 20
TOLERANCE = 0.10
PASS_RATE = 0.95
PER_SYMBOL_PASS_RATE = 0.90


def test_the_golden_snapshot_parses_to_the_expected_shape() -> None:
    path = config.golden_dir() / "relay" / GOLDEN
    if not path.is_file():
        pytest.skip(
            f"缺黄金样本 {path}——重放：对 report_rc 拉一份 ts_code=600519.SH "
            "limit=5000 的真实响应存进数据根 golden/relay/（ADR-0015 决定 4）"
        )
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    data = body["data"]
    rows = parse_report_rc("rds", [str(f) for f in data["fields"]], data["items"])

    assert len(data["items"]) == 5000, "样本是 limit=5000 那一发（静默顶的整页）"
    # 23 = 源自带的空 eps 行（含 quarter 为 null/'Q' 的 7 行，两者在样本里重叠）——行级拒，
    # 拒完剩 4977。源改了脏值形状，这三个数会对不上，红在这里。
    assert len(rows) == 4977
    assert min(r.report_date for r in rows) == date(2021, 8, 26)
    assert max(r.report_date for r in rows) == date(2026, 9, 11)
    assert len({r.report_date for r in rows}) == 571
    assert len({(r.report_date, r.org_name, r.quarter) for r in rows}) == len(rows), "主键唯一"
    assert all(len(r.quarter) == 6 and r.quarter[4] == "Q" for r in rows), "脏 quarter 不许进表"
    assert {r.symbol for r in rows} == {"600519"}


def test_implied_report_price_agrees_with_clean_zone_closes_on_20_groups() -> None:
    """独立锚点：pe×eps 对同日/前收更近者，≥20 组、分票 ≥90%、pooled ≥95%（判据校准见头注）。"""
    symbols = layout.dataset_symbols("report_rc")
    if not symbols:
        pytest.skip(
            "盘上还没有 report_rc——先跑 zx-relay backfill --table report_rc "
            "--symbols 600519,300308,000001 再过这道锚"
        )
    start, end = date(1990, 1, 1), date.today()
    per_symbol: dict[str, list[float]] = {}
    for symbol in symbols:
        rows = tables.read_table("report_rc", symbol, start, end)
        if not rows:
            continue
        bars = read_bars(
            symbol, start, end, adjust="raw", dataset="daily", root=config.parquet_dir()
        )
        closes = {bar.trade_date: bar.close for bar in bars if bar.volume > 0}
        days = sorted(closes)
        prev_close = {day: closes[days[i - 1]] for i, day in enumerate(days) if i > 0}
        devs: list[float] = []
        for row in rows:
            if row.pe is None or row.eps <= 0:
                continue
            if row.report_date not in closes:
                continue  # 发布日不是有量交易日——没得对，不算对不上
            implied = row.pe * row.eps
            if implied <= 0:
                continue
            candidates = [abs(implied / closes[row.report_date] - 1.0)]
            if row.report_date in prev_close:
                candidates.append(abs(implied / prev_close[row.report_date] - 1.0))
            devs.append(min(candidates))
        if devs:
            per_symbol[symbol] = devs
    compared = [dev for devs in per_symbol.values() for dev in devs]
    assert len(compared) >= MIN_GROUPS, (
        f"可对账组数 {len(compared)} < {MIN_GROUPS}：pe 行或干净区日线不够——"
        "锚对不起来就不算验收过（04 §五）"
    )
    for symbol, devs in per_symbol.items():
        within = sum(1 for dev in devs if dev <= TOLERANCE)
        rate = within / len(devs)
        assert rate >= PER_SYMBOL_PASS_RATE, (
            f"{symbol}：pe×eps 对同日/前收 {len(devs)} 组里只有 {within} 组落在 "
            f"{TOLERANCE:.0%} 内（{rate:.1%} < {PER_SYMBOL_PASS_RATE:.0%}）——"
            "这只票的行与干净区对不上，不能靠别的票把均值抬过线"
        )
    within = sum(1 for dev in compared if dev <= TOLERANCE)
    rate = within / len(compared)
    assert rate >= PASS_RATE, (
        f"pe×eps pooled {len(compared)} 组里只有 {within} 组落在 {TOLERANCE:.0%} 内"
        f"（{rate:.1%} < {PASS_RATE:.0%}）——两个上游在同一量纲上对不上，整批可信度存疑"
    )


def test_windowed_probe_of_offset_five_thousand_is_documented_as_measured() -> None:
    """offset≥5000 的实测结论以代码事实钉住（不是文档里的一句话）。

    2026-09-25 实测：`offset=5000&limit=100` → HTTP 200 / **0 行**；窗口化查询各自仍顶
    5000 且比全史首行更早（全史首行 20210826、`20150101..20240630` 窗内到 20190329）——
    单查询静默只留最新 5000 行。本测试钉住守卫因此存在：短页族只认 page_size=5000 且
    满页必须缩窗，任何"offset 翻页"的改法都会先撞 `fetch_pages_short` 的发前拒。
    """
    from zhixing_quant.sources.relay.pages import (
        SHORT_PAGE_TABLES,
        SOURCE_ROW_CAP,
        fetch_pages_short,
    )

    assert "report_rc" in SHORT_PAGE_TABLES and SOURCE_ROW_CAP == 5000
    calls: list[dict[str, Any]] = []

    def fetch(_api: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        calls.append(dict(params))
        # 复刻实测：offset>0 的请求一律空页（跨顶/≥顶），offset=0 且窗口内不满 5000 才有行
        if params.get("offset", 0) > 0:
            return "rds", {"code": 0, "data": {"fields": ["ts_code"], "items": []}}
        return "rds", {"code": 0, "data": {"fields": ["ts_code"], "items": [["600519.SH"]]}}

    _source, items, _fields, _total = fetch_pages_short(
        "report_rc",
        {"ts_code": "600519.SH", "start_date": "20260101", "end_date": "20260131"},
        fetch=fetch,
        today=date(2026, 9, 25),
    )
    assert len(items) == 1
    assert all(call.get("offset", 0) == 0 for call in calls), (
        "这一族不许出现 offset 翻页——offset≥5000 实测空页，翻页等于把截断读成拉完"
    )
    assert calls[0]["start_date"] == "20260101" and calls[0]["end_date"] == "20260131"
