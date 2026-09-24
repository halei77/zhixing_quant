"""`news` 表的数据侧验收（04 §五 通用清单；读真实数据根，缺样本/缺盘则 skip 点名）。

1. **黄金样本重放**（L2）：真实响应快照落 `${ZX_DATA_ROOT}/golden/`（05 Q1-③：采纳进
   `tests/golden/` 需用户批准）。源改列、改主键形状、改时间戳格式 → 解析立刻红。

   2026-09-25 实测留档（三票 90 天窗，`news_em_notice_list-2026-09-25.json`）：
   600519=6、300308=49、000001=17 条；`art_code` 为 AN+数字；`notice_date −
   date(display_time) ∈ {0: 35, 1: 37}`（盘后发布归次日），恒 ≥ 0；正文样本 6 条
   5000 字页顶到 603 字短公告全非空。

2. **行级锚注册在代码里**（ADR-0015 决定 3）：未来公告日、公告日期早于挂网日 → problems
   （整批拒）；空标题/空正文 → 跳行计数——三条判据的实测依据见探测报告 §2.2/§2.3。

3. **独立锚点抽样对账 ≥20 组**（ADR-0015 决定 3 / 04 §五）：盘上 news 与**巨潮 cninfo**
   （akshare `stock_zh_a_disclosure_report_cninfo`，与东财不同上游）按
   (symbol, 归一化标题, 公告日期) 精确对表。2026-09-25 黄金样本实测 **66 组**
   （600519 6、300308 47、000001 13；归一化 = 去标点 + **全局去股票名**——东财标题是
   "名:名标题"、巨潮有的带名有的不带，只剥前缀会在 600519 上 0 配对）。

4. **回填可续跑**：注入假 lister/reader，首跑落 N 行、复跑判据读盘 0 新增（幂等）、
   连续失败熔断退出码 2——形态对齐 zx-relay backfill。
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from zhixing_quant import config
from zhixing_quant.sources.akshare import notice
from zhixing_quant.storage.tables import NewsRow, read_table

GOLDEN_LIST = "news_em_notice_list-2026-09-25.json"
GOLDEN_CONTENT = "news_em_notice_content-2026-09-25.json"
GOLDEN_CNINFO = "news_cninfo_anchor-2026-09-25.json"

SYMBOLS = ("600519", "300308", "000001")
NAMES = {"600519": "贵州茅台", "300308": "中际旭创", "000001": "平安银行"}
#: 黄金列表三票的行数（total_hits 实测，2026-09-25 窗口 20260627..20260925）。
EXPECTED_LISTED = {"600519": 6, "300308": 49, "000001": 17}
#: 抽样对账下限（04 §五：随机 ≥20 个组）与实测值（66）。
MIN_GROUPS = 20
MEASURED_GROUPS = 66


def _golden(name: str) -> dict[str, Any]:
    path = config.golden_dir() / name
    if not path.is_file():
        pytest.skip(f"缺黄金样本 {path}——重放命令见模块 docstring，缺了不冒充验收过")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


# ── 1) 黄金重放：列表与正文两种响应形状 ────────────────────────────────────────


def test_the_golden_list_parses_to_the_expected_shape() -> None:
    golden = _golden(GOLDEN_LIST)
    all_codes: set[str] = set()
    rows: list[notice.NewsRow] = []
    for symbol in SYMBOLS:
        items = notice.parse_notice_list(golden["responses"][symbol], symbol=symbol)
        assert len(items) == EXPECTED_LISTED[symbol], f"{symbol} 金侧行数漂了"
        for item in items:
            assert item.art_code.startswith("AN") and item.art_code[2:].isdigit()
            assert item.art_code not in all_codes, f"{item.art_code} 跨票/跨页重复"
            all_codes.add(item.art_code)
            assert item.title, "列表行不该有空标题（有就该在锚上被拒）"
            rows.append(notice.row_of(item, "正文占位"))

    problems, unanchored, kept = notice.anchor_news(rows, today=date(2026, 9, 25))
    assert problems == [], f"三票黄金列表过行级锚不该有问题：{problems[:3]}"
    assert unanchored == 0 and len(kept) == len(rows)
    # 时序不变量（探测报告 §2.2）：公告日期恒不早于挂网日
    for row in kept:
        assert row.ann_date >= date.fromisoformat(row.publish_time[:10])


def test_the_golden_content_parses_to_original_text() -> None:
    golden = _golden(GOLDEN_CONTENT)
    texts = [
        notice.parse_notice_content(entry["content"]) for entry in golden["responses"].values()
    ]
    assert len(texts) == 6, "三票各 2 条正文样本"
    for text in texts:
        assert len(text) >= 500, "正文样本最短 603 字——小于它说明解析截了源"
    assert any("证券代码" in text for text in texts), "A 股公告正文起手式在场"


def test_the_rows_the_two_layers_build_are_the_same_shape() -> None:
    """行模型互认（storage ↔ sources 同形同序，`storage.tables` 模块说明的约定）：
    漂了会由 write_table 的行形状检查当场响，这里把两边的列名单钉成同一个元组。"""
    assert notice.NewsRow._fields == NewsRow._fields
    assert notice.NewsRow._fields == (
        "source",
        "symbol",
        "ann_date",
        "publish_time",
        "art_code",
        "title",
        "content",
    )


# ── 2) 行级锚：判据注册在代码里 ────────────────────────────────────────────────


def test_the_row_level_guard_is_registered_in_code_not_just_in_docs() -> None:
    today = date(2026, 9, 25)
    clean = notice.row_of(
        notice.NoticeItem(
            "600519",
            date(2026, 9, 24),
            "2026-09-24 20:24:36:584",
            "AN202609241829871808",
            "某某:关于回购的公告",
        ),
        "正文非空",
    )
    future = clean._replace(ann_date=date(2026, 9, 26))
    reversed_time = clean._replace(ann_date=date(2026, 9, 23))  # 公告日早于挂网日
    empty_body = clean._replace(content="   ")
    empty_title = clean._replace(title="")
    bad_clock = clean._replace(publish_time="not-a-date")

    problems, unanchored, kept = notice.anchor_news(
        [clean, future, reversed_time, empty_body, empty_title, bad_clock], today=today
    )
    assert len(problems) == 3, "未来公告日、时序反转、时钟不可解析 → 三条 problems"
    assert any("在未来" in problem for problem in problems)
    assert any("早于挂网日" in problem for problem in problems)
    assert unanchored == 2, "空正文、空标题 → 跳行计数（源没给原文，不是形状错）"
    assert kept == [clean], "唯一干净的行才入库"


# ── 3) 独立锚点：盘上 news × 巨潮黄金 ≥20 组 ──────────────────────────────────


def _norm(title: Any, name: str) -> str:
    """标题归一化：去标点 + **全局去股票名**。

    东财标题形如「贵州茅台:贵州茅台关于…」（名:名标题），巨潮有的带名有的不带——
    只剥前缀会在 600519 上把两边归一成不同串（实测 0 配对），全局替换才对齐
    （2026-09-25 实测三票 66 组精确）。
    """
    text = re.sub(r"[\s:：（）()【】\[\]<>《》“”\"'·、,，。.－—\-]", "", str(title))
    return text.replace(name, "")


def test_the_independent_anchor_matches_cninfo_on_at_least_20_groups() -> None:
    golden = _golden(GOLDEN_CNINFO)
    window_dates = [
        date.fromisoformat(str(row["公告时间"])[:10])
        for rows in golden["rows"].values()
        for row in rows
    ]
    start, end = min(window_dates), max(window_dates)

    matched = 0
    checked = 0
    for symbol in SYMBOLS:
        name = NAMES[symbol]
        disk = read_table("news", symbol, start, end, root=config.parquet_dir())
        if not disk:
            pytest.skip(
                "缺 news 盘——先跑 `uv run --frozen python -m "
                "zhixing_quant.sources.akshare.notice --symbols 600519,300308,000001`"
            )
        index: dict[str, set[date]] = {}
        for row in disk:
            index.setdefault(_norm(row.title, name), set()).add(row.ann_date)
        for row in golden["rows"][symbol]:
            checked += 1
            key = _norm(row["公告标题"], name)
            announced = date.fromisoformat(str(row["公告时间"])[:10])
            if announced in index.get(key, set()):
                matched += 1

    assert matched >= MIN_GROUPS, (
        f"盘上 news 与巨潮只有 {matched} 组 (symbol, 标题, 日期) 精确对上（要求 ≥{MIN_GROUPS}，"
        f"黄金实测 {MEASURED_GROUPS}）——对不上说明盘被换了数或归一化漂了"
    )


# ── 4) 回填可续跑：判据读盘幂等 + 熔断 ────────────────────────────────────────


def _fake_payload(codes: list[str]) -> dict[str, Any]:
    """一页形状与真响应一致的假列表（art_code/双时间戳/标题）。"""
    return {
        "data": {
            "total_hits": len(codes),
            "list": [
                {
                    "art_code": code,
                    "display_time": "2026-09-10 20:41:29:380",
                    "notice_date": "2026-09-10 00:00:00",
                    "title": "某某股份:关于变更回购方案的公告",
                }
                for code in codes
            ],
        }
    }


def _run_backfill(tmp_path: Path, lister: Any, reader: Any) -> tuple[str, int]:
    return notice.backfill(
        ["600519"],
        days=90,
        root=tmp_path / "data",
        directory=tmp_path / "reports",
        attempts=1,
        breaker=2,
        pace=0.0,
        sleep=lambda _seconds: None,
        now=datetime(2026, 9, 25, 12, 0),
        lister=lister,
        reader=reader,
    )


def test_backfill_resumes_from_disk_so_a_rerun_adds_nothing(tmp_path: Path) -> None:
    """判据读盘 + 主键幂等：首跑落 2 行，复跑同窗口 0 新增——"续跑重跑零改动"的机器证据。"""
    codes = ["AN202609101827994401", "AN202609101827994402"]
    calls = {"list": 0}

    def lister(symbol: str, begin: date, end: date, **_kwargs: object) -> dict[str, Any]:
        assert symbol == "600519" and begin <= date(2026, 9, 10) <= end, "窗口要罩住样本公告"
        calls["list"] += 1
        return _fake_payload(codes)

    def reader(art_code: str) -> dict[str, Any]:
        return {"data": {"notice_content": f"这是 {art_code} 的公告正文原文。"}}

    first_md, first_code = _run_backfill(tmp_path, lister, reader)
    assert first_code == 0
    assert "新增 2" in first_md, first_md
    assert (
        len(
            read_table(
                "news", "600519", date(2026, 6, 27), date(2026, 9, 25), root=tmp_path / "data"
            )
        )
        == 2
    )
    assert (tmp_path / "reports" / "news-backfill-2026-09-25.md").is_file(), "报告要归档"

    second_md, second_code = _run_backfill(tmp_path, lister, reader)
    assert second_code == 0
    assert "新增 0" in second_md, f"复跑必须 0 新增（判据读盘差集为空）：{second_md}"
    assert "已有 2" in second_md, "复跑报告要印出盘上已有 2 条——缺口是 0 不是没读盘"
    assert (
        len(
            read_table(
                "news", "600519", date(2026, 6, 27), date(2026, 9, 25), root=tmp_path / "data"
            )
        )
        == 2
    ), "复跑不许改盘"


def test_backfill_trips_the_breaker_after_consecutive_failures(tmp_path: Path) -> None:
    """连续失败到 --breaker 就熔断收工（退出码 2，与 zx-relay 同义），不把票池耗完。"""

    def broken_lister(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise RuntimeError("上游 503")

    markdown, code = notice.backfill(
        ["600519", "300308", "000001"],
        days=90,
        root=tmp_path / "data",
        directory=tmp_path / "reports",
        attempts=1,
        breaker=2,
        pace=0.0,
        sleep=lambda _seconds: None,
        now=datetime(2026, 9, 25, 12, 0),
        lister=broken_lister,
        reader=lambda _code: {"data": {"notice_content": "x"}},
    )
    assert code == 2, "两票连败后第三票该被熔断，整跑退出码 2"
    assert markdown.count("failed") == 2, markdown
    assert "熔断跳过 1 票" in markdown, markdown
    assert "`000001` breaker" in markdown, "第三票的结局行要印成 breaker，不是 failed"
