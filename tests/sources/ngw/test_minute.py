"""ngw 1 分钟适配器 L1：`ts` 映射边界、分→元、`curvalue` 不除 100、0 行显式分支、不复权路径。

边界各一条钉死（09:30 / 11:30 / 13:01 / 15:00）：这条映射是设计合成 §2.3 点名「最容易
出错的地方」，而实测推翻了合成里的「缺 15:00、末根 14:59」（那是 start 截止参数的排他
边界假象）——所以边界不测「合成说的那样」，测**实测标签 → 右端点 identity** 的原文。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from zhixing_quant.sources.ngw import minute


def _row(times: str, **over: Any) -> dict[str, Any]:
    """一行源侧原样 timedata。数值全字符串·分，照实测形状。"""
    row: dict[str, Any] = {
        "times": times,
        "openp": "125158",
        "highp": "125200",
        "lowp": "125100",
        "nowv": "125180",
        "curvol": "27663",
        "curvalue": "34219131",
    }
    row.update(over)
    return row


def test_ts_is_the_label_itself_at_every_session_boundary() -> None:
    """四条边界一杆打完：identity 映射（ADR-0009 右端点）的原文就是标签本身。"""
    rows = [
        _row("20260924093000"),  # 开盘竞价栏：实测存在、OHLC 全等开盘价
        _row("20260924113000"),  # 午末：+1min 会把它推进午休（11:31），identity 不会
        _row("20260924130100"),  # 午后首根：13:00 不存在，13:01 就是午后第一根
        _row("20260924145900"),
        _row("20260924150000"),  # 末根：实测存在（合成说「缺 15:00」是翻页截止假象）
    ]
    drafts = minute.minute_drafts(rows, symbol="600519")
    assert [draft.ts for draft in drafts] == [
        datetime(2026, 9, 24, 9, 30),
        datetime(2026, 9, 24, 11, 30),
        datetime(2026, 9, 24, 13, 1),
        datetime(2026, 9, 24, 14, 59),
        datetime(2026, 9, 24, 15, 0),
    ]
    assert [draft.trade_date for draft in drafts] == [date(2026, 9, 24)] * 5


def test_a_full_day_has_no_collision_and_no_lunch_bar() -> None:
    """完整一天 241 根（121 上午 + 120 下午）映射后 241 个互异 ts，午休里一根都没有。"""
    morning: list[str] = []
    for hour, start_minute, end_minute in ((9, 30, 59), (10, 0, 59), (11, 0, 30)):
        for minute_ in range(start_minute, end_minute + 1):
            morning.append(f"20260924{hour:02d}{minute_:02d}00")
    afternoon: list[str] = []
    for hour, start_minute, end_minute in ((13, 1, 59), (14, 0, 59), (15, 0, 0)):
        for minute_ in range(start_minute, end_minute + 1):
            afternoon.append(f"20260924{hour:02d}{minute_:02d}00")
    assert len(morning) == 121 and len(afternoon) == 120
    drafts = minute.minute_drafts([_row(t) for t in morning + afternoon], symbol="600519")
    stamps = [draft.ts for draft in drafts]
    assert len(stamps) == 241
    assert len(set(stamps)) == 241, "两根K线一个主键就是 R008 的重复行"
    assert all(stamp is not None for stamp in stamps)
    assert stamps == sorted(stamp for stamp in stamps if stamp is not None)
    lunch = [
        ts
        for ts in stamps
        if ts is not None and datetime(2026, 9, 24, 11, 30) < ts < datetime(2026, 9, 24, 13, 1)
    ]
    assert lunch == []


def test_prices_come_as_fen_strings_and_leave_as_yuan() -> None:
    """硬约束 4 前半：OHLC 字符串·分 → ÷100 → 元。"""
    (draft,) = minute.minute_drafts([_row("20260924093100")], symbol="600519")
    assert (draft.open, draft.high, draft.low, draft.close) == (1251.58, 1252.0, 1251.0, 1251.8)


def test_curvalue_is_yuan_and_is_never_divided_by_100() -> None:
    """硬约束 4 后半：`curvalue` = 元。实测 27663×1237.00=34,219,131 与该字段逐分相等；
    照 Qoute T-001 的「/100」抄会把成交额缩小 100 倍（fixture 按分造数，量纲记反了）。"""
    row = _row("20260924150000", curvol="27663", curvalue="34219131", nowv="123700")
    (draft,) = minute.minute_drafts([row], symbol="600519")
    assert draft.amount == 34_219_131.0
    assert draft.volume == 27663.0  # 股，原样
    assert draft.close == 1237.0
    assert draft.amount == pytest.approx(draft.volume * draft.close)  # 元口径自洽的那一验


def test_off_session_labels_are_dropped_and_unparseable_times_are_not_invented() -> None:
    """午休/盘外标签不是一根K线，整行丢；times 认不出的行不编日期，留空交 R010。"""
    drafts = minute.minute_drafts(
        [
            _row("20260924113100"),  # 午休第一分钟
            _row("20260924120000"),  # 午休
            _row("20260924130000"),  # 实测不存在的 13:00
            _row("20260924090000"),  # 盘前
            _row("20260924150100"),  # 盘后
            _row("garbage"),  # 解析不出
            _row("20260924093100"),  # 正常一根要保住
        ],
        symbol="600519",
    )
    stamps = [draft.ts for draft in drafts]
    assert stamps[-2] is None  # garbage：留空（R010 的「缺 trade_date」现场），不编时刻
    assert drafts[-2].trade_date is None
    assert stamps[-1] == datetime(2026, 9, 24, 9, 31)
    assert stamps[:-2] == [], f"盘外/午休标签没被丢干净：{stamps}"


def test_zero_rows_is_the_explicit_empty_branch() -> None:
    """硬约束 3 的适配器侧：0 行 → 空列表（「这批没有行」），不是「到底了」也不是异常。"""
    assert minute.minute_drafts([], symbol="600519") == []


def test_source_is_ngw_minute_one_and_unknown_periods_are_refused() -> None:
    """04 §三 按源打分：ngw 1 分钟是独立源标识；周期表只有 layout 一份。"""
    assert minute.source_for("1") == "ngw_minute_1"
    (draft,) = minute.minute_drafts([_row("20260924093100")], symbol="600519")
    assert draft.source == "ngw_minute_1"
    with pytest.raises(ValueError, match="不支持的分钟周期"):
        minute.source_for("15")


def test_no_adjust_factor_and_row_order_are_left_as_the_source_gave_them() -> None:
    """分钟线不落因子（ADR-0009 决定 4）；行序原样保留——在这里排一次序，R009 就瞎了。"""
    late = _row("20260924150000")
    early = _row("20260924093100")
    drafts = minute.minute_drafts([late, early], symbol="sh600519")
    assert [draft.ts for draft in drafts] == [
        datetime(2026, 9, 24, 15, 0),
        datetime(2026, 9, 24, 9, 31),
    ]
    assert all(draft.adj_factor is None for draft in drafts)
    assert all(draft.is_suspended is False for draft in drafts)
    assert all(draft.symbol == "sh600519" for draft in drafts)  # 归一留给契约层（Bar）


def test_missing_columns_become_none_not_an_exception() -> None:
    """列名漂移 → None → R010 整批 FATAL。在这里抛 KeyError 等于把采集炸成半批入库。"""
    (draft,) = minute.minute_drafts([{"times": "20260924093100", "px": "125180"}], symbol="600519")
    assert draft.close is None
    assert draft.volume is None
    assert draft.amount is None
    assert draft.ts == datetime(2026, 9, 24, 9, 31)
