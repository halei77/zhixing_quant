"""03 §二 L3：R006 缺失半边的判定完备性——报缺失 ⟺ 该日 ∈ 日历 ∩ 观测窗 ∧ ∉ 观测 ∧ ∉ 登记。

判据**另写一遍**（`_should_flag`），不复用 `calendar_alignment` 的算式：同源自证是这类
测试唯一的失败方式。双向断言比单向"脏数据被拦"强——单向容得下一个"什么都不放行"的门禁。

「任何未登记缺口 100% 被拦」是第二条性质：随机挖掉的每个没登记交易日，都必须让 R006
至少报出一条缺失原因（报文只给计数与首日，所以按"报/不报 + 计数"对）。
"""

from datetime import date, timedelta
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from tests.properties._profile import property_settings
from zhixing_quant.domain.bar import BarDraft
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Interval, Listing, SecurityMaster
from zhixing_quant.quality.facts import BatchFacts
from zhixing_quant.quality.rules import calendar_alignment
from zhixing_quant.sources.akshare.suspend import merge_intervals

PARAMS: dict[str, Any] = {}
CODE = "600519"
#: 一段固定交易日窗（周一~周五），从 2024-01-02（周二）起 20 天——与 R006 单测同一把尺。
_ALL_DAYS = tuple(
    d for d in (date(2024, 1, 2) + timedelta(days=i) for i in range(60)) if d.weekday() < 5
)[:20]
CALENDAR = TradingCalendar(_ALL_DAYS)


def _draft(day: date) -> BarDraft:
    return BarDraft(
        source="prop",
        symbol=CODE,
        trade_date=day,
        open=10.0,
        high=10.5,
        low=9.5,
        close=10.0,
        volume=1000.0,
        amount=10000.0,
        adj_factor=1.0,
    )


def _should_flag(observed: set[date], registered: set[date]) -> list[date]:
    """04 §二 R006 判定原文的独立重写：交易日 ∩ 观测窗 − 观测 − 登记。"""
    if not observed:
        return []
    window = [d for d in _ALL_DAYS if min(observed) <= d <= max(observed)]
    return [d for d in window if d not in observed and d not in registered]


@st.composite
def _scenario(draw: Any) -> tuple[list[date], list[Interval]]:
    """观测集 ⊆ 交易日窗（避开 illegal 半边，本性质只测缺失半边）+ 若干停牌区间。"""
    observed = draw(
        st.lists(st.sampled_from(_ALL_DAYS), min_size=2, max_size=len(_ALL_DAYS), unique=True)
    )
    spans: list[Interval] = []
    for _ in range(draw(st.integers(min_value=0, max_value=4))):
        start_idx = draw(st.integers(0, len(_ALL_DAYS) - 1))
        length = draw(st.integers(0, 5))
        end_idx = min(start_idx + length, len(_ALL_DAYS) - 1)
        spans.append(Interval(CODE, _ALL_DAYS[start_idx], _ALL_DAYS[end_idx]))
    return sorted(observed), spans


def _registered_covering(spans: list[Interval]) -> set[date]:
    out: set[date] = set()
    for day in _ALL_DAYS:
        if any(iv.covers(day) for iv in spans):
            out.add(day)
    return out


@given(_scenario())
@property_settings
def test_r006_reports_missing_exactly_when_a_gap_is_unregistered(
    scenario: tuple[list[date], list[Interval]],
) -> None:
    observed, spans = scenario
    registered = _registered_covering(spans)
    expected = _should_flag(set(observed), registered)
    # 随机生成的区间可能重叠：先并集合并（生产路径同一函数），SecurityMaster 才肯装载
    master = SecurityMaster(
        [Listing(CODE, "属性", date(2000, 1, 4))], suspensions=merge_intervals(spans)
    )
    reasons = calendar_alignment(
        BatchFacts(drafts=tuple(_draft(d) for d in observed), master=master, calendar=CALENDAR),
        PARAMS,
    )
    missing_reasons = [r for r in reasons if "缺" in r]
    if not expected:
        assert missing_reasons == [], (observed, spans, reasons)
    else:
        assert len(missing_reasons) == 1, (observed, spans, reasons)
        assert f"缺 {len(expected)} 个交易日" in missing_reasons[0], (expected, missing_reasons)


@given(_scenario())
@property_settings
def test_any_unregistered_gap_is_always_blocked(
    scenario: tuple[list[date], list[Interval]],
) -> None:
    """反向性质：只要观测窗里存在一个**未登记**的缺口，R006 必报（fail-closed 不许漏）。"""
    observed, spans = scenario
    unregistered = _should_flag(set(observed), _registered_covering(spans))
    master = SecurityMaster(
        [Listing(CODE, "属性", date(2000, 1, 4))], suspensions=merge_intervals(spans)
    )
    reasons = calendar_alignment(
        BatchFacts(drafts=tuple(_draft(d) for d in observed), master=master, calendar=CALENDAR),
        PARAMS,
    )
    if unregistered:
        assert any("缺" in r for r in reasons), (observed, spans, unregistered, reasons)
