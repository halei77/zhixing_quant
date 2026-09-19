"""04 §二 规则目录 v1：R001-R010 每条一个正例、一个反例（03 §三"必须测"第一行）。

阈值在这里显式给出，不读 `config/gate.toml`——谓词逻辑与发布口径是两件事，
配置对不对由 tests/test_gate_config.py 对着 04 §二 逐字核。这里要测的是
"给定这组阈值，判定是否严格照契约"，包括**判不了的时候拒**（fail-closed，04 引言）。

反例不止是"值不对"，还包括"没主数据/没日历/字段缺失"这类无从判定的形状：
把它们放行，日报就会显示"今天一条都没违规"，而这正是 00 宪章第二节说的自欺。
"""

from datetime import date, datetime
from typing import Any

import pytest

from zhixing_quant.domain.bar import BarDraft
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Interval, Listing, SecurityMaster, SecurityState
from zhixing_quant.quality.facts import BatchFacts, RowFacts
from zhixing_quant.quality.rules import (
    adjustment_factor_jump,
    calendar_alignment,
    duplicate_key,
    field_completeness,
    ghost_bar,
    limit_breach,
    nonnegative_volume_price,
    ohlc_legal,
    prev_close_consistency,
    timestamp_monotonic,
)

DAY = date(2024, 1, 3)
WEEK_DAYS = [
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
    date(2024, 1, 9),
    date(2024, 1, 10),
]
CALENDAR = TradingCalendar(WEEK_DAYS)

#: 与 04 §二 同值的测试用阈值（值本身在 test_gate_config.py 里对表核）。
PARAMS: dict[str, Any] = {
    "tolerance_pct": 0.5,
    "limits_pct": {"main": 10.0, "st": 5.0, "gem": 20.0, "star": 20.0, "bse": 30.0},
    "new_listing_no_limit_days": {"main": 5, "gem": 5, "star": 5, "bse": 1},
    "gap_extra_pct": 0.5,
    "jump_ratio": 0.02,
}

CLEAN: dict[str, Any] = {
    "source": "ak",
    "symbol": "600519",
    "trade_date": DAY,
    "open": 10.0,
    "high": 11.0,
    "low": 9.5,
    "close": 10.5,
    "volume": 1000.0,
    "amount": 10500.0,
    "adj_factor": 1.0,
}


def _draft(**over: Any) -> BarDraft:
    return BarDraft.model_validate({**CLEAN, **over})


def _state(code: str = "600519", is_st: bool = False, is_suspended: bool = False) -> SecurityState:
    return SecurityState(
        code=code,
        as_of=DAY,
        name="测试",
        listed_on=date(2000, 1, 4),
        delisted_on=None,
        is_st=is_st,
        is_suspended=is_suspended,
    )


def _row(draft: BarDraft, **over: Any) -> RowFacts:
    facts: dict[str, Any] = {
        "draft": draft,
        "state": None,
        "prev_close": None,
        "prev_factor": None,
        "already_present": False,
        "out_of_order": False,
        "master": None,
        "calendar": None,
    }
    facts.update(over)
    return RowFacts(**facts)


def _batch(*drafts: BarDraft, calendar: TradingCalendar | None = CALENDAR) -> BatchFacts:
    return BatchFacts(drafts=drafts, master=None, calendar=calendar)


def _said(reason: str | None) -> str:
    """谓词"报了话"的断言写法：先证明非 None，再核内容。

    直接写 `assert "越界" in limit_breach(...)` 在 mypy --strict 下过不了（`in` 的右操作数
    可能是 None），而 `assert f(...) is not None` 又把每条反例写成两行。
    """
    assert reason is not None
    return reason


def _master(listed_on: date, code: str = "600519") -> SecurityMaster:
    return SecurityMaster([Listing(code, "测试", listed_on)])


# --- R001 OHLC 合法性 -------------------------------------------------------------


def test_r001_accepts_legal_and_one_price_bar() -> None:
    row = _row(_draft())
    assert ohlc_legal(row, PARAMS) is None
    assert ohlc_legal(_row(_draft(open=10.0, high=10.0, low=10.0, close=10.0)), PARAMS) is None


@pytest.mark.parametrize(
    "over",
    [
        {"high": 8.0},  # high < low
        {"close": 12.0},  # close > high
        {"low": 10.8},  # low > open/close 较低者
        {"close": float("nan")},  # NaN 只在 close：三条比较会全"看起来通过"
        {"high": float("nan")},
    ],
)
def test_r001_rejects_each_inversion(over: dict[str, Any]) -> None:
    assert ohlc_legal(_row(_draft(**over)), PARAMS)


def test_r001_defers_missing_price_to_r010() -> None:
    """字段缺失只归 R010：两级同判会把一条数据记到两个规则名下，日报明细就此失真。"""
    assert ohlc_legal(_row(_draft(high=None)), PARAMS) is None


# --- R002 量价非负 ----------------------------------------------------------------


def test_r002_accepts_positive_prices_and_zero_volume() -> None:
    assert nonnegative_volume_price(_row(_draft()), PARAMS) is None
    assert nonnegative_volume_price(_row(_draft(volume=0.0, amount=0.0)), PARAMS) is None


@pytest.mark.parametrize(
    "over",
    [
        {"open": 0.0},
        {"low": -1.0},
        {"volume": -5.0},
        {"amount": -0.5},
    ],
)
def test_r002_rejects_negative_or_zero(over: dict[str, Any]) -> None:
    assert nonnegative_volume_price(_row(_draft(**over)), PARAMS)


def test_r002_leaves_nan_to_r001() -> None:
    """NaN 不满足 `<= 0`，由 R001 抓；两边都判会变成一条数据两笔账。"""
    assert nonnegative_volume_price(_row(_draft(close=float("nan"))), PARAMS) is None


def test_r002_defers_missing_price_to_r010() -> None:
    """价格没给全时 R002 无话可说：负号与缺失是两件事，别在同一行上记两笔账。"""
    assert nonnegative_volume_price(_row(_draft(volume=None)), PARAMS) is None
    assert nonnegative_volume_price(_row(_draft(high=None)), PARAMS) is None


# --- R003 幽灵K线 -----------------------------------------------------------------


@pytest.mark.parametrize(
    "over",
    [
        {"volume": 1000.0},
        {"volume": 0.0, "is_suspended": True},
    ],
)
def test_r003_accepts_traded_and_marked_suspended(over: dict[str, Any]) -> None:
    assert ghost_bar(_row(_draft(**over)), PARAMS) is None


def test_r003_accepts_zero_volume_suspended_per_master() -> None:
    row = _row(_draft(volume=0.0), state=_state(is_suspended=True))
    assert ghost_bar(row, PARAMS) is None


def test_r003_rejects_zero_volume_without_suspension_mark() -> None:
    row = _row(_draft(volume=0.0), state=_state())
    assert "停牌" in _said(ghost_bar(row, PARAMS))


def test_r003_rejects_when_master_cannot_confirm_suspension() -> None:
    """fail-closed：「当天确实没停牌」这件事本身要主数据才能肯定，缺了就照 WARN 记一笔。"""
    assert ghost_bar(_row(_draft(volume=0.0)), PARAMS)


def test_r003_defers_missing_volume() -> None:
    assert ghost_bar(_row(_draft(volume=None)), PARAMS) is None


# --- R004 涨跌幅越界 --------------------------------------------------------------


def _main_row(prev_close: float, close: float) -> RowFacts:
    return _row(
        _draft(open=prev_close, close=close, high=max(prev_close, close) + 0.1, low=1.0),
        state=_state(),
        prev_close=prev_close,
    )


@pytest.mark.parametrize("close", [10.0, 11.0, 9.0, 10.54])
def test_r004_accepts_moves_within_board_limit_plus_tolerance(close: float) -> None:
    """主板 10% + 容差 0.5%：+10.4% 放行，+11% 拒收（04 §二 R004 列"留 0.5% 容差"）。"""
    assert limit_breach(_main_row(10.0, close), PARAMS) is None


@pytest.mark.parametrize("close", [11.06, 8.94])
def test_r004_rejects_moves_beyond_board_limit(close: float) -> None:
    assert "越界" in _said(limit_breach(_main_row(10.0, close), PARAMS))


def test_r004_uses_st_limit() -> None:
    row = _row(
        _draft(open=10.0, close=10.56, high=10.6, low=9.9),
        state=_state(is_st=True),
        prev_close=10.0,
    )
    assert "越界" in _said(limit_breach(row, PARAMS))
    not_st = _row(_draft(open=10.0, close=10.56, high=10.6, low=9.9), prev_close=10.0)
    # 无 ST 标记来源时按"判不了"拒收（fail-closed），而不是按最宽的创业板档放行
    assert "无法判定" in _said(limit_breach(not_st, PARAMS))


@pytest.mark.parametrize(
    ("symbol", "prev", "close"),
    [("300750", 10.0, 12.0), ("688111", 10.0, 12.0), ("920002", 10.0, 13.0)],
)
def test_r004_honours_wider_limits_per_board(symbol: str, prev: float, close: float) -> None:
    """创业板/科创板 20%、北交所 30%：按主板阈值判会把整批成长股拒收。"""
    row = _row(
        _draft(symbol=symbol, open=prev, close=close, high=close + 0.1, low=1.0),
        state=_state(code=symbol),
        prev_close=prev,
    )
    assert limit_breach(row, PARAMS) is None


@pytest.mark.parametrize(
    ("symbol", "prev", "close"),
    [("300750", 10.0, 12.11), ("688111", 10.0, 7.89), ("920002", 10.0, 13.11)],
)
def test_r004_rejects_beyond_each_board(symbol: str, prev: float, close: float) -> None:
    row = _row(
        _draft(symbol=symbol, open=prev, close=close, high=close + 0.1, low=1.0),
        state=_state(code=symbol),
        prev_close=prev,
    )
    assert limit_breach(row, PARAMS)


def test_r004_rejects_unknown_code_prefix() -> None:
    """认不出板块 = 不知道上限 = 判不了，拒收而不是按最宽档放行。"""
    row = _row(_draft(symbol="123456", open=10.0, close=99.0, high=100.0, low=1.0), prev_close=10.0)
    assert "无法判定" in _said(limit_breach(row, PARAMS))


def test_r004_skips_first_rows_without_previous_close() -> None:
    """首日没有昨收可比：那是 R010/覆盖率的议题，不是涨跌幅违规。"""
    assert limit_breach(_row(_draft(), state=_state()), PARAMS) is None


def test_r004_exempts_new_listing_window() -> None:
    """04 §二 豁免：注册制新股前 5 个交易日不设涨跌幅。上市后第 3 个交易日涨 30% 合法。"""
    master = _master(date(2024, 1, 8))
    row = _row(
        _draft(trade_date=date(2024, 1, 10), open=10.0, close=13.0, high=13.1, low=9.9),
        state=_state(),
        prev_close=10.0,
        master=master,
        calendar=CALENDAR,
    )
    assert limit_breach(row, PARAMS) is None


def test_r004_stops_exempting_after_the_window() -> None:
    """同一条数据，上市日换成上周 → 已过 5 个交易日，30% 就是越界。"""
    master = _master(date(2024, 1, 2))
    row = _row(
        _draft(trade_date=date(2024, 1, 10), open=10.0, close=13.0, high=13.1, low=9.9),
        state=_state(),
        prev_close=10.0,
        master=master,
        calendar=CALENDAR,
    )
    assert "越界" in _said(limit_breach(row, PARAMS))


def test_r004_survives_master_missing_code_instead_of_crashing() -> None:
    """北交所票、主数据里没有它：不能抛 KeyError 炸掉整批，要落成一条拒收。"""
    row = _row(
        _draft(symbol="920002", open=10.0, close=13.2, high=13.3, low=9.9),
        state=_state(code="920002"),
        prev_close=10.0,
        master=_master(date(2024, 1, 2), code="600519"),
        calendar=CALENDAR,
    )
    assert "越界" in _said(limit_breach(row, PARAMS))


# --- R005 复权因子突变 ------------------------------------------------------------


def test_r005_accepts_stable_and_small_moves() -> None:
    assert adjustment_factor_jump(_row(_draft(), prev_factor=1.0), PARAMS) is None
    row = _row(_draft(adj_factor=1.01), prev_factor=1.0)
    assert adjustment_factor_jump(row, PARAMS) is None


def test_r005_warns_on_factor_jump() -> None:
    row = _row(_draft(adj_factor=1.2), prev_factor=1.0)
    assert "复权因子" in _said(adjustment_factor_jump(row, PARAMS))


@pytest.mark.parametrize(
    ("draft_over", "prev"),
    [({"adj_factor": None}, 1.0), ({"adj_factor": 1.2}, None), ({"adj_factor": 1.2}, 0.0)],
)
def test_r005_skips_when_there_is_nothing_to_compare(
    draft_over: dict[str, Any], prev: float | None
) -> None:
    assert adjustment_factor_jump(_row(_draft(**draft_over), prev_factor=prev), PARAMS) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_r005_says_it_cannot_judge_a_broken_factor(bad: float) -> None:
    """因子是 NaN/无穷时报"判不了"，而不是沉默。

    沉默是最坏的：NaN 恰恰是 CSV 空值最常见的形状，而 R005 是 WARN 级待核清单——
    漏报等于"这批因子没人看过"，日报上却与"看过且没问题"长得一模一样。
    """
    today = _row(_draft(adj_factor=bad), prev_factor=1.0)
    yesterday = _row(_draft(adj_factor=1.0), prev_factor=bad)
    assert "无法判定" in _said(adjustment_factor_jump(today, PARAMS))
    assert "无法判定" in _said(adjustment_factor_jump(yesterday, PARAMS))


# --- R006 交易日历对齐（整批 FATAL）------------------------------------------------


def test_r006_accepts_a_contiguous_trading_window() -> None:
    drafts = [_draft(trade_date=d) for d in WEEK_DAYS[:4]]
    assert calendar_alignment(_batch(*drafts), PARAMS) == ()


def test_r006_is_fatal_without_a_calendar() -> None:
    batch = _batch(_draft(), calendar=None)
    assert "日历" in calendar_alignment(batch, PARAMS)[0]


def test_r006_rejects_non_trading_dates() -> None:
    batch = _batch(_draft(trade_date=date(2024, 1, 6)))  # 周六
    reasons = calendar_alignment(batch, PARAMS)
    assert any("不是交易日" in r for r in reasons)


def test_r006_flags_missing_trading_days_inside_the_window() -> None:
    """观测区间中间断了：04 §二 R006 的"交易日缺失无说明"半边。"""
    drafts = [_draft(trade_date=d) for d in (WEEK_DAYS[0], WEEK_DAYS[2])]
    reasons = calendar_alignment(_batch(*drafts), PARAMS)
    assert any("缺 1 个交易日" in r for r in reasons)


def test_r006_ignores_days_beyond_the_observed_window() -> None:
    """窗口之外不算缺：否则任何增量抓取都会报一屏假缺失（calendar.missing_days 的口径）。"""
    drafts = [_draft(trade_date=d) for d in WEEK_DAYS[:3]]
    assert calendar_alignment(_batch(*drafts), PARAMS) == ()


def test_r006_rejects_a_batch_with_no_dates() -> None:
    assert calendar_alignment(_batch(_draft(trade_date=None)), PARAMS)


# --- R007 昨收一致性 --------------------------------------------------------------


def _gap_row(
    symbol: str,
    prev_close: float,
    open_: float,
    draft_over: dict[str, Any] | None = None,
    **facts_over: Any,
) -> RowFacts:
    draft = _draft(
        symbol=symbol,
        open=open_,
        high=max(open_, prev_close) + 0.1,
        low=1.0,
        close=open_,
        **(draft_over or {}),
    )
    return _row(draft, prev_close=prev_close, **facts_over)


def test_r007_accepts_a_normal_gap() -> None:
    row = _gap_row("600519", 10.0, 10.5, state=_state(code="600519"))
    assert prev_close_consistency(row, PARAMS) is None


def test_r007_threshold_is_the_board_limit_linked() -> None:
    """主板 10+0.5+0.5=11%（04 §二 原文的 11%），创业板同一规则放行 20% 跳空。"""
    main = _gap_row("600519", 10.0, 11.09, state=_state(code="600519"))
    breach = _gap_row("600519", 10.0, 11.2, state=_state(code="600519"))
    gem = _gap_row("300750", 10.0, 12.05, state=_state(code="300750"))
    assert prev_close_consistency(main, PARAMS) is None
    assert prev_close_consistency(breach, PARAMS)
    assert prev_close_consistency(gem, PARAMS) is None


def test_r007_exempts_ex_div_day() -> None:
    """复权因子变了 → 跳空有除权除息可解释（04 §二 R007）。"""
    row = _gap_row(
        "600519",
        10.0,
        12.0,
        draft_over={"adj_factor": 1.5},
        state=_state(),
        prev_factor=1.0,
    )
    assert prev_close_consistency(row, PARAMS) is None


def test_r007_exempts_resumption_day() -> None:
    """长期停牌后复牌首日的合理跳空：前一交易日在主数据里是停牌。"""
    master = SecurityMaster(
        [Listing("600519", "测试", date(2000, 1, 4))],
        suspensions=[Interval("600519", date(2024, 1, 2), date(2024, 1, 2))],
    )
    row = _gap_row("600519", 10.0, 12.0, state=_state(), master=master, calendar=CALENDAR)
    assert prev_close_consistency(row, PARAMS) is None


def test_r007_rejects_an_unexplained_gap() -> None:
    """主数据在、日历在、前一日没停牌、因子没变——四条都排除掉才落到拒收。"""
    row = _gap_row(
        "600519",
        10.0,
        12.0,
        draft_over={"adj_factor": 1.5},
        state=_state(),
        prev_factor=1.5,
        master=_master(date(2000, 1, 4)),
        calendar=CALENDAR,
    )
    assert "无除权/复牌可解释" in _said(prev_close_consistency(row, PARAMS))


def test_r007_rejects_without_master_data() -> None:
    """没主数据就不知道板块上限，也就无权说这个跳空合法。"""
    assert "无法判定" in _said(prev_close_consistency(_gap_row("600519", 10.0, 12.0), PARAMS))


def test_r007_skips_rows_without_previous_close() -> None:
    row = _row(_draft(), state=_state())
    assert prev_close_consistency(row, PARAMS) is None


# --- R008 / R009 顺序与重复 --------------------------------------------------------


def test_r008_rejects_only_the_later_duplicate() -> None:
    assert duplicate_key(_row(_draft(), already_present=True), PARAMS)
    assert duplicate_key(_row(_draft()), PARAMS) is None


def test_r008_names_the_bar_time_when_there_is_one() -> None:
    """拒收一条分钟K线时只报日期，等于什么都没说：那一天有 48 根。

    两种粒度的键都在这一条消息里（ADR-0009 决定 3）：日线不带时刻，读起来还是 `(symbol, date)`。
    """
    minute = _draft(ts=datetime(2024, 1, 3, 9, 35))
    assert _said(duplicate_key(_row(minute, already_present=True), PARAMS)) == (
        "同一 600519@2024-01-03 09:35:00 重复入库，拒后到的一条"
    )
    assert _said(duplicate_key(_row(_draft(), already_present=True), PARAMS)) == (
        "同一 600519@2024-01-03 重复入库，拒后到的一条"
    )


def test_r009_predicate_is_independent_of_the_grain_it_runs_on() -> None:
    """谓词不认识粒度，启用集才认识（ADR-0009 决定 5）：这里直接轰函数本身。

    日线批不跑它由 `config/gate.toml` 的 `grains` 保证（test_gate_config.py 钉），把这条判据
    重复写进谓词里就会有两处真值——那时"分钟线要不要跑 R009"就取决于谁先改。
    """
    assert timestamp_monotonic(_row(_draft(), out_of_order=True), PARAMS)
    assert timestamp_monotonic(_row(_draft()), PARAMS) is None
    assert timestamp_monotonic(
        _row(_draft(ts=datetime(2024, 1, 3, 9, 35)), out_of_order=True), PARAMS
    )


# --- R010 字段完备性（整批 FATAL）---------------------------------------------------


def test_r010_accepts_complete_rows() -> None:
    assert field_completeness(_batch(_draft(), _draft(symbol="000001")), PARAMS) == ()


def test_r010_names_the_offending_row_and_fields() -> None:
    reasons = field_completeness(_batch(_draft(), _draft(volume=None, close=None)), PARAMS)
    assert len(reasons) == 1
    assert "1 行" in reasons[0]
    assert "close" in reasons[0] and "volume" in reasons[0]


def test_r010_counts_every_offender_but_shows_a_few() -> None:
    """整批级规则最容易刷爆日报：给总数、只展样，04 §四 的"明细 Top10"同口径。"""
    drafts = [_draft(symbol=f"60000{i}", amount=None) for i in range(5)]
    reasons = field_completeness(_batch(*drafts), PARAMS)
    assert "5 行" in reasons[0]
    assert reasons[0].count("amount") == 3


def test_r010_flags_source_and_adj_factor_as_optional() -> None:
    """source/adj_factor 不在 04 §二 R010 的必需字段里：缺它们不该整批拒收。"""
    assert field_completeness(_batch(_draft(adj_factor=None, source="")), PARAMS) == ()
