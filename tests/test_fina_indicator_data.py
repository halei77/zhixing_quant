"""`fina_indicator` 的数据侧验收（04 §五 转接专项；读真实数据根，缺样本/缺盘则 skip 点名）。

1. **黄金样本重放**（L2）：真实响应快照落 `${ZX_DATA_ROOT}/golden/relay/`（05 Q1-③：
   采纳进 `tests/golden/` 需用户批准，所以样本留在数据根）。源改列、改脏值形状 → 解析
   计数立刻红。缺样本时 skip 并点名重放命令——CI 没数据根是常态，本机门禁有样本就是全量验。

   2026-09-25 实测留档（600519.SH 无窗口 limit=5000 一发）：**100 行**（静默顶，实测
   limit≥101 一律 100）、默认响应 **不带 `update_flag` 列**、孪生在指标列上同值——
   100 行里 55 个 (ann,end) 键、45 对孪生，解析去重后 **55 行**、全部季末、全部
   ann_date ≥ end_date、三列指标 0 个 null。

2. **独立锚点抽样对账 ≥20 组**（ADR-0015 决定 3 / 04 §五「独立锚点照常，不因付费源免」）：
   盘上 fina_indicator 与**两个不同上游**（东财业绩报表、新浪财务指标——都与 rds 的
   Tushare 谱系不同源）在同一量纲上对表，不是同谱系自证（ADR-0014 后果）。

   **判据是校准出来的，实测留档**（2026-09-25，3 票 × 10 期锚点黄金样本）：
   - `tr_yoy` vs 东财「营业总收入-同比增长」：30/30 精确（max 4.7e-5，四舍五入级）
     → 容差 **0.01pp ≥95%**；
   - `netprofit_yoy` vs 东财「净利润-同比增长」：30/30 ≤0.04pp（东财给两位小数）
     → 容差 **0.05pp ≥95%**；
   - `roe_waa` vs 新浪「加权净资产收益率」：29/30 ≤0.05pp，唯一离群 000001 2026Q1
     （rds 0.24 vs 新浪/东财双源 2.83——源侧单格异常，两独立源互相印证）→
     **容差 0.05pp ≥90%**（通过率吸收并留档，不为一格放宽容差也不删组）。
   锚点只按 **(symbol, end_date)** 连接：东财黄金样本里的 `ann_date` 是它的「最新公告
   日期」，实测 30 组里只有 12 组等于 rds 首披日（旧行给的是后续再公告日）——**不许**
   拿它当时点时钟（03-L4）。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from zhixing_quant import config
from zhixing_quant.sources.relay.tables import parse_fina_indicator
from zhixing_quant.storage import layout, tables

GOLDEN = "fina_indicator-2026-09-25-600519-rds.json"
YJBB_ANCHOR = "yjbb_roe_anchor-2026-09-25.json"
SINA_ANCHOR = "sina_roe_anchor-2026-09-25.json"
#: 抽样对账的下限（04 §五：随机 ≥20 个组）与三道通过率——校准过程见模块 docstring。
MIN_GROUPS = 20
TR_TOLERANCE_PP = 0.01
TR_PASS_RATE = 0.95
NP_TOLERANCE_PP = 0.05
NP_PASS_RATE = 0.95
ROE_TOLERANCE_PP = 0.05
ROE_PASS_RATE = 0.90


def _golden(name: str) -> dict[str, Any]:
    path = config.golden_dir() / name
    if not path.is_file():
        pytest.skip(f"缺黄金样本 {path}——锚点对账要有独立源的留档，缺了不冒充验收过")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def test_the_golden_snapshot_parses_to_the_expected_shape() -> None:
    path = config.golden_dir() / "relay" / GOLDEN
    if not path.is_file():
        pytest.skip(
            f"缺黄金样本 {path}——重放：zx-relay capture --table fina_indicator "
            "--day 2026-09-25 --symbols 600519（ADR-0015 决定 4）"
        )
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    data = body["data"]
    fields = [str(f) for f in data["fields"]]

    assert len(data["items"]) == 100, "样本是无窗口 limit 一发（静默顶的整页）"
    assert "update_flag" not in fields, "默认响应不带 update_flag（2026-09-25 实测）"
    rows = parse_fina_indicator("rds", fields, data["items"])
    # 100 行 → 55 键：45 对孪生在指标列上同值，去重后各留一行（源的真实形状）
    assert len(rows) == 55
    assert len({(r.ann_date, r.end_date) for r in rows}) == 55, "主键唯一"
    assert min(r.end_date for r in rows) == date(2012, 12, 31)
    assert max(r.end_date for r in rows) == date(2026, 6, 30)
    assert min(r.ann_date for r in rows) == date(2013, 3, 29)
    assert max(r.ann_date for r in rows) == date(2026, 8, 15)
    assert {r.symbol for r in rows} == {"600519"}
    # 行级锚的三条在真实样本上全过（与 relay_cli._anchor_fina_indicator 同判据）
    assert all(r.end_date.month in (3, 6, 9, 12) for r in rows), "非季末报告期不许进表"
    assert all(r.ann_date >= r.end_date for r in rows), "首披日早于报告期 = 时序不成立"
    assert all(r.ann_date <= date(2026, 9, 25) for r in rows), "首披日不许在未来"
    # 最新 100 行里没有 null（2012 年后的 600519 三列全有值）——源改了 null 形状这里红
    assert all(
        getattr(r, metric) is not None
        for r in rows
        for metric in ("roe_waa", "tr_yoy", "netprofit_yoy")
    )


def test_growth_and_roe_agree_with_two_independent_upstreams_on_20_groups() -> None:
    """独立锚点：tr_yoy/netprofit_yoy 对东财、roe_waa 对新浪，≥20 组、三道通过率各达标。"""
    symbols = layout.dataset_symbols("fina_indicator")
    if not symbols:
        pytest.skip(
            "盘上还没有 fina_indicator——先跑 zx-relay backfill --table fina_indicator "
            "--symbols 600519,300308,000001 再过这道锚"
        )
    yjbb_rows = _golden(YJBB_ANCHOR)["rows"]
    sina_rows = _golden(SINA_ANCHOR)["rows"]
    # 只按 (symbol, end_date) 连接：东财的 ann_date 不是首披日（见模块 docstring）
    yjbb = {(r["symbol"], r["end_date"]): r for r in yjbb_rows}
    sina = {(r["symbol"], r["end_date"]): r for r in sina_rows}

    tr_devs: list[float] = []
    np_devs: list[float] = []
    roe_devs: list[float] = []
    end = date.today()
    for symbol in symbols:
        for row in tables.read_table("fina_indicator", symbol, date(1985, 1, 1), end):
            key = (symbol, row.end_date.strftime("%Y%m%d"))
            if key in yjbb:
                anchor = yjbb[key]
                if row.tr_yoy is not None and anchor["tr_yoy"] is not None:
                    tr_devs.append(abs(row.tr_yoy - anchor["tr_yoy"]))
                if row.netprofit_yoy is not None and anchor["netprofit_yoy"] is not None:
                    np_devs.append(abs(row.netprofit_yoy - anchor["netprofit_yoy"]))
            if key in sina and row.roe_waa is not None:
                roe_devs.append(abs(row.roe_waa - sina[key]["roe_waa"]))

    groups = max(len(tr_devs), len(np_devs), len(roe_devs))
    assert groups >= MIN_GROUPS, (
        f"可对账组数 {groups} < {MIN_GROUPS}：盘上行或锚点黄金样本不够——"
        "锚对不起来就不算验收过（04 §五）"
    )
    tr_rate = sum(1 for dev in tr_devs if dev <= TR_TOLERANCE_PP) / len(tr_devs)
    np_rate = sum(1 for dev in np_devs if dev <= NP_TOLERANCE_PP) / len(np_devs)
    roe_rate = sum(1 for dev in roe_devs if dev <= ROE_TOLERANCE_PP) / len(roe_devs)
    assert tr_rate >= TR_PASS_RATE, (
        f"tr_yoy pooled {len(tr_devs)} 组只有 {tr_rate:.1%} 落在 {TR_TOLERANCE_PP}pp 内"
        f"（要求 ≥{TR_PASS_RATE:.0%}）——与东财营业总收入同比对不上"
    )
    assert np_rate >= NP_PASS_RATE, (
        f"netprofit_yoy pooled {len(np_devs)} 组只有 {np_rate:.1%} 落在 {NP_TOLERANCE_PP}pp 内"
        f"（要求 ≥{NP_PASS_RATE:.0%}）——与东财净利润同比对不上"
    )
    assert roe_rate >= ROE_PASS_RATE, (
        f"roe_waa pooled {len(roe_devs)} 组只有 {roe_rate:.1%} 落在 {ROE_TOLERANCE_PP}pp 内"
        f"（要求 ≥{ROE_PASS_RATE:.0%}）——与新浪加权 ROE 对不上"
        "（已知单格离群 000001 2026Q1 由通过率吸收，见模块 docstring）"
    )


def test_the_row_level_guard_is_registered_in_code_not_just_in_docs() -> None:
    """行级锚与短页守卫的登记以代码事实钉住（不是探测报告里的一句话）。"""
    from zhixing_quant.sources.jobs.relay_cli import ANCHORS
    from zhixing_quant.sources.relay.client import API_RELAYS
    from zhixing_quant.sources.relay.pages import SHORT_PAGE_CAPS, WINDOW_STARTS
    from zhixing_quant.sources.relay.tables import PARSERS
    from zhixing_quant.storage.tables import SPECS

    assert "fina_indicator" in PARSERS and "fina_indicator" in ANCHORS
    spec = next(spec for spec in SPECS if spec.name == "fina_indicator")
    assert spec.date_field == "ann_date", "公告类表按首披日分区——点时的时钟在 ann_date 上"
    assert spec.key == ("ann_date", "end_date"), "重述（同 end、不同 ann）要两行并存"
    assert SHORT_PAGE_CAPS["fina_indicator"] == 100, "顶登记成 5000 会把截断读成拉完"
    assert WINDOW_STARTS["fina_indicator"].year <= 1989, (
        "全史左界必须罩住实测最早的 19891231 报告期——1990 的默认左界会静默丢它"
    )
    assert API_RELAYS["fina_indicator"] == ("rds",), "promax 实测不适配，白名单钉死 rds"
