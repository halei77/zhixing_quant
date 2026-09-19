"""L3 属性测试：口径切换的复权恒等式（01 Step 3 验收 2、03 §二 L3）。

轰的是 `query.adjusted_bars`——查询接口三个口径共用的那一个换算函数。恒等式讲的是换算，
与 Parquet 无关；"接口有没有把这个函数接对"（分区裁剪、因子从盘上读回、幂等落盘）是接线
问题，由 `tests/storage/` 的逐例测试钉住。

为什么不在这里落盘：03 §三 已决 1 要每条 ≥1 万例，而一次落盘读回约 80ms，一万例是十三分钟。
所以随机轰击放在唯一承受得起它的地方，磁盘那一段用具体例子钉。要改这个分工，改的是 03 §三
与 CI 预算，不是这里偷偷少跑——这句写在这里，是因为它看起来像放宽了阈值，而它没有。

判据写在测试里，不调被测实现：`_factor_at` 把"因子是阶梯函数、当天及以前最后一个非空值
生效"另写一遍。拿 `factor_on` 当判据的写法是 `factor_on(x) == factor_on(x)`，恒真。
炸点集中在查询层把因子压成变化点那一步（`query._factors`）：台阶日期挪一位、或把区间之前
那次除权读漏，逐日全存的实现照样"自洽"，而价格已经错了——所以例子里既有 None（hfq 缺
一天）也有连续相同的因子（没有新台阶）。
"""

from datetime import date, timedelta
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from tests.fakes import bar
from tests.properties._profile import property_settings
from zhixing_quant.domain.adjust import REL_TOL, AdjustmentFactor, close_enough
from zhixing_quant.domain.bar import Bar
from zhixing_quant.storage import query

#: 复权因子。None 是真实形状（hfq 帧缺这一天），不是坏例子，得留在策略里。
factor = st.none() | st.floats(min_value=1e-3, max_value=1e3, allow_nan=False, allow_infinity=False)

#: 一个测量点：`(第几天, 当天 measured 的复权因子)`。
point = st.tuples(st.integers(min_value=0, max_value=400), factor)


#: 一个因子都没有时：无证据 → 这两个口径都不该给出价格。
_ADJUSTED_WITHOUT_EVIDENCE: tuple[query.Adjust, ...] = ("backward", "forward")


@st.composite
def history(draw: Any) -> list[tuple[date, float | None]]:
    """一只票的几天历史：日期严格递增（干净区一天一行），因子随机含空洞。

    天数与因子分开抽：绑在同一个元组里生成，"日期不重复"就靠策略侥幸而不是构造。
    """
    offsets = sorted({offset for offset, _value in draw(st.lists(point, min_size=1, max_size=6))})
    start = date(2019, 1, 2) + timedelta(days=draw(st.integers(0, 1500)))
    return [(start + timedelta(days=offset), draw(factor)) for offset in offsets]


def _bars_of(points: list[tuple[date, float | None]]) -> list[Bar]:
    """恒定收盘价 10.0：把"价格"与"因子"两个变量分开，出错时看得出是哪一侧动了。"""
    return [bar(day, 10.0, factor=value) for day, value in points]


def _factor_at(points: list[tuple[date, float | None]], on: date) -> float:
    """阶梯函数的独立判据：当天及以前最后一个非空因子；一个都没有按 1.0。"""
    known = [value for day, value in points if day <= on and value is not None]
    return known[-1] if known else 1.0


def _series_of(points: list[tuple[date, float | None]]) -> list[AdjustmentFactor]:
    return [
        AdjustmentFactor(code="600519", effective_on=day, factor=value)
        for day, value in points
        if value is not None
    ]


@given(points=history())
@property_settings
def test_switching_adjust_satisfies_the_identity(points: list[tuple[date, float | None]]) -> None:
    """`后复权 = 不复权 × 当日因子`、`前复权 = 不复权 × 当日因子 ÷ 基准`，且基准恒为比值。

    最后一条（前后两口径之比恒等于基准因子）就是"切换口径不改变相对形状"的说法本身——
    回测敢用复权价，靠的是它而不是某个绝对数值。
    """
    assume(any(value is not None for _day, value in points))
    bars = _bars_of(points)
    series = _series_of(points)
    base = _factor_at(points, bars[-1].trade_date)
    back = query.adjusted_bars(bars, adjust="backward", factors=series)
    fwd = query.adjusted_bars(bars, adjust="forward", factors=series)

    for source, backward, forward in zip(bars, back, fwd, strict=True):
        expected = _factor_at(points, source.trade_date)
        assert close_enough(backward.close, source.close * expected, REL_TOL)
        assert close_enough(forward.close, source.close * expected / base, REL_TOL)
        assert close_enough(backward.close / forward.close, base, REL_TOL)
        assert forward.high >= forward.low  # 缩放不许破坏 OHLC 有序
        # 复权只动价格：量、额、因子列原样带着，切换口径不消费掉证据
        assert (forward.volume, forward.amount, forward.adj_factor) == (
            source.volume,
            source.amount,
            source.adj_factor,
        )


@given(points=history())
@property_settings
def test_forward_is_anchored_to_the_last_day(points: list[tuple[date, float | None]]) -> None:
    """前复权的定义：区间末日等于实价。基准一漂，K线图末日就不再和看板上对得上。"""
    assume(any(value is not None for _day, value in points))
    bars = _bars_of(points)
    last = bars[-1]
    forward = query.adjusted_bars(bars, adjust="forward", factors=_series_of(points))[-1]
    assert close_enough(forward.close, last.close, REL_TOL)


@given(points=history())
@property_settings
def test_backward_is_independent_of_the_window_tail(
    points: list[tuple[date, float | None]],
) -> None:
    """后复权与"区间末在哪"无关：换窗口截断，同一天的价格一字不变。

    这条是回测用后复权的理由，也是它与前复权的分界。截断点随机，所以"最后一个因子恰好
    落在末日"这种侥幸例子不会替它过关。
    """
    assume(any(value is not None for _day, value in points))
    bars = _bars_of(points)
    series = _series_of(points)
    whole = query.adjusted_bars(bars, adjust="backward", factors=series)
    cut = len(bars) // 2
    prefix = query.adjusted_bars(bars[: cut + 1], adjust="backward", factors=series)
    assert [b.close for b in prefix] == [b.close for b in whole[: cut + 1]]


@given(points=history())
@property_settings
def test_a_symbol_without_factors_refuses_adjusted_prices(
    points: list[tuple[date, float | None]],
) -> None:
    """一个因子都没有时不给复权价：拿 1.0 兜底会把"源没给 hfq"洗成"这只票从没除过权"。"""
    bars = _bars_of(points)
    known = _series_of(points)
    if known:
        assert query.adjusted_bars(bars, adjust="backward", factors=known)
    else:
        for adjust in _ADJUSTED_WITHOUT_EVIDENCE:
            with pytest.raises(query.Unadjustable, match="没有可用复权因子"):
                query.adjusted_bars(bars, adjust=adjust, factors=known)
    assert query.adjusted_bars(bars, adjust="raw", factors=known) == bars
