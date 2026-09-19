"""L3 属性测试：K线表那两条"错了不会响"的性质（06 §六、ADR-0011 决定 3/5）。

单测能判"我给的那三根排对了"，判不了"任何一根都不会排错"。这张表要选的性质正是那两条最安静
的：把输入顺序打乱，输出必须一字不改（"正序"是**这张表**的性质而不是上游的巧合）；任取一组合法
价格，价格格必须仍是两位小数、量额格必须仍没有小数点。这两条哪天被人"顺手优化"掉（比如改成不
排序、上游给什么就印什么），单测里那几根K线很可能还是对的，而提示词已经开始给大模型倒着画的
走势。

换手率那条是决定 5 的另一半：分子在K线上、分母是调用方给的流通股本，所以判的是"格子里那个数
确实等于 成交量 ÷ 流通股本 × 100"，而不是"这一列存在"。

跑量见 `_profile.py`（03 §三 已决 1：日常 CI 每条 ≥1 万例）。不落盘：判据与 Parquet 无关，
把 IO 塞进一万例只会让 CI 慢到没人跑，还测不到别的形状。
"""

import re
from datetime import date, datetime
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from tests.fakes import daily_span, minute_span
from tests.properties._profile import property_settings
from zhixing_quant.domain.bar import Bar
from zhixing_quant.site.table import render
from zhixing_quant.site.templates import Adjust, Format

#: 价格上限 1e5：后复权价能到这个量级（茅台因子 8.88），再大就只是在测格式化函数。
PRICES = st.floats(min_value=0.01, max_value=1e5, allow_nan=False, allow_infinity=False)
SPREADS = st.floats(min_value=0.0, max_value=0.05, allow_nan=False, allow_infinity=False)
#: 量额抽到 1e9：A 股最大盘的流通股本在这个量级，`{:.0f}` 在这个范围内不会退化成科学计数。
QUANTITY = st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
DAYS = st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 12, 31))
MOMENTS = st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2030, 12, 31, 23, 59))
SHARES = st.floats(min_value=1.0, max_value=1e10, allow_nan=False, allow_infinity=False)
FIELDS = ("open", "high", "low", "close", "volume", "amount")
PRICE_CELLS = slice(1, 5)
QUANTITY_CELLS = slice(5, None)
TWO_DP = re.compile(r"^\d+\.\d{2}$")
WHOLE = re.compile(r"^\d+$")


@st.composite
def _ohlc(draw: Any) -> tuple[float, float, float, float]:
    """一根合法的K线：开收各抽一次，高低价从两者之上/之下推开，于是 OHLC 天然有序。

    不去抽"任意四个浮点"再用 `assume` 筛：那样的拒绝率会随维度涨上去，一万例就变成在测筛选器。
    """
    open_, close = draw(PRICES), draw(PRICES)
    top, bottom = max(open_, close), min(open_, close)
    return open_, top * (1.0 + draw(SPREADS)), bottom * (1.0 - draw(SPREADS)), close


@st.composite
def _daily(draw: Any) -> list[Bar]:
    """若干根日线，日期互不相同——`ts=None` 时"日期不同"就是"时刻不同"。

    必须互不相同：同一天的两根怎么排都算"正序"，输入顺序就此泄进了输出，第一条性质当场失效。
    """
    out: list[Bar] = []
    for day in draw(st.lists(DAYS, min_size=1, max_size=9, unique=True)):
        open_, high, low, close = draw(_ohlc())
        out.append(
            daily_span(
                day,
                open_=open_,
                high=high,
                low=low,
                close=close,
                volume=abs(draw(QUANTITY)),
                amount=abs(draw(QUANTITY)),
            )
        )
    return out


@st.composite
def _minute(draw: Any) -> list[Bar]:
    """若干根分钟线，时刻互不相同（同一条理由，只是键换成了 `ts`）。"""
    out: list[Bar] = []
    for when in draw(st.lists(MOMENTS, min_size=1, max_size=9, unique=True)):
        open_, high, low, close = draw(_ohlc())
        out.append(
            minute_span(
                when,
                open_=open_,
                high=high,
                low=low,
                close=close,
                volume=abs(draw(QUANTITY)),
            )
        )
    return out


@st.composite
def _pair(draw: Any, bars: st.SearchStrategy[list[Bar]]) -> tuple[list[Bar], list[Bar]]:
    """一份K线 + 它的任意一种排列。第一条性质要的就是这一对。"""
    given = draw(bars)
    return given, draw(st.permutations(given))


def _render(
    bars: list[Bar],
    format: Format = "markdown",
    fields: tuple[str, ...] = FIELDS,
    adjust: Adjust = "backward",
    shares: float | None = None,
) -> str:
    return render(bars, format=format, fields=fields, adjust=adjust, float_shares=shares)


def _markdown(text: str) -> list[list[str]]:
    """数据行拆成格子：表头与分隔行不留（分隔行是全场唯一含 `---` 的一行）。"""
    lines = [x for x in text.splitlines() if x.startswith("|") and "---" not in x]
    return [[cell.strip() for cell in line.strip("|").split("|")] for line in lines][1:]


def _csv(text: str) -> list[list[str]]:
    """紧凑模式没有竖线可切，而且第 0 行是 `#` 口径行、第 1 行才是表头。"""
    return [line.split(",") for line in text.splitlines()[2:]]


@property_settings
@given(_pair(_daily()))
def test_the_table_does_not_depend_on_the_input_order(
    pair: tuple[list[Bar], list[Bar]],
) -> None:
    bars, shuffled = pair
    assert _render(bars) == _render(shuffled)


@property_settings
@given(_pair(_minute()))
def test_the_same_holds_for_minute_bars(pair: tuple[list[Bar], list[Bar]]) -> None:
    """分钟线单独一条：日线排 `trade_date`、分钟线排 `ts`，两条式子看着像一条。

    `_ordered` 用的是 `(日期, 时刻)` 那个共同键，所以实现上确实是一条；但只测日线的话，有人把它
    改成只按 `trade_date` 排也测不出来——那一天里的 48 根会全并列，表的顺序就又交回上游了。
    """
    bars, shuffled = pair
    assert _render(bars) == _render(shuffled)


@property_settings
@given(_daily())
def test_every_cell_keeps_the_documented_shape(bars: list[Bar]) -> None:
    """06 §六：价格 2 位小数、量额取整。抽到的任何一根都不许例外。"""
    rows = _markdown(_render(bars))
    assert len(rows) == len(bars)
    for row in rows:
        assert len(row) == 1 + len(FIELDS)
        assert all(TWO_DP.match(cell) for cell in row[PRICE_CELLS])
        assert all(WHOLE.match(cell) for cell in row[QUANTITY_CELLS])


@property_settings
@given(_daily())
def test_the_time_column_never_goes_backwards(bars: list[Bar]) -> None:
    """ISO 日期与 `%Y-%m-%d %H:%M` 都是字典序=时间序，所以这条能直接比字符串。"""
    stamps = [row[0] for row in _markdown(_render(bars))]
    assert stamps == sorted(stamps)


@property_settings
@given(bars=_daily(), shares=SHARES)
def test_the_turnover_cell_is_the_volume_share_of_the_float(bars: list[Bar], shares: float) -> None:
    """换手率那一格判的是算式，不是"这列存在"（ADR-0011 决定 5）。

    用紧凑模式：它没有对齐空格，格子拿回来就是数本身，不必先剥竖线再担心剥错。
    """
    rows = _csv(_render(bars, format="csv", fields=("close", "turnover"), shares=shares))
    by_day = {bar.trade_date.isoformat(): bar for bar in bars}
    assert len(rows) == len(bars)
    for row in rows:
        bar = by_day[row[0]]
        assert row[2] == f"{bar.volume * 100.0 / shares:.2f}"
