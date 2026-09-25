"""`analyst_rating` 组件（远期评级与目标价明细，#66）的钉测试。

怀疑点（事件型表家族的通用错法，news/forecast 同款教训）：

1. **点时**（03-L4）：发布日 > as_of 的研报一个字都不许出现——把下个月才发布的评级递给
   今天的模型就是未来函数。
2. **倒序截断的方向**：截 ANALYST_TOP 条必须丢最老的、保最新的；丢了最新的等于组件
   在最需要的地方自残。
3. **格子语义**：目标价「—」是"这份研报没给"，不许洗成 0 或区间端点；评级 null 保持
   「—」，不许冒充"中性"。
4. **空段出交代**（ADR-0020）：没研报就 NO_DATA_NOTE，不给只有表头的空表。
5. **schema 演进**（#66 的读侧地基）：老 7 列文件读新 Spec 不许炸——新列读成 None。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tests.fakes import bar, snapshot_root
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.site import table
from zhixing_quant.site.prompt import ANALYST_TOP, NO_DATA_NOTE, build
from zhixing_quant.site.templates import Selection, Template
from zhixing_quant.sources.relay.tables import ReportRcRow, parse_report_rc
from zhixing_quant.storage import layout
from zhixing_quant.storage.tables import write_table
from zhixing_quant.storage.write import store_bars

D1, D2, D3 = date(2026, 9, 14), date(2026, 9, 15), date(2026, 9, 16)
WEEK = (D1, D2, D3, date(2026, 9, 17), date(2026, 9, 18))
CLOSE = 100.0


def report(
    day: date,
    *,
    org: str = "中泰证券",
    quarter: str = "2026Q4",
    eps: float = 50.0,
    rating: str | None = "买入",
    hi: float | None = 120.0,
    lo: float | None = 100.0,
) -> ReportRcRow:
    return ReportRcRow("rds", "600519", day, org, quarter, eps, None, rating, hi, lo)


def _tmpl() -> Template:
    return Template(
        name="波段",
        role="你是资深 A 股分析师",
        task="给出波段结构",
        data=(
            Selection(dataset=layout.DAILY, days=3),
            Selection(
                dataset="analyst_rating",
                days=120,
                fields=("rating", "max_price", "min_price", "eps"),
            ),
        ),
        output="先给结论。",
        format="markdown",
        fields=("open", "close"),
        adjust="raw",
        status="ready",
        waiting_on="",
    )


@pytest.fixture
def disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = snapshot_root(tmp_path, monkeypatch)
    store_bars([bar(day, CLOSE) for day in WEEK[:3]], dataset=layout.DAILY, root=base / "data")
    return base / "data"


def _write(root: Path, rows: list[ReportRcRow]) -> None:
    write_table(rows, table="report_rc", root=root)


def _build(as_of: date) -> str:
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    return build(_tmpl(), "600519", as_of, calendar=calendar, token_warn_above=100_000).text


# ── build 级：点时、倒序、截断、空段 ─────────────────────────────────────────


def test_section_lists_visible_reports_newest_first(disk: Path) -> None:
    _write(disk, [report(D1, org="中信证券"), report(D3, org="中泰证券"), report(D2)])
    text = _build(D3)
    assert "### 远期评级与目标价（研报明细）（3 条研报，最新在前）" in text
    assert "远期评级与目标价口径" in text
    body = text.split("### 远期评级与目标价")[1]
    assert body.index(str(D3)) < body.index(str(D1)), "倒序：最新的研报在最前"
    assert "| 2026-09-16 | 中泰证券 | 2026Q4 | 买入 | 100.00–120.00 | 50.00 |" in text


def test_reports_after_as_of_are_invisible_even_on_disk(disk: Path) -> None:
    """点时（03-L4）：D3 之后发布的研报在 as_of=D3 的生成里一个字都不许有。"""
    _write(disk, [report(D1), report(D3, eps=60.0), report(date(2026, 9, 20), eps=80.0)])
    text = _build(D3)
    assert "2026-09-20" not in text and "| 80.00 |" not in text
    assert "### 远期评级与目标价（研报明细）（2 条研报，最新在前）" in text


def test_cap_keeps_the_newest_and_drops_the_oldest(disk: Path) -> None:
    """61 条研报截 60：丢的是最老那条（D1），最新的必须在。"""
    _write(
        disk,
        [report(D1, org="最老标记"), *[report(D2, org=f"券商{i:02d}") for i in range(ANALYST_TOP)]],
    )
    text = _build(D3)
    assert f"（{ANALYST_TOP} 条研报，最新在前）" in text
    assert "最老标记" not in text, "截断必须丢最老的，不是丢最新的"


def test_no_visible_report_gives_the_note_not_an_empty_table(disk: Path) -> None:
    assert disk.is_dir()  # 夹具只负责把数据根钉进 env；本例断言"没有 report_rc 行"那一侧
    text = _build(D1)
    assert NO_DATA_NOTE.splitlines()[0] in text
    assert "| 发布日 | 券商 |" not in text


# ── 渲染格子：— 的三种来源都是「—」 ─────────────────────────────────────────


def test_price_and_rating_cells_keep_missing_as_dash() -> None:
    rows = [
        report(D1, rating=None, hi=None, lo=None),
        report(D2, hi=None, lo=None),
        report(D3, hi=120.0, lo=120.0),  # 单值目标价
    ]
    out = table.render_analyst(rows)
    assert "| 2026-09-14 | 中泰证券 | 2026Q4 | — | — | 50.00 |" in out
    assert "| 2026-09-16 | 中泰证券 | 2026Q4 | 买入 | 120.00 | 50.00 |" in out
    assert "| 0.00" not in out, "缺目标价不许洗成 0"


# ── 解析器：三列可空 + 源没给列不整批拒 ──────────────────────────────────────

_RC_FIELDS = [
    "ts_code",
    "report_date",
    "org_name",
    "quarter",
    "eps",
    "pe",
    "rating",
    "max_price",
    "min_price",
]


def test_parse_picks_rating_and_target_price_with_null_texts_as_none() -> None:
    items = [
        [
            "600519.SH",
            "20260915",
            "中泰证券",
            "2026Q4",
            "70.97",
            "None",
            "增持(上调)",
            "132.5",
            "None",
        ]
    ]
    (row,) = parse_report_rc("rds", _RC_FIELDS, items)
    assert (row.rating, row.max_price, row.min_price) == ("增持(上调)", 132.5, None)


def test_parse_survives_sources_without_the_new_columns() -> None:
    """源没给这三列（老版 fields 清单）：读 None 继续收，不整批拒——可选丰富列的分工。"""
    old_fields = ["ts_code", "report_date", "org_name", "quarter", "eps", "pe"]
    (row,) = parse_report_rc(
        "rds", old_fields, [["600519.SH", "20260915", "中泰证券", "2026Q4", "70.97", "None"]]
    )
    assert (row.rating, row.max_price, row.min_price) == (None, None, None)


# ── schema 演进：老 7 列文件读新 Spec（#66 读侧地基） ────────────────────────


def test_old_schema_file_reads_with_new_columns_as_none(tmp_path: Path) -> None:
    import duckdb

    from zhixing_quant.storage import partition
    from zhixing_quant.storage.tables import ReportRcRow as StoredRow

    path = tmp_path / "year=2026.parquet"
    old_columns = (
        ("source", "VARCHAR"),
        ("symbol", "VARCHAR"),
        ("report_date", "DATE"),
        ("org_name", "VARCHAR"),
        ("quarter", "VARCHAR"),
        ("eps", "DOUBLE"),
        ("pe", "DOUBLE"),
    )
    con = duckdb.connect()
    partition.rewrite(
        con, path, old_columns, [("rds", "600519", D1, "中泰证券", "2026Q4", 70.97, None)]
    )
    rows = partition.read(
        con,
        path,
        (
            "source",
            "symbol",
            "report_date",
            "org_name",
            "quarter",
            "eps",
            "pe",
            "rating",
            "max_price",
            "min_price",
        ),
        StoredRow,
    )
    assert [(r.eps, r.rating, r.max_price, r.min_price) for r in rows] == [
        (70.97, None, None, None)
    ]
