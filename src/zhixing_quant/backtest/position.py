"""底仓台账：T+1 可卖额度与回合配对（ADR-0010 决定 5、决定 6）。

这里管的是**账户允不允许**，与 `fills.attempt` 管的"市场允不允许"是两件事：一笔卖单可以
市场那头完全成交得了（有量、没封板），却因为卖的是今天刚买的股票而不成立。两类拒单各自记
原因，报告才答得出"今天为什么没做成这笔 T"。

台账的形状由做T 这件事本身决定（07 §二）：底仓是既有的持仓、不是交易，所以它不进配对队列；
一笔卖出如果没吃到"买了没卖出"的那一截，就成了欠仓（等买回），一笔买入如果没吃到欠仓，
就成了加仓（等卖出）。两条队列各一方向、按 FIFO 配对，**允许跨日**——第二天接回正是 T飞 的
后续，按日切断就把亏损藏起来了（决定 6）。

盈亏只算在这两截真金白银上：底仓自身的涨跌是持有这只票的 beta，不是做T 的成绩，所以它进
净值、不进回合（07 §七 验收 5 要手工核算的那几个数，全部来自回合）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from datetime import date

from zhixing_quant.backtest.fills import Fill
from zhixing_quant.backtest.spec import Side, Sizing
from zhixing_quant.domain.bar import Bar, Stamp, stamp_of

#: 第五种拒单原因，住在台账而不是 `fills`：见模块 docstring 那句"账户 vs 市场"。
#: 判它也在 `attempt` 之前——一笔根本不该存在的委托，不必去问市场接不接。
REASON_T1 = "t_plus_1"


@dataclass(frozen=True)
class Leg:
    """一截还没还原的仓位。`shares` 是**剩余**可配股数（部分配对后会缩小）。

    费用按股摊薄存：一笔委托的最低佣金里有一部分是固定额，按股摊会在"一腿被拆成两个回合"时
    分不匀。做T 的每一腿都是同手数的整笔（07 §二），拆腿本身已是边角情况，摊薄够用且说得清。
    """

    opened_on: date
    opened_at: Stamp
    shares: int
    price: float
    fee_per_share: float


@dataclass(frozen=True)
class RoundTrip:
    """一个回合：一买一卖配上的那一截。盈亏已扣两笔费用。"""

    code: str
    buy_at: Stamp
    sell_at: Stamp
    shares: int
    buy_price: float
    sell_price: float
    fee: float

    @property
    def pnl(self) -> float:
        return (self.sell_price - self.buy_price) * self.shares - self.fee

    @property
    def same_day(self) -> bool:
        """日内完成的回合（先买后卖或先卖后买都算）。跨日的不是 T，是接回。"""
        return self.buy_at[0] == self.sell_at[0]


@dataclass(frozen=True)
class DayEvent:
    """某票某日收盘的 T飞/加仓计数（决定 6 的两个事件计数，单位是**笔**）。

    一笔委托只要收盘时还剩股数没还原就计 1——计"股数"会让同一个动作有两种读法（这笔 T 飞了
    还是没飞），而 07 §七 验收 5 要的是"T飞率"这个能手工数出来的数。
    """

    code: str
    trade_date: date
    sells: int
    buys: int
    flew: int
    added: int


@dataclass(frozen=True)
class Closing:
    """一次回测跑完时某票的期末状态（报告那句"还有 N 手未平仓"的出处）。"""

    code: str
    held: int
    cash: float
    last_price: float
    base_shares: int
    base_cost: float
    owed: tuple[Leg, ...]
    extra: tuple[Leg, ...]

    @property
    def open_shares(self) -> int:
        """未还原的股数合计（欠仓 + 加仓）。报告按 `lot_size` 换成手数给人看。"""
        return sum(leg.shares for leg in self.owed + self.extra)

    @property
    def unrealized(self) -> float:
        """未还原仓位的浮盈亏（欠仓浮盈 = 卖价跌下来了；不含底仓的 beta）。"""
        out = 0.0
        for leg in self.extra:
            out += (self.last_price - leg.price) * leg.shares - leg.fee_per_share * leg.shares
        for leg in self.owed:
            out += (leg.price - self.last_price) * leg.shares - leg.fee_per_share * leg.shares
        return out

    @property
    def base_price(self) -> float:
        """底仓入账价 = 首日开盘价。是假设不是事实，报告要印它（`Ledger.open`）。"""
        return self.base_cost / self.base_shares if self.base_shares else 0.0

    @property
    def base_pnl(self) -> float:
        """底仓自身的浮动盈亏：持有这只票的代价/收获，与做T 无关，报告单列。"""
        return (self.last_price - self.base_price) * self.base_shares


@dataclass
class CodeBook:
    """一只票的台账。可变，但只在一次 `engine.run` 内部活着——跨 run 不共享。"""

    code: str
    lot_size: int
    base_shares: int
    base_cost: float
    held: int
    day_start_held: int
    sold_today: int
    cash: float
    last_price: float
    opened_on: date
    owed: deque[Leg] = field(default_factory=deque)
    extra: deque[Leg] = field(default_factory=deque)
    sells_today: int = 0
    buys_today: int = 0

    @property
    def sellable(self) -> int:
        """今天还能卖多少：**当日期初持仓 − 当日已卖**（决定 5）。

        当日买入不进这里——那正是 T+1。它也不减：今天买了 300 又卖掉期初那 300 是合法的
        （先买后卖的 T入→T出），交易所只限制"当日买入的不可卖"，不区分卖出的是哪一批。
        """
        return self.day_start_held - self.sold_today

    def apply(self, fill: Fill) -> tuple[RoundTrip, ...]:
        """记下一笔成交，返回它配上的回合（可能 0 个，也可能 2 个：一腿被两腿吃掉）。"""
        order = fill.order
        shares = order.shares
        fee_per_share = fill.fee / shares
        on = fill.bar.trade_date
        at = stamp_of(on, fill.bar.ts)
        if order.side == "buy":
            self.buys_today += 1
            trips = self._match(self.owed, "buy", at, fill.price, fee_per_share, shares)
            left = shares - sum(t.shares for t in trips)
            if left:
                self.extra.append(Leg(on, at, left, fill.price, fee_per_share))
        else:
            self.sells_today += 1
            self.sold_today += shares
            trips = self._match(self.extra, "sell", at, fill.price, fee_per_share, shares)
            left = shares - sum(t.shares for t in trips)
            if left:
                self.owed.append(Leg(on, at, left, fill.price, fee_per_share))
        self.held += shares if order.side == "buy" else -shares
        self.cash += -fill.notional - fill.fee if order.side == "buy" else fill.notional - fill.fee
        self.last_price = fill.price
        return tuple(trips)

    def _match(
        self,
        queue: deque[Leg],
        side: Side,
        at: Stamp,
        price: float,
        fee_per_share: float,
        shares: int,
    ) -> list[RoundTrip]:
        """从对面那截队列里按 FIFO 吃掉股数。`queue` 是"等着被我还原"的那些腿。"""
        trips: list[RoundTrip] = []
        left = shares
        while left > 0 and queue:
            leg = queue[0]
            matched = min(leg.shares, left)
            buy_at, sell_at = (leg.opened_at, at) if side == "sell" else (at, leg.opened_at)
            buy_price, sell_price = (leg.price, price) if side == "sell" else (price, leg.price)
            trips.append(
                RoundTrip(
                    code=self.code,
                    buy_at=buy_at,
                    sell_at=sell_at,
                    shares=matched,
                    buy_price=buy_price,
                    sell_price=sell_price,
                    fee=(leg.fee_per_share + fee_per_share) * matched,
                )
            )
            left -= matched
            queue[0] = replace(leg, shares=leg.shares - matched)
            if queue[0].shares == 0:
                queue.popleft()
        return trips

    def close_day(self, on: date) -> DayEvent | None:
        """收盘结算：数出今天的 T飞/加仓，然后把日内的量翻篇。

        返回 None 表示今天这只票一笔都没做——那不是"0 飞 0 加"，是"今天没做"，
        报告里两句话不能长一样（04 §四 那条老规矩）。
        """
        event: DayEvent | None = None
        if self.sells_today or self.buys_today:
            event = DayEvent(
                code=self.code,
                trade_date=on,
                sells=self.sells_today,
                buys=self.buys_today,
                flew=sum(1 for leg in self.owed if leg.opened_on == on),
                added=sum(1 for leg in self.extra if leg.opened_on == on),
            )
        self.sells_today = 0
        self.buys_today = 0
        self.sold_today = 0
        self.day_start_held = self.held
        return event

    def closing(self) -> Closing:
        return Closing(
            code=self.code,
            held=self.held,
            cash=self.cash,
            last_price=self.last_price,
            base_shares=self.base_shares,
            base_cost=self.base_cost,
            owed=tuple(self.owed),
            extra=tuple(self.extra),
        )


@dataclass
class Ledger:
    """一次回测的全部底仓台账。引擎只通过它记账，自己不碰持仓。"""

    sizing: Sizing
    books: dict[str, CodeBook] = field(default_factory=dict)

    def open(self, bar: Bar) -> CodeBook:
        """这只票第一次出现：按那一根的开盘价立起底仓。

        入账价是个**假设**而不是事实（我们不知道用户何时买的、买在多少），所以它只用来算净值
        的起点——回合的盈亏一个都不碰它。报告要把它印出来。
        """
        code = bar.code
        existing = self.books.get(code)
        if existing is not None:
            return existing
        shares = self.sizing.base_shares
        book = CodeBook(
            code=code,
            lot_size=self.sizing.lot_size,
            base_shares=shares,
            base_cost=shares * bar.open,
            held=shares,
            day_start_held=shares,
            sold_today=0,
            cash=0.0,
            last_price=bar.close,
            opened_on=bar.trade_date,
        )
        self.books[code] = book
        return book

    def close_day(self, on: date) -> tuple[DayEvent, ...]:
        return tuple(
            event
            for event in (book.close_day(on) for _, book in sorted(self.books.items()))
            if event is not None
        )

    def closings(self) -> tuple[Closing, ...]:
        return tuple(book.closing() for _, book in sorted(self.books.items()))
