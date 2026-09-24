"""停牌行 → `Interval` 与并集合并（任务 #57；04 §二 R006 豁免通道的行级入口）。

测的三类失效：

1. **解析口径**：东财含头含尾；百度 end = 复牌 − 1（复牌日本身有分钟行）、NaN 复牌按当日。
2. **代码陷阱**：5 位港股码 zfill 会落进 `001` 主板前缀；三板 `872707` 前缀长得像北交所。
3. **并集合并**：相接/重叠/包含必须合成一段——不合就在 `SecurityMaster` 装载时抛
   `MasterConflict`，`read_master` 六个入口一起退出码 2。相邻不相接的**不许**合并。
"""

from datetime import date

import pytest

from zhixing_quant.domain.security import Interval, Listing, SecurityMaster
from zhixing_quant.sources.akshare.suspend import (
    intervals_from_baidu,
    intervals_from_em,
    merge_intervals,
)


def _em(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "代码": "000851",
        "名称": "*ST高鸿",
        "停牌时间": "2025-09-29",
        "停牌截止时间": "2025-11-10",
        "预计复牌时间": "2025-11-11",
    }
    row.update(over)
    return row


def _baidu(**over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "股票代码": "002762",
        "股票简称": "金发拉比",
        "交易所代码": "SZ",
        "停牌时间": "2025-04-23",
        "复牌时间": "2025-04-24",
        "证券类型": "stock",
        "市场类型": "ab",
    }
    row.update(over)
    return row


# --- 东财解析 ----------------------------------------------------------------------


def test_em_interval_is_inclusive_on_both_ends() -> None:
    """*ST高鸿 000851 佐证：截止 2025-11-10 / 预计复牌 2025-11-11 → end 取截止日**当天**。"""
    (iv,) = intervals_from_em([_em()])
    assert (iv.code, iv.start, iv.end) == ("000851", date(2025, 9, 29), date(2025, 11, 10))
    assert iv.covers(date(2025, 11, 10)) and not iv.covers(date(2025, 11, 11))


def test_em_missing_end_becomes_open_ended() -> None:
    """实测 10/865 行缺 `停牌截止时间`：未复牌 = 持续中（`end=None`），不是丢行。"""
    (iv,) = intervals_from_em([_em(停牌截止时间=None)])
    assert iv.end is None and iv.covers(date(2030, 1, 1))


def test_em_missing_start_drops_the_row() -> None:
    """没有起点摆不出区间。丢行的方向是"少豁免"，R006 照旧计缺失（fail-closed）。"""
    assert intervals_from_em([_em(停牌时间=None)]) == ()


def test_em_rejects_five_digit_and_non_a_codes() -> None:
    assert intervals_from_em([_em(代码="01428")]) == ()  # 港股 5 位
    assert intervals_from_em([_em(代码="900901")]) == ()  # B股


# --- 百度解析 ----------------------------------------------------------------------


def test_baidu_end_is_resume_day_minus_one() -> None:
    """002762：停牌 2025-04-23 → 复牌 2025-04-24，分钟盘只缺 04-23，end 取复牌 − 1。"""
    (iv,) = intervals_from_baidu([_baidu()])
    assert (iv.start, iv.end) == (date(2025, 4, 23), date(2025, 4, 23))
    assert not iv.covers(date(2025, 4, 24))  # 复牌日当天**不算**停牌


def test_baidu_nan_resume_means_same_day() -> None:
    for empty in (None, float("nan"), "", "nan"):
        (iv,) = intervals_from_baidu([_baidu(复牌时间=empty)])
        assert (iv.start, iv.end) == (date(2025, 4, 23), date(2025, 4, 23))


def test_baidu_drops_hk_and_neeq_rows_without_zfill_trap() -> None:
    """zfill 陷阱：`'01939'→'001939'` 会落进 `001` 主板前缀——5 位码必须整行丢掉，
    不能补零。三板 `872707` 前缀像北交所（`87`），靠 `交易所代码=NQ` 分开。"""
    rows = [
        _baidu(股票代码="01939", 交易所代码="HK", 市场类型="hk"),
        _baidu(股票代码="01428", 交易所代码="HK", 市场类型="hk"),
        _baidu(股票代码="872707", 交易所代码="NQ"),
        _baidu(股票代码="838879", 交易所代码="NQ"),
        _baidu(股票代码="603159", 交易所代码="SH"),  # 留下的那条
    ]
    assert [iv.code for iv in intervals_from_baidu(rows)] == ["603159"]


def test_baidu_requires_stock_ab_and_known_exchange() -> None:
    assert intervals_from_baidu([_baidu(证券类型="fund")]) == ()
    assert intervals_from_baidu([_baidu(市场类型="hk")]) == ()
    assert intervals_from_baidu([_baidu(交易所代码="TW")]) == ()


def test_baidu_accepts_sh_sz_bj() -> None:
    codes = [
        ("603159", "SH"),
        ("000008", "SZ"),
        ("920002", "BJ"),
    ]
    out = intervals_from_baidu([_baidu(股票代码=c, 交易所代码=e) for c, e in codes])
    assert [iv.code for iv in out] == ["603159", "000008", "920002"]


# --- 并集合并（五种形态）-------------------------------------------------------------


def test_merge_unions_touching_intervals() -> None:
    """相接（end == next.start）必须合并：`_reject_overlap` 判 `cur.start <= prev.end`，
    首尾相接也算冲突——百度就产得出这种（603159）。"""
    out = merge_intervals(
        [
            Interval("603159", date(2026, 6, 15), date(2026, 6, 16)),
            Interval("603159", date(2026, 6, 16), date(2026, 6, 23)),
        ]
    )
    assert out == (Interval("603159", date(2026, 6, 15), date(2026, 6, 23)),)


def test_merge_unions_overlapping_intervals() -> None:
    """东财 ∩ 百度 同一次停市必然重叠：EM [09-29, 11-10] ∪ 百度 [09-29, 11-10] 合一段。"""
    out = merge_intervals(
        [
            Interval("000851", date(2025, 9, 29), date(2025, 11, 10)),
            Interval("000851", date(2025, 10, 1), date(2025, 10, 8)),
        ]
    )
    assert out == (Interval("000851", date(2025, 9, 29), date(2025, 11, 10)),)


def test_merge_absorbs_fully_contained_intervals() -> None:
    out = merge_intervals(
        [
            Interval("600519", date(2024, 1, 1), date(2024, 1, 31)),
            Interval("600519", date(2024, 1, 10), date(2024, 1, 12)),
        ]
    )
    assert out == (Interval("600519", date(2024, 1, 1), date(2024, 1, 31)),)


def test_merge_keeps_adjacent_but_disjoint_apart() -> None:
    """[1,3] 与 [5,7] 中间隔着 4：不许合并——合并会把没停牌的日子也算成停牌。"""
    out = merge_intervals(
        [
            Interval("600519", date(2024, 1, 1), date(2024, 1, 3)),
            Interval("600519", date(2024, 1, 5), date(2024, 1, 7)),
        ]
    )
    assert out == (
        Interval("600519", date(2024, 1, 1), date(2024, 1, 3)),
        Interval("600519", date(2024, 1, 5), date(2024, 1, 7)),
    )


def test_merge_drops_empty_and_inverted_segments() -> None:
    """空段（end < start）摆不出区间，丢掉；丢的方向是"少豁免"，照旧 fail-closed。"""
    assert merge_intervals([Interval("600519", date(2024, 1, 5), date(2024, 1, 1))]) == ()
    assert merge_intervals([]) == ()


def test_merge_groups_by_code_and_swallows_after_open_ended() -> None:
    """跨代码同日停牌是常态，不互相合并；`end=None`（持续中）吞掉该代码之后的一切。"""
    out = merge_intervals(
        [
            Interval("600519", date(2024, 1, 1)),
            Interval("000001", date(2024, 1, 1), date(2024, 1, 2)),
            Interval("600519", date(2025, 6, 1), date(2025, 6, 30)),
        ]
    )
    assert out == (
        Interval("000001", date(2024, 1, 1), date(2024, 1, 2)),
        Interval("600519", date(2024, 1, 1)),  # 持续中，吞掉 2025 那段
    )


def test_merged_output_loads_into_the_master_without_conflict() -> None:
    """并集合并后的区间进 `SecurityMaster` 不抛——这正是"装载前必须合并"的验收句。"""
    raw = [
        Interval("603159", date(2026, 6, 15), date(2026, 6, 16)),
        Interval("603159", date(2026, 6, 16), date(2026, 6, 23)),
        Interval("603159", date(2026, 6, 15), date(2026, 6, 23)),
    ]
    listing = [Listing("603159", "样本", date(2010, 1, 1))]
    # 不合并就抛：相接也算重叠（`_reject_overlap` 判 `cur.start <= prev.end`）
    with pytest.raises(ValueError, match="区间重叠"):
        SecurityMaster(listing, suspensions=raw)
    ok = SecurityMaster(listing, suspensions=merge_intervals(raw))
    assert ok.suspended_on("603159", date(2026, 6, 16))
    assert ok.suspended_on("603159", date(2026, 6, 23))  # 含头含尾
    assert not ok.suspended_on("603159", date(2026, 6, 24))  # 复牌日当天不算停牌
