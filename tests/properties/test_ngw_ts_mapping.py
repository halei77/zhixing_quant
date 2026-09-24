"""03 §二 L3：ngw `times` → `ts` 映射的性质轰炸——同日、单调、无午休越界、单射。

判据**不复用**适配器实现（同源自证是这类测试唯一的失败方式）：性质直接对着「映射应该
是什么」写——输出必须与输入同属一个交易日、保持输入顺序严格递增、全部落在两个交易段的
闭区间内、且两根不同标签绝不撞出同一个 ts。最后一条钉的是 +1min 重标那类错误
（11:29+1 与钉住的 11:30 撞、14:59+1 与 15:00 撞）——实测标签含 11:30 与 15:00。

生成器故意混入午休与盘外标签：性质要对**任意**输入成立，不是只对源今天会给的输入成立。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from datetime import time as time_of
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from tests.properties._profile import property_settings
from zhixing_quant.sources.ngw import minute

#: 实测会话段（闭区间）：上午 09:30–11:30、下午 13:01–15:00（2026-09-24 实测 241 根/日）。
_SEGMENTS = ((time_of(9, 30), time_of(11, 30)), (time_of(13, 1), time_of(15, 0)))

DAY = date(2026, 9, 24)  # 周三，完整交易日


def _label(moment: datetime) -> str:
    return moment.strftime("%Y%m%d%H%M%S")


def _in_session(moment: time_of) -> bool:
    return any(start <= moment <= end for start, end in _SEGMENTS)


def _minutes(start: datetime, count: int) -> list[str]:
    return [_label(start + timedelta(minutes=i)) for i in range(count)]


@st.composite
def _times_sequences(draw: Any) -> list[str]:
    """升序的合法标签（任取子集）+ 随机散落的午休/盘外标签。"""
    legal_pool = _minutes(datetime(2026, 9, 24, 9, 30), 121)  # 09:30–11:30
    legal_pool += _minutes(datetime(2026, 9, 24, 13, 1), 120)  # 13:01–15:00
    legal = sorted(draw(st.lists(st.sampled_from(legal_pool), min_size=2, unique=True)))
    illegal_pool = [
        *_minutes(datetime(2026, 9, 24, 11, 31), 89),  # 11:31–12:59 午休
        _label(datetime(2026, 9, 24, 13, 0)),  # 实测不存在的 13:00
        _label(datetime(2026, 9, 24, 9, 0)),
        _label(datetime(2026, 9, 24, 15, 1)),
        _label(datetime(2026, 9, 24, 8, 59)),
    ]
    illegal = draw(st.lists(st.sampled_from(illegal_pool), max_size=6, unique=True))
    return _interleave(legal, illegal, draw)


def _interleave(legal: list[str], illegal: list[str], draw: Any) -> list[str]:
    """把非法标签随机撒进升序合法序列（输出顺序 = 混排顺序）。"""
    merged = list(legal)
    for item in illegal:
        merged.insert(draw(st.integers(0, len(merged))), item)
    return merged


def _row(times: str) -> dict[str, Any]:
    return {
        "times": times,
        "openp": "100",
        "highp": "101",
        "lowp": "99",
        "nowv": "100",
        "curvol": "10",
        "curvalue": "1000",
    }


@given(_times_sequences())
@property_settings
def test_ts_mapping_is_same_day_ordered_session_bound_and_injective(times: list[str]) -> None:
    drafts = minute.minute_drafts([_row(t) for t in times], symbol="600519")
    stamps = [draft.ts for draft in drafts]

    # 同一交易日：日期从标签来，一天的标签只许产出这一天的 ts。
    assert all(draft.trade_date == DAY for draft in drafts)
    assert all(stamp is not None and stamp.date() == DAY for stamp in stamps)

    # 单调：输出保持输入顺序（丢行只许整行丢，不许重排——R009 判的是源交来的顺序）。
    kept = [t for t in times if _in_session(datetime.strptime(t, "%Y%m%d%H%M%S").time())]
    assert [stamp.strftime("%Y%m%d%H%M%S") for stamp in stamps if stamp] == kept

    # 无午休越界 / 盘外：映射不制造交易段之外的 ts。
    for stamp in stamps:
        assert stamp is not None
        moment = stamp.time()
        assert any(start <= moment <= end for start, end in _SEGMENTS), stamp

    # 单射：不同标签 → 不同 ts（+1min 重标在 11:30/15:00 两处会撞的正是这条）。
    assert len(set(stamps)) == len(stamps)

    # 合法标签一根不丢、非法标签一根不留。
    legal_count = sum(1 for t in times if _in_session(datetime.strptime(t, "%Y%m%d%H%M%S").time()))
    assert len(drafts) == legal_count

    # 单调：升序输入必出升序输出（客户端已把源的倒序契约归一，适配器保序——两者合起来
    # 才是门禁看到的形态）。
    ordered = minute.minute_drafts([_row(t) for t in sorted(times)], symbol="600519")
    ordered_stamps = [draft.ts for draft in ordered]
    assert all(s is not None for s in ordered_stamps)
    assert ordered_stamps == sorted(s for s in ordered_stamps if s is not None)
