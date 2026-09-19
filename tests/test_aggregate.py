"""分钟K线 → 合成日K（01 Step 4 验收 2 的第一半；ADR-0009 决定 2/4/6）。

这里只判"装配"：哪个字段从哪一根来、哪些批必须拒。判据（价相等、量额留容差）在
`test_reconcile.py`——两层各测各的，合成一旦改了字段归属，只会红这一边。

`ts` 是收盘时刻（右端点），所以"最早那根"就是覆盖当天第一个五分钟的那根：09:35 那根的
`open` 是当天开盘价。这条在源侧实测过，测试要防的是"以后有人把排序改成 `ts` 的相反方向"。
"""

from datetime import date, datetime

import pytest

from tests.fakes import minute_span
from zhixing_quant.domain.aggregate import daily_from_minutes

DAY = date(2024, 1, 3)
MORNING = datetime(2024, 1, 3, 9, 35)
NOON = datetime(2024, 1, 3, 11, 30)
TAIL = datetime(2024, 1, 3, 15, 0)


def test_an_empty_batch_is_none_not_an_error() -> None:
    """ "这天一根都没有"是覆盖率要的那句话，得能和外层区分开。

    抛错的话，日报上"这个源今天没给分钟线"和"这批数据有一根坏了"会长成同一个异常。
    """
    assert daily_from_minutes([]) is None


def test_each_field_comes_from_the_bar_its_edge_belongs_to() -> None:
    """开盘取最早那根的 open、收盘取最晚那根的 close、极值跨全天——四根各给各的数，取错就红。"""
    day = daily_from_minutes(
        [
            minute_span(MORNING, open_=10.00, high=10.10, low=9.60, close=10.05, volume=300.0),
            minute_span(NOON, open_=10.05, high=10.30, low=9.90, close=10.20, volume=100.0),
            minute_span(TAIL, open_=10.20, high=10.40, low=9.80, close=10.10, volume=200.0),
        ]
    )
    assert day is not None
    assert (day.open, day.close) == (10.00, 10.10)
    assert (day.high, day.low) == (10.40, 9.60)
    assert (day.volume, day.amount) == (
        600.0,
        10.05 * 300 + 10.20 * 100 + 10.10 * 200,
    )


def test_input_order_does_not_decide_which_bar_is_the_opening_one() -> None:
    """源给的是"最近 1970 根"，顺序由它决定；批次乱序时开盘价仍然是最早那根的。

    落盘按 `(trade_date, ts)` 排过，但合成不认识落盘——它拿到的可能是内存里现拼的一批。
    """
    bars = [
        minute_span(TAIL, open_=10.20, high=10.40, low=9.80, close=10.10),
        minute_span(MORNING, open_=10.00, high=10.10, low=9.90, close=10.05),
        minute_span(NOON, open_=10.05, high=10.30, low=9.70, close=10.20),
    ]
    day = daily_from_minutes(bars)
    assert day is not None
    assert (day.open, day.close) == (10.00, 10.10)
    assert day.low == 9.70


def test_the_synthetic_bar_keeps_its_parents_identity() -> None:
    """`source` 留成分钟源、`adj_factor` 留空。

    因子为空是决定 4：分钟线自己不带因子，复权是"分钟价 × 当日日线因子"。合成出的日K若挂上
    一个从别处抄来的因子，下游就会把它当成日线因子源，那条唯一换算路径于是有了第二个入口。
    """
    day = daily_from_minutes([minute_span(MORNING, open_=10.0, high=10.1, low=9.9, close=10.05)])
    assert day is not None
    assert (day.source, day.symbol, day.trade_date) == ("akshare_minute_5", "600519", DAY)
    assert day.adj_factor is None
    assert not day.is_suspended


def test_a_partial_day_synthesizes_anyway() -> None:
    """尾巴上最早那天只有两三根（实测：42/247/493 天深度的最旧一天就是这样）。

    这里不能拒：拒了以后对账看到的是"那天没有分钟行"，而真话是"那天只抓到尾巴上的一截"。
    量额差得离谱由 R011 的容差去说，不是由合成层装死。
    """
    day = daily_from_minutes(
        [
            minute_span(TAIL, open_=10.20, high=10.40, low=9.80, close=10.10, volume=200.0),
        ]
    )
    assert day is not None
    assert (day.open, day.close, day.volume) == (10.20, 10.10, 200.0)


def test_mixing_periods_would_double_count_volume() -> None:
    """两种周期混成一批时量额会翻倍，而 OHLC 看起来完全正常——所以抛错，不"尽力合成"。

    分钟任务的批次本来就按 dataset 分开（一只票 × 一个周期 = 一批），这里是兜住"以后有人图省事
    把两个周期拼一起交进来"。那种错比抛错贵得多。
    """
    mixed = [
        minute_span(MORNING, open_=10.0, high=10.1, low=9.9, close=10.05),
        minute_span(TAIL, open_=10.05, high=10.2, low=10.0, close=10.10, dataset="minute_30"),
    ]
    with pytest.raises(ValueError, match="量额会翻倍"):
        daily_from_minutes(mixed)


def test_one_call_one_symbol_one_day() -> None:
    """跨票、跨天合成的那根日K谁都不是：既不属于这只票，也不属于那一天。"""
    with pytest.raises(ValueError, match="一次只合成一只票的一天"):
        daily_from_minutes(
            [
                minute_span(MORNING, open_=10.0, high=10.1, low=9.9, close=10.05),
                minute_span(NOON, open_=10.0, high=10.1, low=9.9, close=10.05, symbol="300750"),
            ]
        )
    with pytest.raises(ValueError, match="一次只合成一只票的一天"):
        daily_from_minutes(
            [
                minute_span(MORNING, open_=10.0, high=10.1, low=9.9, close=10.05),
                minute_span(
                    datetime(2024, 1, 2, 9, 35),
                    open_=9.8,
                    high=9.9,
                    low=9.7,
                    close=9.85,
                ),
            ]
        )


def test_a_bar_without_a_close_time_is_refused_not_placed() -> None:
    """没有 `ts` 就分不清哪根最早、哪根最晚，开盘价与收盘价于是全靠猜。

    不 `or trade_date` 兜底：那样一根日线混进批里会"成功"合成，而它的 close 是一天的收盘，
    被当成某根分钟K线的收盘参与排序。
    """
    broken = minute_span(MORNING, open_=10.0, high=10.1, low=9.9, close=10.05)
    with pytest.raises(ValueError, match="没有收盘时刻"):
        daily_from_minutes([broken, broken.model_copy(update={"ts": None})])
