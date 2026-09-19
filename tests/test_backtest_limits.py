"""板价、一字板与成交价边界（ADR-0010 决定 4、决定 9；03-4.4 的判据半边）。

这里只测"制度"这一半：什么形状的一根K线算封死、成交价最高能到哪儿。委托本身（方向、量）
在 `test_backtest_fills.py`——两层分开是因为"封板"与"买不到"不是一回事：涨停封死时卖出照样
成交得了，合成一个函数就会有人只测一半。
"""

from datetime import datetime

import pytest

from tests.fakes import minute_span
from zhixing_quant.backtest.limits import PriceBand, clamp_fill, locked, one_price
from zhixing_quant.domain.bar import Bar

WHEN = datetime(2026, 9, 18, 10, 30)


def flat(price: float, *, volume: float = 5000.0) -> Bar:
    """一根四价相等的K线（一字）。`minute_span` 就是为四个价各给各的场合准备的。"""
    return minute_span(WHEN, open_=price, high=price, low=price, close=price, volume=volume)


def test_the_bound_is_a_pair_of_prices_off_the_prev_close() -> None:
    band = PriceBand.bound(10.0, 100.0)
    assert band.ceiling() == pytest.approx(110.0)
    assert band.floor() == pytest.approx(90.0)
    assert band.judgeable


def test_unlimited_and_unknown_are_two_different_sentences() -> None:
    """都不给板价，但一句话是"今天真的没有板"，另一句是"我不知道板在哪"。

    合成一个 `None` 返回，引擎就没法在报告里分开这两种：前者不该拒单，后者必须拒单。
    """
    unlimited = PriceBand.unlimited("新股无涨跌幅窗口第 2 个交易日")
    unknown = PriceBand.unknown("主数据没有这只票")
    assert unlimited.judgeable and unlimited.ceiling() is None
    assert not unknown.judgeable and unknown.floor() is None
    assert "窗口" in unlimited.note and "主数据" in unknown.note


@pytest.mark.parametrize("prev_close", [0.0, -1.0])
def test_a_band_cannot_be_built_on_a_prev_close_that_does_not_exist(prev_close: float) -> None:
    """拿 0 当"没有昨收"会把板价算成 0，于是任何成交价都"越界"，跌停价还是个负数。

    上市首日真有其事（那天没有昨收），所以这里直接拒建，让调用方去写 `unknown`。
    """
    with pytest.raises(ValueError, match="昨收"):
        PriceBand.bound(10.0, prev_close)


def test_one_price_needs_all_four_prices_together() -> None:
    wide = minute_span(WHEN, open_=10.0, high=10.30, low=9.95, close=10.10)
    assert not one_price(wide)
    assert one_price(flat(10.0))


def test_a_flat_bar_at_the_board_is_locked_in_that_direction() -> None:
    band = PriceBand.bound(10.0, 100.0)
    assert locked(flat(110.0), band) == "up"
    assert locked(flat(90.0), band) == "down"


def test_a_flat_bar_in_the_middle_of_the_band_is_not_a_board() -> None:
    """一根全天只在一个价上成交的K线不等于封板：涨 3% 的冷门票也会这样印。

    判成板就会拒绝一笔现实中做得成的成交，而拒单在报告里与"策略今天没信号"一样只是一行字。
    """
    assert locked(flat(103.0), PriceBand.bound(10.0, 100.0)) is None


def test_a_bar_that_opened_on_the_board_and_walked_away_is_not_locked() -> None:
    """开在板上又打开了的那根不算封死（ADR-0010 代价二说清了它偏在哪一侧）。"""
    bar = minute_span(WHEN, open_=110.0, high=110.0, low=107.20, close=108.0)
    assert locked(bar, PriceBand.bound(10.0, 100.0)) is None


def test_the_board_price_is_rounded_to_cents_so_the_percentage_lags_behind() -> None:
    """交易所把"昨收 × 幅度"四舍五入到分，真实涨停的涨幅因此低于档位。

    10.03 元涨 10% 是 11.033，取分成 11.03 —— 只涨 9.970%。不留这块余量就会在最该拒单的价位
    上判"没封板"，而那个方向是高估可成交性。复权口径帮不上忙：取整发生在真实报价那一侧。
    """
    band = PriceBand.bound(10.0, 10.03)
    assert locked(flat(11.03), band) == "up"
    # 余量也不能再宽：涨 9.47% 的一字是现实中做得成的一笔，判成封板等于替策略判死。
    assert locked(flat(10.98), band) is None


def test_a_cheap_stock_is_still_covered_because_half_a_cent_is_a_bigger_share() -> None:
    """1.07 元的 ST 票涨停是 1.12，只涨 4.67%：档位 5% 要留 0.5pp 才接得住。

    把 `ALLOWANCE_PP` 调小这条就红——那正是它取 0.5 的理由写在码里的地方。
    """
    assert locked(flat(1.12), PriceBand.bound(5.0, 1.07)) == "up"


def test_the_hole_under_one_yuan_is_known_and_pinned_here() -> None:
    """昨收低到两角的票，半分钱就是 2pp：0.24→0.25 只涨 4.17%，判不出板。

    这不是漏网之鱼而是选边的结果（`ALLOWANCE_PP` 模块注释）：余量按真实报价算才对，而真实
    报价不在引擎的口径里。这条测试钉住边界的**位置**——有人调那个常量时它会响。
    """
    assert locked(flat(0.25), PriceBand.bound(5.0, 0.24)) is None


def test_no_board_no_judgement() -> None:
    """不设限与判不出都不判方向：前者真没有板，后者由 `attempt` 那条拒单接住。"""
    bar = flat(130.0)
    assert locked(bar, PriceBand.unlimited("首日")) is None
    assert locked(bar, PriceBand.unknown("认不出代码")) is None


def test_the_fill_price_is_pushed_back_inside_the_board() -> None:
    band = PriceBand.bound(10.0, 100.0)
    assert clamp_fill(112.0, band, "buy") == pytest.approx(110.0)
    assert clamp_fill(112.0, band, "sell") == pytest.approx(112.0)
    assert clamp_fill(88.0, band, "sell") == pytest.approx(90.0)
    assert clamp_fill(105.0, band, "buy") == pytest.approx(105.0)


def test_no_board_means_nothing_to_clamp_against() -> None:
    for band in (PriceBand.unlimited("新股窗口"), PriceBand.unknown("缺昨收")):
        assert clamp_fill(130.0, band, "buy") == 130.0
