"""事件驱动的分钟K回测引擎（ADR-0010 决定 2、决定 3、决定 7）。

一K线一判、不追单、不回头。整个模块只依赖 `domain/*` 与 `backtest` 自己的数据结构：
读盘、主数据、涨跌幅档位都由调用方注入（`bands` 就是一个查表函数）。这不是架构洁癖——
03-4.1 的"跑 3 次逐位相等"、03-4.2 的"截断一致性"和 L1 的属性轰炸都要求引擎能在不碰磁盘
的前提下跑起来，而任何一处 `import storage` 都会让这些测试退化成集成测试。

每根K线内的事件顺序是**定死的**（决定 2）：结算挂单 → 更新净值 → 问策略。反过来写成
"先问策略再成交"，策略就会在同一根里既看见这根的收盘、又成交在这根的开盘——那是把时间
倒流当常态，回测里最难查的那种错。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from zhixing_quant.backtest.fills import (
    REASON_NO_BAR,
    Fill,
    Order,
    Outcome,
    Reject,
    attempt,
)
from zhixing_quant.backtest.limits import PriceBand
from zhixing_quant.backtest.position import (
    REASON_T1,
    Closing,
    CodeBook,
    DayEvent,
    Ledger,
    RoundTrip,
)
from zhixing_quant.backtest.spec import Assumptions, Cost, Side
from zhixing_quant.domain.bar import Bar, Stamp, stamp_of

#: 板从哪儿来：`(代码, 交易日) → 那天的涨跌幅边界`。判不出也要给（`PriceBand.unknown`），
#: 引擎会按"不成交"处理并把原因记成 `band_unknown`——缺主数据不许冒充"没有板"。
BandLookup = Callable[[str, date], PriceBand]


def say(stamp: Stamp) -> str:
    """把 `(日期, 时刻)` 写成给人看的一句话。日线的时刻是 `datetime.min`，那就只报日期。"""
    when_day, when_ts = stamp
    return f"{when_day} {when_ts:%H:%M}" if when_ts != _MIN else f"{when_day}"


_MIN = stamp_of(None, None)[1]


@dataclass(frozen=True)
class Signal:
    """策略的一个动作。**不带代码**：策略每次只被问一只票，答的就是这只票；让它填代码就多出
    一种"把 A 的信号打在 B 上"的错法（决定 7）。
    """

    side: Side
    lots: int
    note: str = ""

    def __post_init__(self) -> None:
        if self.lots <= 0:
            raise ValueError(f"信号要下 {self.lots} 手：0 手或负数的手不是动作，是没做")


@dataclass(frozen=True)
class View:
    """策略的可见世界：当前根及以前。没有成交价、没有未来的根、没有别的票。

    当前根**在**里面——`ts` 是收盘时刻，这根已经走完了，看见它不算穿越。截断一致性
    （03-4.2）测的正是"外面那些根进不来"。
    """

    code: str
    stamp: Stamp
    bar: Bar
    bars: Sequence[Bar]
    holding: int
    sellable: int
    lot_size: int
    index: int

    @property
    def lots_held(self) -> int:
        """持仓合几手。策略想知道"我还剩几手能 T"，不必自己记台账。"""
        return self.holding // self.lot_size

    @property
    def lots_sellable(self) -> int:
        return self.sellable // self.lot_size


@dataclass(frozen=True)
class EquityPoint:
    """某一刻某只票的账户快照。

    按票逐个记而不是按时刻合并：合并要求"所有票在这一刻都有行"，而停牌与断档恰恰让这件事
    不成立（ADR-0009 决定 6）。合并那一半留给报告，它手上有一整张日历。
    """

    stamp: Stamp
    code: str
    cash: float
    held: int
    market_value: float
    pnl: float


@dataclass(frozen=True)
class Result:
    """一次回测的全部事实。指标、报告、台账登记只读它，不许再算一遍任何一笔。"""

    assumptions: Assumptions
    fills: tuple[Fill, ...]
    rejects: tuple[Reject, ...]
    round_trips: tuple[RoundTrip, ...]
    day_events: tuple[DayEvent, ...]
    equity: tuple[EquityPoint, ...]
    closings: tuple[Closing, ...]

    @property
    def rejects_by_reason(self) -> dict[str, int]:
        """拒单按原因计数。没出现过的那一类**不在表里**，而不是占一行 0：这里造一张五档全
        零的表，是把"每一类都查过"冒充成事实。而缺席确实等于 0——每一笔委托要么成交要么拒，
        两条路都在 `run` 里走完，不存在"跳过没判"这一档。报告要印全五档就自己补零。
        """
        out: dict[str, int] = {}
        for reject in self.rejects:
            out[reject.reason] = out.get(reject.reason, 0) + 1
        return out


class Strategy(Protocol):
    """策略接口（决定 7）：只出信号，绝不下单——成交价、能不能成交都不归它管。"""

    name: str

    # 形参写成只按位置传：协议里给出名字等于承诺"可以 signals(view=...)"，而调用方一律
    # 位置传参，写死名字只会逼着实现方用同一个词（桩实现那个参数根本不用）。
    def signals(self, view: View, /) -> Signal | None: ...


class NeverSignal:
    """什么都不做的策略。

    Step 6 不写任何真策略（候选池是 Step 7 的事），但引擎得能跑起来、净值曲线得能画、
    "零回合"得能被报告说成"无回合"而不是 0.0%——这个桩就是给那些测试用的。
    """

    name: str = "never"

    def signals(self, _view: View, /) -> Signal | None:
        return None


def _settle(
    order: Order,
    *,
    bar: Bar,
    book: CodeBook,
    band: PriceBand,
    cost: Cost,
    participation: float,
) -> Outcome:
    """把一笔委托落在这一根上。T+1 判在 `attempt` 之前：一笔账户层面根本不成立的委托，
    不必去问市场接不接，而两个原因同时成立时报告里只能有一个数（决定 4 的判序）。
    """
    if order.side == "sell" and order.shares > book.sellable:
        return Reject(
            order=order,
            reason=REASON_T1,
            detail=(
                f"{order.code} 要卖 {order.shares} 股，今天只剩 {book.sellable} 股可卖："
                "T+1 之下当日买入的不能当日卖（ADR-0010 决定 5）"
            ),
        )
    return attempt(order, bar, band, cost=cost, max_participation_pct=participation)


def run(
    bars: Sequence[Bar],
    *,
    strategy: Strategy,
    assumptions: Assumptions,
    bands: BandLookup,
) -> Result:
    """跑一遍。纯函数：同样的 `bars` 加同样的假设，结果逐位相同（03-4.1）。"""
    sizing = assumptions.sizing
    cost = assumptions.cost
    delay = assumptions.execution.delay_bars
    participation = assumptions.execution.max_participation_pct
    ledger = Ledger(sizing=sizing)
    fills: list[Fill] = []
    rejects: list[Reject] = []
    trips: list[RoundTrip] = []
    events: list[DayEvent] = []
    equity: list[EquityPoint] = []
    history: dict[str, list[Bar]] = {}
    pending: dict[str, list[tuple[int, Order]]] = {}
    day: date | None = None

    for bar in sorted(bars, key=lambda b: (*stamp_of(b.trade_date, b.ts), b.code)):
        if day is not None and bar.trade_date != day:
            # 跨日：先把上一天的账结掉（T飞/加仓按"当日收盘仍未还原"数，决定 6），
            # 再把每只票的可卖额度翻成"今日期初"。晚到的票不在上一天里，`close_day` 对
            # 没动过的账本只会把期初持仓抄成当前持仓，不改任何一笔钱。
            events.extend(ledger.close_day(day))
        day = bar.trade_date
        code = bar.code
        book = ledger.open(bar)
        stamp = stamp_of(bar.trade_date, bar.ts)
        past = history.setdefault(code, [])
        index = len(past)
        past.append(bar)

        due = pending.setdefault(code, [])
        for target, order in [pair for pair in due if pair[0] == index]:
            due.remove((target, order))
            outcome = _settle(
                order,
                bar=bar,
                book=book,
                band=bands(code, bar.trade_date),
                cost=cost,
                participation=participation,
            )
            if isinstance(outcome, Fill):
                fills.append(outcome)
                trips.extend(book.apply(outcome))
            else:
                rejects.append(outcome)

        # 净值挂在收盘上：`ts` 是这根的收盘时刻（ADR-0009 决定 2），此刻它已走完，用它标记
        # 不算穿越。挂在结算之后、问策略之前，让"结算 → 净值 → 问策略"在代码里就是那个顺序。
        book.last_price = bar.close
        value = book.held * bar.close
        equity.append(
            EquityPoint(
                stamp=stamp,
                code=code,
                cash=book.cash,
                held=book.held,
                market_value=value,
                pnl=book.cash + value - book.base_cost,
            )
        )

        signal = strategy.signals(
            View(
                code=code,
                stamp=stamp,
                bar=bar,
                bars=tuple(past),
                holding=book.held,
                sellable=book.sellable,
                lot_size=sizing.lot_size,
                index=index,
            )
        )
        if signal is not None:
            due.append(
                (
                    index + delay,
                    Order(
                        code=code,
                        side=signal.side,
                        shares=signal.lots * sizing.lot_size,
                        signal_at=stamp,
                        strategy=strategy.name,
                    ),
                )
            )

    if day is not None:
        events.extend(ledger.close_day(day))
    for code, queued in sorted(pending.items()):
        for _, order in sorted(queued, key=lambda pair: pair[0]):
            rejects.append(
                Reject(
                    order=order,
                    reason=REASON_NO_BAR,
                    detail=(
                        f"{code} {say(order.signal_at)} 的信号等不到第 {delay} 根："
                        "区间到头了（这单不追，ADR-0010 决定 4）"
                    ),
                )
            )
    return Result(
        assumptions=assumptions,
        fills=tuple(fills),
        rejects=tuple(rejects),
        round_trips=tuple(trips),
        day_events=tuple(events),
        equity=tuple(equity),
        closings=ledger.closings(),
    )
