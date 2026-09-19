"""单一查询接口（ADR-0003 决定 2、01 Step 3 验收 2）。

这里钉的是"口径语义"，不是"因子乘没乘"：恒等式那部分交给属性测试（
`tests/properties/test_query_adjust.py`）。单元测试要抓的是三个容易在重构里溜掉的
语义决定——历史区间外也要读因子、None 因子沿用一个已知值、前复权的基准在区间末。
"""

from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from tests.fakes import bar, minute_bar
from zhixing_quant.domain.bar import Bar
from zhixing_quant.storage import layout, query, write

DAY1 = date(2024, 1, 2)
DAY2 = date(2024, 1, 3)
DATETIME1 = datetime(2024, 1, 2, 9, 35)
DATETIME_NOON = datetime(2024, 1, 2, 11, 30)
FULL = (date(2020, 1, 1), date(2029, 12, 31))


def load(tmp_path: Path, adjust: query.Adjust = "raw", **window: date) -> list[Bar]:
    start = window.get("start", FULL[0])
    end = window.get("end", FULL[1])
    return query.read_bars("600519", start, end, adjust=adjust, root=tmp_path)


def test_raw_returns_what_was_stored(tmp_path: Path) -> None:
    write.store_bars([bar(DAY1, 10.0), bar(DAY2, 11.0)], root=tmp_path)
    assert [(b.trade_date, b.close) for b in load(tmp_path)] == [(DAY1, 10.0), (DAY2, 11.0)]


def test_the_window_is_inclusive_and_prunes_years(tmp_path: Path) -> None:
    write.store_bars(
        [bar(date(2023, 12, 29)), bar(DAY1), bar(DAY2), bar(date(2025, 1, 2))], root=tmp_path
    )
    got = load(tmp_path, start=date(2024, 1, 1), end=DAY2)
    assert [b.trade_date for b in got] == [DAY1, DAY2]


def test_a_symbol_never_written_is_an_empty_answer(tmp_path: Path) -> None:
    """没落过盘不是错误：日报会报"今天判了多少只"，查询层不该为一只从没抓过的票抛异常。"""
    write.store_bars([bar(DAY1)], root=tmp_path)
    assert query.read_bars("600520", *FULL, root=tmp_path) == []
    assert query.read_bars("600520", *FULL, adjust="backward", root=tmp_path) == []


def test_a_reversed_window_is_refused_not_answered_with_nothing(tmp_path: Path) -> None:
    """`start > end` 与"这段时间确实没数据"长得一模一样，必须分开：前者是调用方的 bug。"""
    write.store_bars([bar(DAY1)], root=tmp_path)
    with pytest.raises(ValueError, match="区间颠倒"):
        query.read_bars("600519", DAY2, DAY1, root=tmp_path)


def test_a_factor_from_before_the_window_still_applies_inside_it(tmp_path: Path) -> None:
    """除权发生在区间之外，价格照样要复权：只读区间内的因子会把老票在新区间里算回 1.0。"""
    write.store_bars(
        [
            bar(date(2015, 6, 1), 10.0, factor=1.0),
            bar(date(2015, 6, 2), 10.0, factor=8.0),
            bar(DAY1, 12.0, factor=None),
        ],
        root=tmp_path,
    )
    got = load(tmp_path, "backward", start=DAY1, end=DAY1)
    assert [(b.close, b.adj_factor) for b in got] == [(96.0, None)]


def test_a_day_without_its_own_factor_inherits_the_last_known_one(tmp_path: Path) -> None:
    """hfq 帧少一天不是"那天没除权事件"：阶梯函数在两个测量点之间是恒定的，沿用前值。"""
    write.store_bars([bar(DAY1, 10.0, factor=8.0), bar(DAY2, 11.0, factor=None)], root=tmp_path)
    assert [b.close for b in load(tmp_path, "backward")] == [80.0, 88.0]


def test_forward_prices_end_at_the_real_price(tmp_path: Path) -> None:
    """前复权的定义就是"尾日等于实价"（`domain.adjust` 的口径），钉住它，别让它漂成基准=首日。"""
    write.store_bars([bar(DAY1, 10.0, factor=8.0), bar(DAY2, 11.0, factor=8.0)], root=tmp_path)
    assert load(tmp_path, "forward")[-1].close == 11.0


def test_forward_is_anchored_to_the_window_end(tmp_path: Path) -> None:
    """同一天的前复权价随窗口末尾变化：这是前复权的定义而非 bug，但它是使用口径时最容易
    踩空的地方，所以留一条测试当告示牌——真要改成"基准=该票最新已知因子"，先改文档。"""
    write.store_bars([bar(DAY1, 10.0, factor=8.0), bar(DAY2, 11.0, factor=16.0)], root=tmp_path)
    first_day = load(tmp_path, "forward", end=DAY1)[0].close
    both_days = load(tmp_path, "forward", end=DAY2)[0].close
    assert (first_day, both_days) == (10.0, 5.0)


def test_a_symbol_without_any_factor_refuses_adjusted_prices(tmp_path: Path) -> None:
    """不拿 1.0 兜底：那样两个口径会给出一样的价格，"复权其实没生效"就永远看不见。"""
    write.store_bars([bar(DAY1, factor=None)], root=tmp_path)
    assert load(tmp_path, "raw")
    for adjust in ("backward", "forward"):
        with pytest.raises(query.Unadjustable, match="没有可用复权因子"):
            load(tmp_path, adjust)


def test_volume_and_amount_are_not_adjusted(tmp_path: Path) -> None:
    """复权只动价格：成交量"复权"没有谁认的口径，跟着乘一遍会把换手率变成虚构数。"""
    write.store_bars([bar(DAY1, 10.0, factor=8.0)], root=tmp_path)
    adjusted = load(tmp_path, "backward")[0]
    assert (adjusted.volume, adjusted.amount) == (1000.0, 10000.0)
    assert adjusted.adj_factor == 8.0  # 因子列原样带着，口径切换不消费掉证据


def test_adjusted_bars_keep_the_clean_zone_invariants(tmp_path: Path) -> None:
    """缩放后的四价仍要过 `Bar` 的不变量：这里过不了，说明因子给成了负数或零。"""
    write.store_bars([bar(DAY1, 10.0, factor=8.0)], root=tmp_path)
    adjusted = load(tmp_path, "backward")[0]
    assert adjusted.low <= adjusted.open <= adjusted.high
    assert adjusted.low <= adjusted.close <= adjusted.high


def test_a_missing_factor_reads_back_as_missing(tmp_path: Path) -> None:
    """读回来还是"没有因子"：若落盘时被推断成 0 或 1，这条就断了，而复权会安静地失效。"""
    write.store_bars([bar(DAY1, factor=None), bar(DAY2, factor=8.0)], root=tmp_path)
    first = query.read_bars("600519", DAY1, DAY1, root=tmp_path)[0]
    assert first.adj_factor is None
    assert isinstance(first.volume, float)


def test_a_factor_change_beyond_the_window_does_not_leak_in(tmp_path: Path) -> None:
    """区间之后的那次除权不该影响区间内的价格：读因子读到 `end.year` 就停，
    否则同一天的后复权价会随着"这只票后来又除权了"而变，回测就再也复现不出来。"""
    write.store_bars(
        [bar(DAY1, 10.0, factor=8.0), bar(date(2025, 1, 2), 11.0, factor=16.0)], root=tmp_path
    )
    assert [b.close for b in load(tmp_path, "backward", end=DAY1)] == [80.0]


def test_the_factor_staircase_is_cut_at_the_window_end_year(tmp_path: Path) -> None:
    """`_factors` 自己的边界：只读到 `up_to_year` 为止，之后的分区根本不进视野。

    经 `read_bars` 走不到空列表这一支（区间里没有行会先返回空列表），所以直接轰这个接缝。
    上一条测试只能证明区间内的价格没被区间外的除权改动，证明不了"没去读"——两边都给，
    才看得出裁剪真的发生在年份上，而不是碰巧数值相同。
    """
    write.store_bars([bar(date(2025, 1, 2), 10.0, factor=8.0)], root=tmp_path)

    def staircase(con: duckdb.DuckDBPyConnection, up_to_year: int) -> tuple[float, ...]:
        found = query._factors(con, "600519", root=tmp_path, up_to_year=up_to_year)
        return tuple(f.factor for f in found)

    with duckdb.connect() as con:
        assert staircase(con, 2024) == ()
        assert staircase(con, 2025) == (8.0,)


def test_dataset_names_keep_each_other_out(tmp_path: Path) -> None:
    """日线与分钟线共用布局但不共用文件：`dataset` 写错就等于查另一个东西，别混着放。"""
    write.store_bars([minute_bar(DATETIME1)], dataset=layout.MINUTE_5, root=tmp_path)
    assert query.read_bars("600519", *FULL, root=tmp_path) == []
    assert len(query.read_bars("600519", *FULL, dataset=layout.MINUTE_5, root=tmp_path)) == 1
    # 三个分钟周期同样互不相干：把 30 分钟的目录当 5 分钟读，行数量级看着都"正常"。
    write.store_bars(
        [minute_bar(DATETIME_NOON, period=30)], dataset=layout.MINUTE_30, root=tmp_path
    )
    assert len(query.read_bars("600519", *FULL, dataset=layout.MINUTE_5, root=tmp_path)) == 1
    assert len(query.read_bars("600519", *FULL, dataset=layout.MINUTE_30, root=tmp_path)) == 1
    # 拼错的 dataset 名不返回空列表（那与"这只票没数据"长得一样），直接抛。
    with pytest.raises(ValueError, match="未知 dataset"):
        query.read_bars("600519", *FULL, dataset="minute5", root=tmp_path)


def test_minute_prices_are_rescaled_by_the_daily_factor(tmp_path: Path) -> None:
    """分钟线自己不带因子，复权只能借当日日线因子（ADR-0009 决定 4）：同一天K线共用那一天的档。

    两根在同一天的价格是等比放大的，跨天才跳档——这正是"因子是阶梯函数、日内不变"的落盘含义。
    顺序也是这里一起钉的：结果按"交易日、再日内时刻"升序，`ts` 存成 TIMESTAMP 而不是字符串，
    否则 09:35 会排在 15:00 之后而没人报错。
    """
    write.store_bars([bar(DAY1, 10.0, factor=2.0), bar(DAY2, 10.0, factor=4.0)], root=tmp_path)
    write.store_bars(
        [
            minute_bar(datetime(2024, 1, 2, 15, 0), 11.0),
            minute_bar(datetime(2024, 1, 2, 9, 35), 10.0),
            minute_bar(datetime(2024, 1, 3, 9, 35), 10.0),
        ],
        dataset=layout.MINUTE_5,
        root=tmp_path,
    )
    got = query.read_bars(
        "600519", *FULL, adjust="backward", dataset=layout.MINUTE_5, root=tmp_path
    )
    assert [(b.trade_date, b.ts, b.close) for b in got] == [
        (DAY1, datetime(2024, 1, 2, 9, 35), 20.0),
        (DAY1, datetime(2024, 1, 2, 15, 0), 22.0),
        (DAY2, datetime(2024, 1, 3, 9, 35), 40.0),
    ]


def test_minute_adjustment_without_daily_data_is_refused(tmp_path: Path) -> None:
    """只有分钟线时给不出复权价：因子恒在日线 dataset 上，这里不拿 1.0 兜底。

    兜底的后果比抛错难查——后复权价会等于分钟原价，而"这只票从没除权过"与"日线还没落盘"
    在结果里长得一模一样（`Unadjustable` 的类文档记的就是同一条理由）。
    """
    write.store_bars([minute_bar(DATETIME1)], dataset=layout.MINUTE_5, root=tmp_path)
    with pytest.raises(query.Unadjustable, match="没有可用复权因子"):
        query.read_bars("600519", *FULL, adjust="backward", dataset=layout.MINUTE_5, root=tmp_path)
    assert query.read_bars("600519", *FULL, dataset=layout.MINUTE_5, root=tmp_path) != []
