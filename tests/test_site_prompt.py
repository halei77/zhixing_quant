"""`site/prompt.py`：模板里的"要 N 个交易日"接到盘上真实深度（ADR-0012）。

这一段判的全是"说没说实话"。标题里那个天数是不是实际取到的天数、表里的价是不是模板声明的那一口、
超阈值的警告是不是留在人这一侧而没混进给模型的文本——三样都是提示词站点唯一能被机器核实的品质
（06 §八-1/3/5）。取数本身（哪一天有没有行）是存储层的事，这里只判接线。

日历一律手写注入（决定 2）：这一层不许自己去抓日历，测试里没有网络这条路是结构性的，不是巧合。
"""

from datetime import date
from pathlib import Path

import pytest

from tests.fakes import bar, minutes, snapshot_root
from zhixing_quant.domain.bar import Bar
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.site import tokens
from zhixing_quant.site.prompt import (
    CalendarTooShort,
    TemplateNotReady,
    UnnamedDataset,
    build,
    quote_of,
    title_of,
    window,
)
from zhixing_quant.site.table import IncompleteComponent
from zhixing_quant.site.templates import Adjust, Format, Selection, Status, Template
from zhixing_quant.storage import layout
from zhixing_quant.storage.query import read_bars
from zhixing_quant.storage.write import store_bars

D1 = date(2024, 1, 2)
D2 = date(2024, 1, 3)
D3 = date(2024, 1, 4)
#: 周日：不是交易日，所以 `window` 的 `end` 必须退到 D3（周五）。
SUNDAY = date(2024, 1, 7)
DAYS = (D1, D2, D3)
CALENDAR = TradingCalendar([*DAYS, date(2024, 1, 8), date(2024, 1, 9)])

#: 与 `tests.fakes.bar` 默认因子一致：原始价 10.0 → 后复权 80.0，一眼看得出走的哪条路。
FACTOR = 8.0
#: 换手率的分母（流通股本，股）：与 `test_site_table` 同一量级，够让每一格的值互不相同。
SHARES = 246900.0


def tmpl(
    *,
    data: tuple[Selection, ...] = (Selection(dataset=layout.DAILY, days=3),),
    fields: tuple[str, ...] = ("open", "high", "low", "close"),
    adjust: Adjust = "backward",
    format: Format = "markdown",
    status: Status = "ready",
    waiting_on: str = "",
) -> Template:
    """一条合规模的模板：默认短期投资那套选择，测试只改自己要判的那一格。"""
    return Template(
        name="短期投资",
        role="你是资深 A 股分析师",
        task="判断未来 1–4 周的机会与风险",
        data=data,
        output="先给结论，再给依据。",
        format=format,
        fields=fields,
        adjust=adjust,
        status=status,
        waiting_on=waiting_on,
    )


def build_it(template: Template, *, float_shares: float | None = None) -> str:
    """默认那只票、那三天、那个日历，只改模板与换手率的分母。"""
    return build(
        template,
        "600519",
        D3,
        calendar=CALENDAR,
        token_warn_above=100_000,
        float_shares=float_shares,
    ).text


@pytest.fixture(autouse=True)
def data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """三天日线（原始价 10/11/12），落在只属于本测试的数据根里。"""
    base = snapshot_root(tmp_path, monkeypatch)
    store_bars(
        [bar(D1, 10.0), bar(D2, 11.0), bar(D3, 12.0)],
        dataset=layout.DAILY,
        root=base / "data",
    )
    return base / "data"


def test_the_title_counts_the_days_it_actually_got() -> None:
    """要 3 天、盘上 3 天：标题就只写 3 天，不加任何"盘上"字样（那是缺口才说的话）。"""
    assert "### 日K（3 个交易日）" in build_it(tmpl())


def test_a_shallower_pool_says_so_in_the_title() -> None:
    """决定 3 的核心：模板要 120 天而盘上只有 3 天，标题必须自己承认，一张表都不许多装。

    反过来的写法——照抄模板里的 120——是这一层能犯的最重的错：模型没有任何办法从那张 3 行的
    表里发现自己被骗，而它接下来会按"最近半年"来回答。
    """
    text = build_it(tmpl(data=(Selection(dataset=layout.DAILY, days=120),)))
    assert "### 日K（盘上 3 个交易日，模板要 120）" in text
    assert "（120 个交易日）" not in text


def test_the_table_carries_the_caliber_the_template_declared() -> None:
    """口径接线：`backward` 的价是 80 而不是 10，自证口径那句跟着进表（ADR-0011 决定 3）。"""
    text = build_it(tmpl())
    assert "80.00" in text
    assert "| 10.00 |" not in text
    assert "价格口径：后复权" in text


def test_a_pending_template_is_refused() -> None:
    """pending 不生成：少一列的表看起来和满列的一样完整，而模型会替你把那一列编出来（决定 5）。"""
    pending = tmpl(status="pending", waiting_on="流通股本没有出处（坑 #30）")
    with pytest.raises(TemplateNotReady) as caught:
        build_it(pending)
    message = str(caught.value)
    assert "短期投资" in message
    assert "流通股本没有出处" in message


def test_turnover_still_needs_its_denominator() -> None:
    """K线在盘上、分母不在：换手率那一段照样拒生成，不在这里兜 0（ADR-0011 决定 5）。

    后半条是接线的反证：给了分母就出得来（1000 股 × 100 ÷ 246900 = 0.41%），说明刚才拒的是
    缺分母这件事，不是分钟线、不是模板、也不是盘上没数。
    """
    request = tmpl(fields=("close", "turnover"))
    with pytest.raises(IncompleteComponent) as caught:
        build_it(request)
    assert "流通股本" in str(caught.value)
    assert "0.41" in build_it(request, float_shares=246900.0)


def test_a_request_nothing_can_answer_is_refused_not_emptied() -> None:
    """分钟线一段都没落盘：拒生成，而不是交出一份"指令齐全、数字全靠编"的提示词。"""
    minute = tmpl(data=(Selection(dataset=layout.MINUTE_5, days=2),))
    with pytest.raises(IncompleteComponent):
        build_it(minute)


def test_sections_come_in_the_order_the_template_declares(data: Path) -> None:
    """日K在前、60 分在后是模板里写的顺序，接上盘之后不许倒过来。"""
    store_bars(
        minutes(D1, [10.0, 10.5]) + minutes(D2, [11.0, 11.5]) + minutes(D3, [12.0, 12.5]),
        dataset=layout.MINUTE_60,
        root=data,
    )
    text = build_it(
        tmpl(data=(Selection(dataset=layout.DAILY, days=3), Selection(layout.MINUTE_60, 3)))
    )
    assert text.index("### 日K（3 个交易日）") < text.index("### 60 分K（3 个交易日）")
    assert text.count("【数据纪律】") == 1


def test_the_token_warning_stays_on_the_human_side_of_the_wall() -> None:
    """警告是 06 §八-5 给**人**看的东西：写进提示词，模型会把它当成内容读（决定 6）。

    阈值取 1 而不是取"正好那份文本的长度"：这条判的是"超了要说"，说给了谁才是重点。
    """
    result = build(tmpl(), "600519", D3, calendar=CALENDAR, token_warn_above=1)
    assert result.warn is not None
    assert "超过阈值" in result.warn
    assert "超过阈值" not in result.text
    assert result.tokens == tokens.estimate(result.text)


def test_exactly_at_the_threshold_there_is_no_warning() -> None:
    """等于阈值不警告（`tokens.over` 是严格大于）：接在 `build` 上才算这条口径走到了终点。"""
    size = tokens.estimate(build_it(tmpl()))
    assert build(tmpl(), "600519", D3, calendar=CALENDAR, token_warn_above=size).warn is None
    assert build(tmpl(), "600519", D3, calendar=CALENDAR, token_warn_above=size - 1).warn


def test_the_window_ends_on_the_last_trading_day_not_on_the_calendar_day() -> None:
    """周日生成提示词，末行应该是周五：`days` 数的是交易日，不是日历日（决定 2）。"""
    assert window(CALENDAR, SUNDAY, 2) == (D2, D3)
    assert window(CALENDAR, D3, 3) == (D1, D3)
    assert window(CALENDAR, D3, 30) == (D1, D3)  # 日历比要的短：到最早那天，差额由标题说


def test_a_calendar_that_never_reached_the_day_refuses_instead_of_guessing() -> None:
    """as_of 早于日历第一天：区间无从定起，这里不拿"最早那天"当 as_of 凑一份出来。"""
    with pytest.raises(CalendarTooShort) as caught:
        window(CALENDAR, date(2023, 1, 1), 3)
    assert "2023-01-01" in str(caught.value)


def test_a_dataset_nobody_named_gets_no_chinese_label() -> None:
    """决定 4：词表在 `layout`，这一层只从名字派生标题；派生不出来就响，不编一个中文名。"""
    assert title_of(layout.DAILY) == "日K"
    assert title_of(layout.MINUTE_5) == "5 分K"
    assert title_of(layout.MINUTE_60) == "60 分K"
    with pytest.raises(UnnamedDataset):
        title_of(layout.QUARANTINE)


def _table(text: str, label: str) -> list[list[str]]:
    """把某一段的表格从提示词里读回来（第一行是表头）。

    解析器在这里**故意另写一份**，不复用 `test_site_table` 里的那份：两边共用一个解析器时，
    同一个偏移（比如都少切一个 `|`）会让比对一起错、一起绿——那正是 06 §八-3 要防的空转。
    """
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"### {label}（"))
    rows: list[list[str]] = []
    for line in lines[start + 1 :]:
        if line.startswith(("### ", "【输出要求】")):
            break
        if line.startswith("|") and "---" not in line:
            rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def _stamp(bar: Bar) -> str:
    """时间格：日线只有日期，分钟线带时刻（规格在测试这边重写一遍，不共用渲染那侧的代码）。"""
    return bar.trade_date.isoformat() if bar.ts is None else bar.ts.strftime("%Y-%m-%d %H:%M")


def test_every_cell_of_the_prompt_is_the_clean_zone_row_it_claims(
    data: Path,
) -> None:
    """06 §八-3 的自动化对照：落盘 → 组装 → 逐格读回，与干净区的查询结果一字不差。

    判的是**接线**（价从哪来、行有没有错位、列有没有串），所以格式化那套写法照测试侧自己的
    规格来（价格两位、量额取整、换手率 = 成交量 ÷ 流通股本 ×100）——格式口径本身归
    `test_site_table` 判。日线与分钟线两段都比，因为两段各自有一次接错的机会。
    """
    store_bars(
        minutes(D1, [10.0, 10.5]) + minutes(D2, [11.0, 11.5]) + minutes(D3, [12.0, 12.5]),
        dataset=layout.MINUTE_5,
        root=data,
    )
    text = build_it(
        tmpl(
            data=(
                Selection(dataset=layout.DAILY, days=3),
                Selection(dataset=layout.MINUTE_5, days=3),
            ),
            fields=("open", "high", "low", "close", "volume", "amount", "turnover"),
        ),
        float_shares=SHARES,
    )
    start, end = window(CALENDAR, D3, 3)
    for label, dataset in (("日K", layout.DAILY), ("5 分K", layout.MINUTE_5)):
        bars = read_bars("600519", start, end, adjust="backward", dataset=dataset)
        rows = _table(text, label)
        assert len(rows) == len(bars) + 1, f"{label}：行数对不上就是行本身错位"
        for row, one in zip(rows[1:], bars, strict=True):
            assert row == [
                _stamp(one),
                f"{one.open:.2f}",
                f"{one.high:.2f}",
                f"{one.low:.2f}",
                f"{one.close:.2f}",
                f"{one.volume:.0f}",
                f"{one.amount:.0f}",
                f"{one.volume * 100.0 / SHARES:.2f}",
            ], f"{label} {row[0]} 那格与干净区不符"


def test_the_quote_is_computed_and_formatted_on_the_server() -> None:
    """涨跌幅是口径、金额是格式化，两样都得在服务端做完——壳不许自己再推一遍（docs/11 §六-2）。"""

    def _bar(day: str, close: float, volume: float | None) -> dict[str, object]:
        return {
            "date": day,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": volume,
        }

    bars = [_bar("2026-09-22", 10.0, 1000.0), _bar("2026-09-23", 11.0, 1234567.0)]
    quote = quote_of(bars)
    assert quote is not None
    assert quote["close"] == "11.00"
    assert quote["change_pct"] == "+10.00%"
    assert quote["up"] is True
    assert quote["volume"] == "1,234,567"
    assert quote_of([]) is None


def test_a_lone_bar_gets_a_dash_for_change_not_a_made_up_zero() -> None:
    """只有一根时涨跌幅给 —："没昨收"和"没涨跌"是两件事，编一个 0 出来就是撒谎。"""
    lone: dict[str, object] = {
        "date": "2026-09-23",
        "open": 5.0,
        "high": 5.0,
        "low": 5.0,
        "close": 5.0,
        "volume": None,
    }
    quote = quote_of([lone])
    assert quote is not None
    assert quote["close"] == "5.00"
    assert quote["change_pct"] == "—"
    assert quote["volume"] == "—"
