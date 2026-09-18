"""04 §二 规则目录 v1 的谓词实现。

统一签名：`(facts, params) -> str | None`（逐条）或 `(facts, params) -> Sequence[str]`
（整批）。返回原因文本或不返回；**级别由配置决定，不在这里写死**——同一条判定，akshare
当 REJECT、备用源可能只当 WARN，级别是源级口径，谓词只管"这个形状对不对"。

引擎对每条规则都传同样的两个参数，所以用不上阈值的谓词也要收下第二个——写成 `_params`，
读出时就知道"这是协议占位，不是漏读了配置"。

判不了就拒（fail-closed）：主数据缺该股、日历没装载，这些情况下没有任何数字能证明
数据是好的。放行会让"门禁报告一页干净"和"其实没判"长得一模一样，而上一版项目的死因
正是前者骗过了人（00 宪章第二节）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from zhixing_quant.domain.bar import BarDraft, nonpositive_prices, not_finite, ohlc_violations
from zhixing_quant.domain.symbol import Board, UnknownCode, board_of
from zhixing_quant.quality.facts import BatchFacts, RowFacts
from zhixing_quant.quality.gate_config import RuleParams


def _prices(draft: BarDraft) -> tuple[float, float, float, float] | None:
    """四价齐全才有的可判形状；缺失返回 None，交由 R010 定级。"""
    open_, high, low, close = draft.open, draft.high, draft.low, draft.close
    if open_ is None or high is None or low is None or close is None:
        return None
    return open_, high, low, close


def _unusable(*values: float | None) -> bool:
    """值在、但不是个可用的数（NaN 或 ±inf）。判法与 domain.bar 共用，两边不会漂。"""
    return any(v is not None and not_finite(v) for v in values)


def ohlc_legal(facts: RowFacts, _params: RuleParams) -> str | None:
    """R001 OHLC 合法性。缺失不判：那是 R010 的活，两级同判会把一条数据记两次。"""
    prices = _prices(facts.draft)
    if prices is None:
        return None
    if bad := ohlc_violations(*prices):
        return "；".join(bad)
    return None


def nonnegative_volume_price(facts: RowFacts, _params: RuleParams) -> str | None:
    """R002 量价非负。

    非有限的量按违规判：`NaN < 0` 与 `inf < 0` 都不成立，但"非负"这件事对它们同样无从
    证明。干净区契约 `volume/amount = Field(ge=0, allow_inf_nan=False)` 也拒它们，这条
    不判就会撞上 `GateInconsistency`——而 NaN 恰恰是 CSV 空值最常见的形状。
    """
    draft = facts.draft
    prices = _prices(draft)
    if prices is None:
        return None
    if _unusable(*prices):
        return None  # 非有限价格归 R001：两条都判会让一行数据记两笔账
    if nonpositive_prices(*prices):
        return "价格 ≤ 0"
    for name in ("volume", "amount"):
        value = getattr(draft, name)
        if value is None:
            continue
        if _unusable(value):
            return f"{name} 非有限值（NaN/±inf）"
        if value < 0:
            return f"{name} < 0"
    return None


def ghost_bar(facts: RowFacts, _params: RuleParams) -> str | None:
    """R003 幽灵K线：零成交但当天没有停牌标记。

    主数据查不到该股时按违规处理——"没有停牌标记"这件事本身就需要主数据才能肯定。
    级别是 WARN，宁可多列进日报待核，不可静默放行。
    """
    draft = facts.draft
    if draft.volume != 0 or draft.is_suspended:
        return None
    if facts.state is not None and facts.state.is_suspended:
        return None
    return "volume=0 且无停牌标记"


def _limit_and_board(facts: RowFacts, params: RuleParams) -> tuple[float, Board] | None:
    """该票该日的涨跌幅上限（百分点，未加容差）与所属板块。判不出来返回 None。

    R004 与 R007 共用它，这就是 04 §二 "R007 阈值与 R004 板块阈值联动"的落点：一条
    代码路径、一份 `limits_pct`，不存在"改了 R004 忘了改 R007"的可能。

    主数据缺席时连北交所也不给上限：早先的版本先返回 bse 档位再问主数据，于是"北交所票、
    主数据没有它"会一路走到新股窗口那次 `listing()` 查询并抛 KeyError——整批跑批被一条
    数据炸掉。判不了就是判不了，级别照常是 REJECT，但引擎不该替它崩溃。
    """
    try:
        board = board_of(facts.draft.symbol)
    except UnknownCode:
        return None
    if facts.state is None:
        return None  # ST 标记与新股窗口都要查主数据，缺了就判不了
    limits: Mapping[str, float] = params["limits_pct"]
    if board is Board.BSE:
        return limits["bse"], board
    return (limits["st"] if facts.state.is_st else limits[board.value]), board


def limit_breach(facts: RowFacts, params: RuleParams) -> str | None:
    """R004 涨跌幅越界（含 04 §二 的新股豁免）。"""
    draft = facts.draft
    judged = _limit_and_board(facts, params)
    if judged is None:
        # 先问"这是哪只票、归哪个板块"，再问"有没有昨收可比"。顺序反过来时，一条
        # 认不出板块的数据在"当天第一行"上会两问都过关，被门禁放行、被 Bar 的 symbol
        # 校验拒收——同一行数据两套口径，正是 GateInconsistency 要抓的那种漂移。
        return "主数据或板块缺失，涨跌幅无法判定"
    prices = _prices(draft)
    if prices is None or facts.prev_close is None or facts.prev_close <= 0:
        return None
    limit, board = judged
    if _in_new_listing_window(facts, board, params):
        return None
    change = (prices[3] - facts.prev_close) / facts.prev_close * 100.0
    tolerance = float(params["tolerance_pct"])
    if abs(change) > limit + tolerance:
        return f"涨跌幅 {change:+.2f}% 越界（上限 {limit}%，容差 {tolerance}%）"
    return None


def _in_new_listing_window(facts: RowFacts, board: Board, params: RuleParams) -> bool:
    """04 §二 的新股无涨跌幅限制期。豁免条目仍进日报待核（同节末句）。

    天数按板块取，不在这里写死 5：北交所是"首日不设、此后 30%"，与注册制主板不同口径。
    """
    draft = facts.draft
    if facts.master is None or facts.calendar is None or draft.trade_date is None:
        return False
    days = int(params["new_listing_no_limit_days"].get(board.value, 0))
    try:
        return facts.master.no_limit_period(draft.symbol, draft.trade_date, facts.calendar, days)
    except KeyError:
        # 主数据没这只票 → 无法证明它在豁免窗口里，按"不在窗口"处理，让上层照常拒收。
        # 在这里抛出去会让一条缺主数据的票炸掉整批。
        return False


def adjustment_factor_jump(facts: RowFacts, params: RuleParams) -> str | None:
    """R005 复权因子突变。

    04 §二 原判定是"因子变化但无分红送转公告对应"——公告数据要到 Step 2 才有源可接，
    现在只能报"变了多少"，把核对留给人。级别配 WARN 正是为此：它不是结论，是待核清单。
    """
    current = facts.draft.adj_factor
    if _unusable(current, facts.prev_factor):
        return "复权因子含 NaN/无穷，无法判定是否突变"
    if current is None or facts.prev_factor is None or facts.prev_factor <= 0:
        return None
    ratio = abs(current - facts.prev_factor) / facts.prev_factor
    if ratio > float(params["jump_ratio"]):
        return f"复权因子 {facts.prev_factor}→{current}，单日变化 {ratio:.2%}"
    return None


def calendar_alignment(facts: BatchFacts, _params: RuleParams) -> Sequence[str]:
    """R006 交易日历对齐（整批 FATAL）。"""
    if facts.calendar is None:
        return ("未装载交易日历，无法判定日期有效性",)
    observed = [d.trade_date for d in facts.drafts if d.trade_date is not None]
    if not observed:
        return ("本批无有效日期",)
    out: list[str] = []
    illegal = sorted({d for d in observed if not facts.calendar.is_trading_day(d)})
    if illegal:
        out.append(f"{len(illegal)} 个数据日期不是交易日，如 {illegal[0]}")
    missing = facts.calendar.missing_days(observed)
    if missing:
        out.append(
            f"观测窗口内缺 {len(missing)} 个交易日，如 {missing[0]}"
            "（已知停市需由调用方登记，否则按缺失计）"
        )
    return tuple(out)


def _ex_div_applied(facts: RowFacts) -> bool:
    """本行与上一行复权因子不同 → 期间有除权除息事件，价格跳空有解释。"""
    current = facts.draft.adj_factor
    return current is not None and facts.prev_factor is not None and current != facts.prev_factor


def _resuming_after_suspension(facts: RowFacts) -> bool:
    """前一交易日停牌、今天有数据 → 复牌首日，跳空合理（04 §二 R007 豁免）。"""
    draft = facts.draft
    if facts.master is None or facts.calendar is None or draft.trade_date is None:
        return False
    prev_day = facts.calendar.prev_trading_day(draft.trade_date)
    return prev_day is not None and facts.master.suspended_on(draft.symbol, prev_day)


def prev_close_consistency(facts: RowFacts, params: RuleParams) -> str | None:
    """R007 昨收一致性：与昨收偏差过大且无除权除息、且不是复牌首日。

    阈值与 R004 板块上限联动（04 §二），不是写死的 11%：创业板 20% 的合法跳空
    用 flat 阈值判会把整批成长股拒收。`gap_extra_pct` 是 R007 相对 R004 的加宽——
    这里量的是"今开 vs 昨收"，开盘价可以合法地比收盘价更贴近板边。
    """
    draft = facts.draft
    prices = _prices(draft)
    if prices is None or facts.prev_close is None or facts.prev_close <= 0:
        return None
    gap = abs(prices[0] - facts.prev_close) / facts.prev_close * 100.0
    judged = _limit_and_board(facts, params)
    if judged is None:
        return "主数据或板块缺失，昨收偏差无法判定"
    threshold = judged[0] + float(params["tolerance_pct"]) + float(params["gap_extra_pct"])
    if gap <= threshold:
        return None
    if _ex_div_applied(facts):
        return None
    if _resuming_after_suspension(facts):
        return None
    return f"今日开盘与昨收偏差 {gap:.2f}% > {threshold:.2f}%，且无除权/复牌可解释"


def duplicate_key(facts: RowFacts, _params: RuleParams) -> str | None:
    """R008 重复数据：(symbol, date) 已出现过，拒后到的。"""
    if facts.already_present:
        return "同一 (symbol, trade_date) 重复入库，拒后到的一条"
    return None


def timestamp_monotonic(facts: RowFacts, _params: RuleParams) -> str | None:
    """R009 时间戳单调：本行打乱了同票既有顺序。日线阶段按 04 §二 暂不启用。"""
    if facts.out_of_order:
        return "时间戳相对同票前一行乱序"
    return None


def field_completeness(facts: BatchFacts, _params: RuleParams) -> Sequence[str]:
    """R010 字段完备性：任一必需字段缺失即整批拒收。"""
    offenders = [
        f"{d.symbol}@{d.trade_date} 缺 {','.join(d.missing_fields())}"
        for d in facts.drafts
        if d.missing_fields()
    ]
    if not offenders:
        return ()
    shown = "；".join(offenders[:3])
    return (f"{len(offenders)} 行字段不完备：{shown}",)
