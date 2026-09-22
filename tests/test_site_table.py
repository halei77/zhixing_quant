"""K线表：06 §六 那几条格式口径全部落成断言（ADR-0011 决定 3/5）。

这张表是站点交给大模型的**唯一数字出处**，所以它自己得先做到两件"错了不会响"的事：口径写在表
里（不靠模板作者记得写）、正序不靠上游（喂一份乱序数组进来也出正序）。两条都是漏了照样能生成、
生成出来却不可信的东西，所以逐条判到格子上，而不是判"渲染没抛异常"。
"""

from datetime import date, datetime

import pytest

from tests.fakes import daily_span, minute_span
from zhixing_quant.domain.bar import Bar
from zhixing_quant.site import table
from zhixing_quant.site.table import IncompleteComponent, render
from zhixing_quant.site.templates import FIELDS, Adjust, Format

DAY1 = date(2026, 9, 17)
DAY2 = date(2026, 9, 18)
AT_EARLY = datetime(2026, 9, 18, 10, 30)
AT_LATE = datetime(2026, 9, 18, 14, 0)
#: 一根K线能要的全部字段（不含 `turnover`：它要分母，单独一组测试伺候）。
PRICES = ("open", "high", "low", "close")
QUANTITY = ("volume", "amount")
ALL_FIELDS = (*PRICES, *QUANTITY)
CALIBERS: tuple[Adjust, ...] = ("raw", "backward", "forward")


def _daily(
    day: date,
    *,
    open_: float = 10.0,
    high: float = 10.4,
    low: float = 9.8,
    close: float = 10.2,
    volume: float = 12345.0,
    amount: float = 125919.0,
    symbol: str = "600519",
) -> Bar:
    """一根四个价各不相同的日线：价相等的话"列顺序 vs 格子顺序"这类判据看不出区别。"""
    return daily_span(
        day,
        open_=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=amount,
        symbol=symbol,
    )


def _minute(when: datetime) -> Bar:
    return minute_span(when, open_=10.0, high=10.4, low=9.8, close=10.2)


def _grid(text: str) -> list[list[str]]:
    """表里的行拆成格子：第一行是表头，其余是数据，分隔行不算。"""
    lines = [x for x in text.splitlines() if x.startswith("|") and "---" not in x]
    return [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]


def _markdown(bars: list[Bar], fields: tuple[str, ...] = ALL_FIELDS) -> str:
    return render(bars, format="markdown", fields=fields, adjust="backward")


@pytest.mark.parametrize("format", ["markdown", "csv"])
def test_the_caliber_line_is_the_first_thing_the_model_sees(format: Format) -> None:
    """口径行必须在**任何数字之前**：先读到 8.88 再读到"这是后复权"，等于没有那一句。"""
    text = render([_daily(DAY1)], format=format, fields=("close",), adjust="backward")
    assert text.splitlines()[0].endswith(table.ADJUST_LABELS["backward"])


def test_the_three_calibers_are_three_different_sentences() -> None:
    """三口径不能共用一句说明：把原始价说成"趋势可读"就是替数据撒了一半的谎。"""
    lines = {
        render([_daily(DAY1)], format="markdown", fields=("close",), adjust=adjust).splitlines()[0]
        for adjust in CALIBERS
    }
    assert len(lines) == 3
    assert all("价格口径" in line for line in lines)


def test_the_body_is_untouched_by_the_caliber() -> None:
    """换口径只换那一行说明，格子还是调用方给的那些数：`render` 不复权，复权是查询层的事。

    这条看着像废话，但它正是决定 1 那道边界的落点：纯层一旦在渲染时顺手乘个因子，
    "表里的数与干净区抽样比对 100% 一致"（06 §八-3）就再也对不上了。
    """
    grids = [
        _grid(render([_daily(DAY1)], format="markdown", fields=ALL_FIELDS, adjust=adjust))
        for adjust in CALIBERS
    ]
    assert grids[0] == grids[1] == grids[2]


def test_the_time_column_is_there_even_when_nobody_asked() -> None:
    """`time` 选不掉（06 §四 的可选字段里没有它）：一张没有时间轴的K线表没法读。"""
    assert _grid(_markdown([_daily(DAY1)], fields=()))[0] == ["时间"]


def test_the_columns_follow_the_order_the_template_declared() -> None:
    text = _markdown([_daily(DAY1)], fields=("close", "open"))
    assert _grid(text)[0] == ["时间", "收盘", "开盘"]
    assert _grid(text)[1] == ["2026-09-17", "10.20", "10.00"]


def test_prices_keep_two_decimals_and_quantity_keeps_none() -> None:
    """06 §六：价格 2 位小数、量额取整。量额写成 `12345.00` 只会白涨一截 token。"""
    assert _grid(_markdown([_daily(DAY1)]))[1] == [
        "2026-09-17",
        "10.00",
        "10.40",
        "9.80",
        "10.20",
        "12345",
        "125919",
    ]


def test_a_daily_row_shows_the_date_and_a_minute_row_the_time() -> None:
    """同一个函数出两种粒度，靠的是 `ts` 缺则退化（与 `domain.bar.stamp_of` 同一手法）。"""
    assert _grid(_markdown([_daily(DAY1)], fields=("close",)))[1][0] == "2026-09-17"
    assert _grid(_markdown([_minute(AT_LATE)], fields=("close",)))[1][0] == ("2026-09-18 14:00")


def test_a_shuffled_input_still_comes_out_in_time_order() -> None:
    """06 §六 的"时间正序"是**这张表**的性质，不是"上游恰好给了有序数组"的巧合。"""
    bars = [_daily(DAY2), _daily(DAY1), _daily(DAY1, symbol="600519")]
    stamps = [row[0] for row in _grid(_markdown(bars))[1:]]
    assert stamps == sorted(stamps)
    assert stamps == ["2026-09-17", "2026-09-17", "2026-09-18"]


def test_minute_rows_sort_by_the_time_not_the_date() -> None:
    """分钟线同一天：按日排的话两根会并列，谁在前就成了输入顺序的函数。"""
    bars = [_minute(AT_LATE), _minute(AT_EARLY)]
    assert [row[0] for row in _grid(_markdown(bars))[1:]] == [
        "2026-09-18 10:30",
        "2026-09-18 14:00",
    ]


def test_the_markdown_table_has_one_separator_cell_per_column() -> None:
    """分隔行的格子数必须与表头一致：少一个，整张表在渲染器眼里就塌成一列。"""
    lines = _markdown([_daily(DAY1)]).splitlines()
    columns = len(lines[2].strip("|").split("|"))
    assert lines[3] == "|" + "---|" * columns
    assert columns == 1 + len(ALL_FIELDS)


def test_the_csv_mode_drops_everything_but_the_numbers() -> None:
    """紧凑模式（06 §六 的"token 紧张时启用"）：没有竖线、没有分隔行，一根一行。"""
    lines = render(
        [_daily(DAY1), _daily(DAY2)], format="csv", fields=ALL_FIELDS, adjust="backward"
    ).splitlines()
    assert lines[0].startswith("# 价格口径：")
    assert not any("|" in line for line in lines)
    assert len(lines) == 2 + 2


def test_turnover_divides_the_volume_by_the_float() -> None:
    """换手率 = 成交量 ÷ 流通股本。12345 股 / 246900 股 = 5%。"""
    text = render(
        [_daily(DAY1)],
        format="markdown",
        fields=("close", "turnover"),
        adjust="backward",
        float_shares=246900.0,
    )
    row = _grid(text)[1]
    assert row[1:] == ["10.20", "5.00"]
    assert "%" not in row[-1], "百分号在表头里，格子里再来一次就是两处事实"


@pytest.mark.parametrize("shares", [None, 0.0, -1.0], ids=["没给", "零", "负"])
def test_turnover_without_its_denominator_is_refused(shares: float | None) -> None:
    """缺分母就拒生成（ADR-0011 决定 5）：静默少一列的表"看起来完整"，而模型不知道自己缺了什么。"""
    with pytest.raises(IncompleteComponent) as caught:
        render(
            [_daily(DAY1)],
            format="markdown",
            fields=("close", "turnover"),
            adjust="backward",
            float_shares=shares,
        )
    assert "流通股本" in str(caught.value)


def test_a_table_without_a_single_bar_is_refused() -> None:
    """空表不进提示词：那是"今天取不到数"，不是"这只票没涨跌"——两者该说的话完全不同。"""
    with pytest.raises(IncompleteComponent) as caught:
        render([], format="markdown", fields=("close",), adjust="backward")
    assert "一张都没有" in str(caught.value)


def test_two_symbols_in_one_table_are_refused() -> None:
    """一期不做多股票对比（06 §九）。混进一张表，表里就没有"这只票的走势"这回事了。"""
    with pytest.raises(IncompleteComponent) as caught:
        _markdown([_daily(DAY1), _daily(DAY1, symbol="000001")])
    message = str(caught.value)
    assert "2 只票" in message and "600519" in message and "000001" in message


def test_the_selectable_fields_and_the_header_table_cannot_drift() -> None:
    """模板能勾选的字段，与这张表能印表头的字段，必须是同一批。

    漂开的方式很安静：往 `FIELDS` 加一个 `eps`，装载能过，点到那条模板时 `HEADERS[...]`
    才 KeyError——而那是用户面前。反过来漂则是有一列印不出来。
    """
    assert set(FIELDS) == set(table.HEADERS) - {"time"}
    assert all(table.HEADERS[name] for name in FIELDS)


def test_render_valuation_nulls_units_and_csv() -> None:
    """ADR-0016：null 渲染成 '—'（禁 0 填充的渲染侧）；市值万元取整不用科学计数法；
    csv 模式的口径行走注释。"""
    from datetime import date

    from zhixing_quant.site.table import render_valuation
    from zhixing_quant.sources.relay.tables import DailyBasicRow

    rows = [
        DailyBasicRow(
            "rds",
            "600519",
            date(2026, 9, 18),
            1257.12,
            0.2,
            None,
            1.14,
            19.2469,
            None,
            6.2381,
            9.2115,
            4.1492,
            4.1262,
            4.1,
            125008.16,
            125008.16,
            56879.87,
            156735231.008,
            156735231.0,
        ),
        # 19 列：source, symbol, trade_date, close, turnover_rate, turnover_rate_f,
        # volume_ratio, pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm, total_share,
        # float_share, free_share, total_mv, circ_mv
    ]
    text = render_valuation(
        rows, fields=["close", "pe_ttm", "pb", "dv_ttm", "total_mv"], format="markdown"
    )
    assert "—" in text and "0.0" not in text.split("口径")[1].split("\n")[0]
    assert "156,735,231" in text, "市值万元取整千分位，不许科学计数法"
    assert "6.24" in text and "4.10" in text
    assert "| — |" in text, "pe_ttm 是 null：渲染成 '—'，不是 0"
    csv_text = render_valuation(rows, fields=["close", "pe_ttm"], format="csv")
    assert csv_text.startswith("# 估值口径"), "csv 模式口径行走注释（ADR-0011 决定 4 同款）"
