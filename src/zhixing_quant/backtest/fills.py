"""一笔委托落在某一根K线上到底成不成交（ADR-0010 决定 3、决定 4）。

只有这一个函数知道全部四条硬判定，这是刻意的：07 §5.2 要的"不可成交建模"如果被拆成
"引擎里查一下涨跌停、别处查一下量"，就会有一处忘了查——而忘了查的后果是回测凭空多出一笔
现实中做不成的成交，收益虚高，且没有任何地方会响。

`attempt` 是纯函数，不碰磁盘也不看时钟：给它委托、那一根K线、那天的板、成本假设。03-4.4 的
构造样本（一字涨停、停牌、零成交）与 03-4.1 的确定性因此都直接测得到它。

判定**顺序是口径**，不是随手写的：制度（有没有板）先于流动性（有没有量）。一根封死又恰好
没什么量的K线同时缺两样，报告只记前者——封板的票谈不上流动性，而两个都记会让"今天为什么
没成交"出现两种算法。
"""

from __future__ import annotations

from dataclasses import dataclass

from zhixing_quant.backtest.limits import PriceBand, clamp_fill, locked
from zhixing_quant.backtest.spec import Cost, Side
from zhixing_quant.domain.bar import Bar, Stamp

#: 拒单原因。这五个字符串是日报与回测报告按类计数的口径（"不可成交"必须看得见，而不是一笔
#: 单子静默消失），所以它们是常量；给人看的那句话在 `Reject.detail` 里。
REASON_NO_BAR = "no_bar"
REASON_BAND_UNKNOWN = "band_unknown"
REASON_LOCKED = "locked"
REASON_ZERO_VOLUME = "zero_volume"
REASON_PARTICIPATION = "participation"

#: 五条市场侧原因，**顺序就是判序**（决定 4 末段）：没有K线 → 判不出板 → 一字封死 →
#: 零成交 → 参与率超了。报告按这个顺序把六档零填充列出来（账户侧的 `t_plus_1` 排在最前，
#: 它比市场侧任何一条都先判），所以改了 `attempt` 的先后就得同时改这里——不一致会让
#: 报告那一节的顺序自相矛盾，而它是用户唯一会读的那张表。
REASONS: tuple[str, ...] = (
    REASON_NO_BAR,
    REASON_BAND_UNKNOWN,
    REASON_LOCKED,
    REASON_ZERO_VOLUME,
    REASON_PARTICIPATION,
)


@dataclass(frozen=True)
class Order:
    """一笔决定要下的单子：方向、股数、以及它是谁在什么时候发的。

    `signal_at` 不是记账用的装饰——拒单要能回答"哪天哪天哪个策略的信号没打成"，那正是
    07 §5.2 要执行延迟建模之后仍然看得见的东西。
    """

    code: str
    side: Side
    shares: int
    signal_at: Stamp
    strategy: str = ""

    def __post_init__(self) -> None:
        if self.shares <= 0:
            raise ValueError(f"{self.code} 委托了 {self.shares} 股：0 或负数的单子不该存在")


@dataclass(frozen=True)
class Fill:
    """成交。`price` 已含滑点并受板价约束，`fee` 是这一笔的全部费用。"""

    order: Order
    bar: Bar
    price: float
    fee: float

    @property
    def notional(self) -> float:
        return self.price * self.order.shares


@dataclass(frozen=True)
class Reject:
    """没成交。`reason` 取上面五个常量之一。"""

    order: Order
    reason: str
    detail: str


Outcome = Fill | Reject


def blocks(side: Side, sealed: str) -> bool:
    """封板方向挡不挡这一笔。涨停挡买、跌停挡卖；反方向在板上照样成交（板上有的是对手盘）。"""
    return (side == "buy") == (sealed == "up")


def fill_price(bar: Bar, band: PriceBand, side: Side, *, slippage_pct: float) -> float:
    """成交价 = 那一根的**开盘价**向不利方向让一个滑点，再压回板价以内（ADR-0010 决定 3）。

    买是加、卖是减：滑点站在用户不利的一侧。写成"开盘价 ± 滑点"而不是"均价 ± 滑点"是因为
    均价（`amount/volume`）是那根K线走完才知道的事实，用它成交就是穿越时间。
    """
    raw = bar.open * (1.0 + slippage_pct / 100.0 if side == "buy" else 1.0 - slippage_pct / 100.0)
    return clamp_fill(raw, band, side)


def attempt(
    order: Order,
    bar: Bar | None,
    band: PriceBand,
    *,
    cost: Cost,
    max_participation_pct: float,
) -> Outcome:
    """一次成交尝试：成了给 `Fill`，没成给 `Reject` 并说清是哪一条拦下的。

    `bar=None` 说的是"那一刻根本没有K线"——停牌、退市、分钟行断了（ADR-0009 决定 6 的断档），
    引擎不许把它当成"价格没变"继续往下算。
    """
    if bar is None:
        return Reject(
            order=order,
            reason=REASON_NO_BAR,
            detail=f"{order.code} 那一刻没有K线：停牌、退市或分钟行断了",
        )
    if not band.judgeable:
        return Reject(
            order=order,
            reason=REASON_BAND_UNKNOWN,
            detail=f"{order.code} 判不出涨跌停边界，按不成交处理：{band.note}",
        )
    sealed = locked(bar, band)
    if sealed is not None and blocks(order.side, sealed):
        return Reject(
            order=order,
            reason=REASON_LOCKED,
            detail=(
                f"{order.code} 整根一字封在{'涨' if sealed == 'up' else '跌'}停"
                f"（{order.side} 方向买/卖不进）：开 {bar.open} 高 {bar.high} 低 {bar.low}"
            ),
        )
    if bar.volume <= 0:
        return Reject(
            order=order, reason=REASON_ZERO_VOLUME, detail=f"{order.code} 该根零成交，无处成交"
        )
    allowed = bar.volume * max_participation_pct / 100.0
    if order.shares > allowed:
        return Reject(
            order=order,
            reason=REASON_PARTICIPATION,
            detail=(
                f"{order.code} 委托 {order.shares} 股超过该根成交量的 "
                f"{max_participation_pct}%（上限 {allowed:.0f} 股）"
            ),
        )
    price = fill_price(bar, band, order.side, slippage_pct=cost.slippage_pct)
    return Fill(order=order, bar=bar, price=price, fee=cost.fee(order.side, price * order.shares))
