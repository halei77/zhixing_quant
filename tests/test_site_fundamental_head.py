"""最新基本面固定头 + 已挂参考表：ADR-0022 决定 1、ADR-0020、06 §十-4 的现场判定。

固定头的三态（有数 / 全空 / 部分空）、null →「—」禁 0 填充、口径行模板删不掉、自定义组合
（直构 `Template`）也强制带上——以及三张挂出去的参考表（daily_basic / stk_limit / index_daily）
出成型提示词、空了出交代、逐格与干净区对照（06 §八-3 手法）。

对照的解析器与格式规格都在测试侧**另写一份**：与渲染共用一份时，同一个偏移会让比对一起错、
一起绿——那正是这条验收要防的空转。
"""

from datetime import date
from pathlib import Path

import pytest

from tests.fakes import bar, snapshot_root
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.site import templates
from zhixing_quant.site.prompt import BENCHMARK, build, title_of, window
from zhixing_quant.site.templates import Selection, Template
from zhixing_quant.sources.akshare.master import read_master
from zhixing_quant.sources.relay.tables import DailyBasicRow, IndexDailyRow, StkLimitRow
from zhixing_quant.storage import layout
from zhixing_quant.storage.query import read_bars
from zhixing_quant.storage.tables import read_table, write_table
from zhixing_quant.storage.write import store_bars

D1 = date(2024, 1, 2)
D2 = date(2024, 1, 3)
D3 = date(2024, 1, 4)
DAYS = (D1, D2, D3)
CALENDAR = TradingCalendar([*DAYS, date(2024, 1, 8), date(2024, 1, 9)])


def tmpl(
    *,
    data: tuple[Selection, ...] = (Selection(dataset=layout.DAILY, days=3),),
    fields: tuple[str, ...] = ("open", "high", "low", "close"),
) -> Template:
    """一条合规模板：默认只声明日K，固定头应当由机制补上而不是靠这里写。"""
    return Template(
        name="短期投资",
        role="你是资深 A 股分析师",
        task="判断未来 1–4 周的机会与风险",
        data=data,
        output="先给结论，再给依据。",
        format="markdown",
        fields=fields,
        adjust="backward",
        status="ready",
        waiting_on="",
    )


def build_it(template: Template, *, code: str = "600519") -> str:
    return build(template, code, D3, calendar=CALENDAR, token_warn_above=100_000).text


def basic(
    day: date,
    *,
    symbol: str = "600519",
    close: float = 12.0,
    turnover_rate: float | None = 0.25,
    pe_ttm: float | None = 18.5,
    pb: float | None = 6.2,
    ps_ttm: float | None = 9.15,
    dv_ttm: float | None = 4.13,
    total_mv: float | None = 156_735_231.0,
    circ_mv: float | None = 156_735_231.0,
) -> DailyBasicRow:
    """一行 daily_basic：没点名的列给 None（渲染侧该出「—」的地方就出「—」）。"""
    return DailyBasicRow(
        "rds",
        symbol,
        day,
        close,
        turnover_rate,
        None,  # turnover_rate_f
        None,  # volume_ratio
        None,  # pe（静态，不在固定头勾选里）
        pe_ttm,
        pb,
        None,  # ps
        ps_ttm,
        None,  # dv_ratio
        dv_ttm,
        None,  # total_share
        None,  # float_share
        None,  # free_share
        total_mv,
        circ_mv,
    )


def index_row(day: date, *, close: float) -> IndexDailyRow:
    return IndexDailyRow(
        "rds",
        BENCHMARK,
        day,
        close - 5.0,
        close + 5.0,
        close - 10.0,
        close,
        1_234_567.0,
        987_654_321.0,
    )


def _section(text: str, title: str) -> str:
    """标题以 `### {title}` 开头那一节的正文（不含标题行，到下一节或输出要求为止）。"""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"### {title}"))
    body: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith(("### ", "【输出要求】")):
            break
        body.append(line)
    return "\n".join(body)


def _grid(body: str) -> list[list[str]]:
    rows = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in body.splitlines()
        if line.startswith("|") and "---" not in line
    ]
    assert rows, f"这一节一张表都没有：{body!r}"
    return rows


def _kv(body: str) -> dict[str, str]:
    rows = _grid(body)
    assert rows[0] == ["项目", "数值"], f"固定头是两列表：{rows[0]}"
    return {row[0]: row[1] for row in rows[1:]}


def _cell(name: str, value: object) -> object:
    """测试侧自己的格式规格（06 §八-3）：null →「—」，市值与量额取整千分位，其余两位。"""
    if value is None:
        return "—"
    if name in ("total_mv", "circ_mv", "volume", "amount"):
        return f"{value:,.0f}"
    return f"{value:.2f}"


@pytest.fixture(autouse=True)
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """三天日线（原始价 10/11/12，因子 8）+ 主数据快照，落在只属于本测试的数据根里。"""
    base = snapshot_root(tmp_path, monkeypatch)
    store_bars(
        [bar(D1, 10.0), bar(D2, 11.0), bar(D3, 12.0)],
        dataset=layout.DAILY,
        root=base / "data",
    )
    return base / "data"


# ── 固定头三态 ────────────────────────────────────────────────────────────────


def test_the_head_leads_the_prompt_with_the_latest_basics_and_its_caliber(data: Path) -> None:
    """有数态：头排最前，口径行由代码给（模板删不掉），收盘是日线末行的**不复权**价。"""
    write_table([basic(D3)], table="daily_basic", root=data)
    text = build_it(tmpl())
    assert text.index("### 最新基本面（1 个交易日）") < text.index("### 日K（")
    head = _section(text, "最新基本面")
    assert head.startswith("> 估值口径："), "口径行是表的一部分，模板没有 knob 能删它"
    cells = _kv(head)
    assert cells["名称"] == "贵州茅台" and cells["代码"] == "600519"
    assert cells["收盘日期"] == "2024-01-04"
    assert cells["收盘"] == "12.00", "头里是不复权收盘（ADR-0016 决定 5 两行口径各行其表）"
    assert "| 收盘 | 96.00 |" not in head, "后复权价不许混进固定头"
    assert cells["PE(TTM)"] == "18.50" and cells["PB"] == "6.20"
    assert cells["总市值(万元)"] == "156,735,231"
    assert cells["股息率(%)"] == "4.13" and cells["换手率(%)"] == "0.25"
    assert text.count("【数据纪律】") == 1, "头是数据段，反幻觉头部照旧只有 compose 那一份"


def test_a_null_metric_is_a_dash_never_a_zero(data: Path) -> None:
    """部分空之一：源给 null 的格子渲染「—」，禁 0 填充（ADR-0016 决定 3 的渲染侧）。"""
    write_table([basic(D3, pe_ttm=None, dv_ttm=None, circ_mv=None)], table="daily_basic", root=data)
    head = _section(build_it(tmpl()), "最新基本面")
    cells = _kv(head)
    assert cells["PE(TTM)"] == "—"
    assert cells["股息率(%)"] == "—"
    assert cells["流通市值(万元)"] == "—"
    assert "0.00" not in head, "0 填充会把「没这数」洗成「这数为 0」，模型会拿它做算术"


def test_the_identity_rows_go_dash_when_there_is_no_listing_or_no_bar(data: Path) -> None:
    """部分空之二：日线没行（或主数据查无此码）时身份格是「—」，估值格照常。"""
    write_table([basic(D3, symbol="600000")], table="daily_basic", root=data)
    head_only = tmpl(
        data=(
            Selection(
                dataset=templates.FUNDAMENTAL_HEAD,
                days=1,
                fields=templates.FUNDAMENTAL_HEAD_FIELDS,
            ),
        )
    )
    cells = _kv(_section(build_it(head_only, code="600000"), "最新基本面"))
    assert cells["名称"] == "—" and cells["收盘日期"] == "—" and cells["收盘"] == "—"
    assert cells["代码"] == "600000"
    assert cells["PE(TTM)"] == "18.50", "缺的是日线与名单，不是估值——各格如实分开"


def test_a_day_without_daily_basic_is_a_note_not_an_empty_head() -> None:
    """全空态：daily_basic 那天没行 → ADR-0020 的交代，不给只有表头的空表。"""
    text = build_it(tmpl())
    assert "### 最新基本面（盘上 0 个交易日，模板要 1）" in text
    head = _section(text, "最新基本面")
    assert "本节无数据" in head and "数据未提供" in head
    assert "|" not in head, "空组件任何形式都不给表骨架（ADR-0020 决定 1）"
    assert "### 日K（3 个交易日）" in text, "头空不拖垮整条：日K那节照常给"


def test_a_custom_style_template_is_forced_to_carry_the_head() -> None:
    """自定义组合（直构 Template，不经 YAML 装载）也强制带——「含自定义组合」的落点。"""
    custom = tmpl(data=(Selection(dataset=layout.DAILY, days=3),))
    text = build_it(custom)
    assert "### 最新基本面" in _head_is_built(text)
    assert "### 日K（3 个交易日）" in text


def _head_is_built(text: str) -> str:
    """断言头已进正文并原样返回文本：让失败信息落在缺的那一节上。"""
    assert "### 最新基本面" in text, "固定头没进提示词：自定义组合绕过了装载，兜底失效"
    return text


# ── 06 §八-3：落盘 → 组装 → 逐格读回 ─────────────────────────────────────────


def test_every_head_cell_is_the_clean_zone_value_it_claims(data: Path) -> None:
    """固定头的逐格对照：名称查名单、收盘查日线（不复权）、估值查 daily_basic，一字不差。"""
    write_table([basic(D1), basic(D2), basic(D3)], table="daily_basic", root=data)
    cells = _kv(_section(build_it(tmpl()), "最新基本面"))
    name = next(item.name for item in read_master().listings if item.code == "600519")
    start, end = window(CALENDAR, D3, 1)
    bars = read_bars("600519", start, end, adjust="raw", dataset=layout.DAILY, root=data)
    rows = read_table("daily_basic", "600519", start, end, root=data)
    assert bars and rows
    last_bar, last_row = bars[-1], rows[-1]
    assert cells["名称"] == name
    assert cells["代码"] == "600519"
    assert cells["收盘日期"] == last_bar.trade_date.isoformat()
    assert cells["收盘"] == _cell("close", last_bar.close)
    for field in templates.FUNDAMENTAL_HEAD_FIELDS:
        if field == "close":
            continue
        label = {
            "total_mv": "总市值(万元)",
            "circ_mv": "流通市值(万元)",
            "pe_ttm": "PE(TTM)",
            "pb": "PB",
            "ps_ttm": "PS(TTM)",
            "dv_ttm": "股息率(%)",
            "turnover_rate": "换手率(%)",
        }[field]
        assert cells[label] == _cell(field, getattr(last_row, field)), field


# ── 盘上已有、刚挂出去的参考表 ───────────────────────────────────────────────


def test_the_hung_reference_tables_produce_formed_prompts_cell_by_cell(data: Path) -> None:
    """06 §十-4：估值/涨跌停/大盘三段成型，逐格与干净区对照 100% 一致。"""
    write_table([basic(D1), basic(D2), basic(D3)], table="daily_basic", root=data)
    write_table(
        [
            StkLimitRow("rds", "600519", D1, 11.0, 9.0),
            StkLimitRow("rds", "600519", D2, 12.1, 9.9),
            StkLimitRow("rds", "600519", D3, 13.31, 10.89),
        ],
        table="stk_limit",
        root=data,
    )
    write_table(
        [index_row(D1, close=3005.5), index_row(D2, close=3010.25), index_row(D3, close=3020.75)],
        table="index_daily",
        root=data,
    )
    template = tmpl(
        data=(
            Selection(dataset=layout.DAILY, days=3),
            Selection(
                dataset="daily_basic",
                days=3,
                fields=("close", "pe_ttm", "pb", "turnover_rate"),
            ),
            Selection(dataset="stk_limit", days=3, fields=("up_limit", "down_limit")),
            Selection(
                dataset="index_daily",
                days=3,
                fields=("open", "high", "low", "close", "volume", "amount"),
            ),
        )
    )
    text = build_it(template)
    start, end = window(CALENDAR, D3, 3)

    valuation = read_table("daily_basic", "600519", start, end, root=data)
    grid = _grid(_section(text, "估值（每日指标）"))
    assert grid[0] == ["时间", "收盘", "PE(TTM)", "PB", "换手率(%)"]
    assert len(grid) == len(valuation) + 1, "行数对不上就是行本身错位"
    for parsed, row in zip(grid[1:], valuation, strict=True):
        assert parsed == [
            row.trade_date.isoformat(),
            _cell("close", row.close),
            _cell("pe_ttm", row.pe_ttm),
            _cell("pb", row.pb),
            _cell("turnover_rate", row.turnover_rate),
        ]

    limits = read_table("stk_limit", "600519", start, end, root=data)
    grid = _grid(_section(text, "涨跌停价"))
    assert grid[0] == ["时间", "涨停价(元)", "跌停价(元)"]
    assert len(grid) == len(limits) + 1
    for parsed, row in zip(grid[1:], limits, strict=True):
        assert parsed == [
            row.trade_date.isoformat(),
            _cell("up_limit", row.up_limit),
            _cell("down_limit", row.down_limit),
        ]

    # 大盘段读的是 BENCHMARK 不是个股：600519 名下没有 index_daily 行，出得来就证明换了 symbol。
    assert f"### 大盘（上证指数 {BENCHMARK}）（3 个交易日）" in text
    index_rows = read_table("index_daily", BENCHMARK, start, end, root=data)
    grid = _grid(_section(text, f"大盘（上证指数 {BENCHMARK}）"))
    assert grid[0] == ["时间", "开盘", "最高", "最低", "收盘", "成交量(手)", "成交额(千元)"]
    assert len(grid) == len(index_rows) + 1
    for parsed, row in zip(grid[1:], index_rows, strict=True):
        assert parsed == [
            row.trade_date.isoformat(),
            _cell("open", row.open),
            _cell("high", row.high),
            _cell("low", row.low),
            _cell("close", row.close),
            _cell("volume", row.volume),
            _cell("amount", row.amount),
        ]


def test_an_empty_hung_reference_table_yields_the_note_not_a_skeleton() -> None:
    """新挂的表空了同样出交代（ADR-0020 与K线、估值同一条线）。"""
    template = tmpl(
        data=(
            Selection(dataset=layout.DAILY, days=3),
            Selection(dataset="stk_limit", days=3, fields=("up_limit", "down_limit")),
        )
    )
    text = build_it(template)
    assert "### 涨跌停价（盘上 0 个交易日，模板要 3）" in text
    body = _section(text, "涨跌停价")
    assert "本节无数据" in body
    assert "|" not in body, "空参考表不许带头表骨架"
    assert "### 日K（3 个交易日）" in text


def test_the_new_tables_have_chinese_titles_that_name_what_they_are() -> None:
    """小标题不编：三张新表各自有中文名，大盘那条连指数代码一起印。"""
    assert title_of("fundamental_head") == "最新基本面"
    assert title_of("stk_limit") == "涨跌停价"
    assert title_of("index_daily") == f"大盘（上证指数 {BENCHMARK}）"
