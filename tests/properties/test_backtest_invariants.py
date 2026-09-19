"""L3 属性测试：成交判定的口径无关性（ADR-0010 决定 9）。

决定 9 那句"同一天的四个价乘同一个因子，相对昨收涨了多少在两种口径下数值相同"是"引擎只吃
后复权价"这条路的地基：它要是假的，用后复权价判出来的那堵墙就不是交易所那堵墙，而错在哪一侧
没人知道。所以这里不测实现自洽，测的是**把整批价格与昨收同时乘任意正常数，判定结果一个都不变**。

这个不变式之所以能成立，是因为 `one_price` 用相对容差、涨跌幅是比值、`ALLOWANCE_PP` 是常数。
哪天有人把余量改成 `0.5 / prev_close` 那种"看起来更准"的写法（它算的是后复权价里有几个半分钱，
而半分钱活在真实报价那一侧），这条测试立刻红——那正是它要防的事。

抽例子时避开阈值 ±0.02 个百分点：缩放前后是两次不同的浮点乘法，恰好压在阈值上的那一根会翻边，
那是 1e-16 量级的噪声而不是判据依赖口径。这句写在这里是因为它看起来像在放宽测试，而它没有。

跑量见 `_profile.py`（03 §三 已决 1：日常 CI 每条 ≥1 万例）。不落盘：判据与 Parquet 无关，
把 IO 塞进一万例只会让 CI 慢到没人跑，还测不到别的形状。
"""

from datetime import datetime
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from tests.fakes import minute_span
from tests.properties._profile import property_settings
from zhixing_quant.backtest.fills import Fill, Order, Outcome, Reject, attempt
from zhixing_quant.backtest.limits import ALLOWANCE_PP, PriceBand, clamp_fill, locked
from zhixing_quant.backtest.spec import Cost, Side
from zhixing_quant.domain.bar import Bar, stamp_of

WHEN = datetime(2026, 9, 18, 14, 0)
SIGNAL_AT = stamp_of(WHEN.date(), WHEN)  # `Order.signal_at` 要的是时钟元组，不是一个时刻
#: 与 `config/gate.toml` 的 `limits_pct` 同一组档位。测试自抽而不是读配置：这里轰的是
# "判定与价格空间无关"，与那张表里具体写了几分钱无关。
BANDS = st.sampled_from([5.0, 10.0, 20.0, 30.0])
STATES = st.sampled_from(["bound", "unlimited", "unknown"])
SPREADS = st.sampled_from([0.0, 0.004, 0.02])  # 0 = 一字，其余是当天真在动
#: 成交量抽的是"流动性能不能挡住一笔 300 股"的四档：没有量、薄、刚好不够、充裕。
#: 它不参与缩放（换复权口径不改成交股数），抽进来是为了让最后那条流水线测试真走到
#  `zero_volume` 与 `participation` 两个分支，而不是只在"能成交"那一侧打转。
VOLUMES = st.sampled_from([0.0, 1000.0, 5000.0, 1e6])
#: 抽成带类型的元组而不是字面列表：`sampled_from(["buy", "sell"])` 会推出 `str`，
#: 而 `clamp_fill` 要的是 `Side` 那个 Literal——类型在测试里也不能靠"看着像"。
SIDES: tuple[Side, ...] = ("buy", "sell")
EDGE = 0.02
SHARES = 300  # 07 §二 那 3 手
COST = Cost(
    commission_pct=0.025, commission_min=5.0, stamp_pct=0.05, transfer_pct=0.001, slippage_pct=0.02
)
PARTICIPATION = 5.0


def _band(state: str, pct: float, prev_close: float) -> PriceBand:
    if state == "bound":
        return PriceBand.bound(pct, prev_close)
    if state == "unlimited":
        return PriceBand.unlimited("新股无涨跌幅窗口")
    return PriceBand.unknown("主数据没有这只票")


def _scaled(bar: Bar, factor: float) -> Bar:
    """整根K线乘一个正常数：这就是"换口径"在数据上的样子（ADR-0009 决定 4）。"""
    return minute_span(
        WHEN,
        open_=bar.open * factor,
        high=bar.high * factor,
        low=bar.low * factor,
        close=bar.close * factor,
        volume=bar.volume,
    )


def _rescaled_band(band: PriceBand, factor: float) -> PriceBand:
    if band.state != "bound":
        return band
    return PriceBand.bound(band.pct, band.prev_close * factor)


@st.composite
def case(draw: Any) -> tuple[Bar, PriceBand, float]:
    """一根K线 + 那天的板 + 一个缩放系数。三个一起抽，才问得出"乘一遍之后判定变不变"。"""
    prev_close = draw(st.floats(min_value=1.0, max_value=2000.0, allow_nan=False))
    pct = draw(BANDS)
    state = draw(STATES)
    move = draw(st.floats(min_value=-30.0, max_value=30.0, allow_nan=False))
    assume(abs(abs(move) - (pct - ALLOWANCE_PP)) > EDGE)
    price = prev_close * (1.0 + move / 100.0)
    spread = draw(SPREADS)
    bar = minute_span(
        WHEN,
        open_=price,
        high=price * (1.0 + spread),
        low=price * (1.0 - spread),
        close=price,
        volume=draw(VOLUMES),
    )
    scale = draw(st.floats(min_value=1e-3, max_value=1e6, allow_nan=False))
    return bar, _band(state, pct, prev_close), scale


@property_settings
@given(case())
def test_the_board_verdict_does_not_depend_on_the_price_space(
    trio: tuple[Bar, PriceBand, float],
) -> None:
    bar, band, factor = trio
    assert locked(_scaled(bar, factor), _rescaled_band(band, factor)) == locked(bar, band)


@property_settings
@given(case())
def test_the_board_prices_scale_with_the_bars(trio: tuple[Bar, PriceBand, float]) -> None:
    """涨停价必须跟着价格一起乘。不跟着乘，"越界"就是口径的产物而不是制度的产物。

    比较留 1e-9 的相对容差：`(prev×k)×(1+幅)` 与 `(prev×(1+幅))×k` 是两次不同的浮点乘法。
    """
    _, band, factor = trio
    moved = _rescaled_band(band, factor)
    # 板价先取进局部变量：`assert band.ceiling() is not None`  narrowed 不了下一次调用，
    # mypy 说得对——两次调用之间盘上什么都没变，但语言不知道。
    ceiling, floor = band.ceiling(), band.floor()
    if ceiling is None or floor is None:
        assert moved.ceiling() is None and moved.floor() is None
        return
    assert moved.ceiling() == pytest.approx(ceiling * factor, rel=1e-9)
    assert moved.floor() == pytest.approx(floor * factor, rel=1e-9)


@property_settings
@given(case(), st.sampled_from(SIDES))
def test_clamping_the_fill_is_the_same_experiment_after_scaling(
    trio: tuple[Bar, PriceBand, float], side: Side
) -> None:
    """把成交价压回板内这件事，换口径前后是同一笔交易。"""
    bar, band, factor = trio
    raw = bar.open * (1.1 if side == "buy" else 0.9)
    plain = clamp_fill(raw, band, side)
    moved = clamp_fill(raw * factor, _rescaled_band(band, factor), side)
    assert moved == pytest.approx(plain * factor, rel=1e-9)


def _verdict(outcome: Outcome) -> str:
    """成了就叫 `fill`，没成就叫那条原因。比较的是**哪一档拦下的**，不是它写的那句话。"""
    return outcome.reason if isinstance(outcome, Reject) else "fill"


@property_settings
@given(case(), st.sampled_from(SIDES))
def test_the_whole_fill_pipeline_is_the_same_experiment_after_scaling(
    trio: tuple[Bar, PriceBand, float], side: Side
) -> None:
    """决定 9 说的那"四条判据"要一起测：引擎用的是 `attempt` 这条流水线，判序也在里面。

    逐条各测一遍留下一个缝：有人把参与率改成按**成交额**算（`shares × price / amount`——它看着
    比按股数更讲道理，实际把价格灌进了一个本该只看量的判据），三条单点测试全都还绿，而换一口
    径就能多成交几笔。这一条会红。

    只比"成了还是哪条拒"与成交价，不比 `fee`：最低佣金 5 元是**钱**上的常数，不跟着价格缩放，
    这与 `ALLOWANCE_PP` 是判定上的常数同一回事——它该不跟着变。
    """
    bar, band, factor = trio
    order = Order(code="600519", side=side, shares=SHARES, signal_at=SIGNAL_AT)
    plain = attempt(order, bar, band, cost=COST, max_participation_pct=PARTICIPATION)
    moved = attempt(
        order,
        _scaled(bar, factor),
        _rescaled_band(band, factor),
        cost=COST,
        max_participation_pct=PARTICIPATION,
    )
    assert _verdict(moved) == _verdict(plain)
    if isinstance(plain, Fill):
        assert isinstance(moved, Fill)
        assert moved.price == pytest.approx(plain.price * factor, rel=1e-9)
