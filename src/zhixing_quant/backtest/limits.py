"""板价与一字板：涨跌停制度落在分钟K线上的形状（ADR-0010 决定 4、决定 9）。

引擎在这里回答两个问题：**这一根K线封死了吗**（封死的方向买不到/卖不出），以及
**成交价最高能到哪儿**（板价是法规边界，滑点不许越过它）。两个问题都只靠一跟K线的六个字段
回答，不引入任何"经验阈值"——9.8% 之类的数不认 ST 的 5%、创业板的 20%、新股首日的不设限，
而那三档在 `config/gate.toml` 里已经有一张表（取数走 `domain/limits.py`）。

**为什么判据是相对量**：涨跌停本来是真实报价上的制度，而引擎吃的是后复权价（决定 9）。同一天
的四个价乘的是同一个因子，所以"相对昨收涨了多少"在两种口径下**数值相同**，判板不需要知道因子。
好处不止省一次换算：除权日的"除权参考价"在后复权口径里就等于昨收，交易所那套参考价规则自己
抵消了，不需要在这里再实现一遍。

**抵消不掉的只有板价的分数取整**：交易所把"昨收 × 幅度"四舍五入到分，真实涨停的涨幅因此可能
比档位低一点点（10.03 元涨 10% 是 11.03，只涨 9.970%）。`ALLOWANCE_PP` 就是为那一点点留的余量，
它宁松不紧——算窄了会把真封板的K线判成"还能买"，那是高估可成交性，07 从头到尾要防的就是
这个方向。取 0.5 个百分点而不是按价格算，是因为余量真正取决于**真实报价**里有几个半分钱，
而真实报价不在引擎的口径里：后复权价 ≥ 真实报价，按后复权价算只会把余量算小（算窄），
一个随价格变小的式子会往错的方向变。留一个常量、把它的边界写在下面，比留一个看起来更聪明
的公式诚实。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from zhixing_quant.backtest.spec import Side
from zhixing_quant.domain.bar import Bar

#: 判"这一根只在一个价上成交过"用的相对容差。四价同源同因子，真一字的相对差是 0；给一点
#: 容差是因为源的小数位不齐（`1261.100` 与 `1261.1`）时精确 `==` 会把真板判漏。
FLAT_REL_TOL = 1e-9
#: 分数取整的余量（百分点）。半分钱对 1 元的票正好是 0.5pp，所以**昨收低于 1 元的票**（面值
#: 退市预警那一档）可能把真封板判成没封板——方向是高估可成交性，已知且写在 ADR-0010 代价里。
#: 昨收 ≥ 1 元的票全覆盖，而余量不随价格变化，判定因此与口径完全无关（属性测试轰的就是这条）。
ALLOWANCE_PP = 0.5

#: 板处于哪种状态。三种都要能说出来："有界"、"今天不设涨跌幅限制"、"我判不出来"。把后两种
#: 都写成"没有板"，回测就会在缺主数据的票上放心大胆地成交。
BandState = Literal["bound", "unlimited", "unknown"]


@dataclass(frozen=True)
class PriceBand:
    """一只票某一天的涨跌幅边界。用下面三个构造器，别自己拼字段。"""

    state: BandState = "unknown"
    pct: float = 0.0
    prev_close: float = 0.0
    note: str = ""

    @classmethod
    def bound(cls, pct: float, prev_close: float) -> PriceBand:
        """有界档：上限 `pct` 个百分点，基准是昨收 `prev_close`（与K线同口径）。

        昨收不为正直接拒建而不是返回 `unknown`：拿 0 当"没有昨收"会把板价算成 0，于是任何
        成交价都算"越界"、跌停价还是个负数——那种数不会响，只会安静地把每一笔单都拒掉，
        日报上看起来像"今天全市场封死跌停"。没有昨收的票走 `unknown`。
        """
        if prev_close <= 0:
            raise ValueError(f"昨收必须是正数，收到 {prev_close}：没有昨收请构造 unknown")
        return cls(state="bound", pct=pct, prev_close=prev_close)

    @classmethod
    def unlimited(cls, why: str) -> PriceBand:
        """今天不设涨跌幅限制。`why` 会进拒单与报告，例如"新股无涨跌幅窗口第 2 个交易日"。"""
        return cls(state="unlimited", note=why)

    @classmethod
    def unknown(cls, why: str) -> PriceBand:
        """判不出来：认不出板块、缺主数据、没有昨收。缺哪一样就写哪一样。"""
        return cls(state="unknown", note=why)

    @property
    def judgeable(self) -> bool:
        """能不能判板。`unlimited` 是"能判，答案是没有限制"，所以它算可判。"""
        return self.state != "unknown"

    def ceiling(self) -> float | None:
        """涨停价。不设限与判不出都是 None——None 不是"没有上限"，是"这里不该走到"。"""
        if self.state != "bound":
            return None
        return self.prev_close * (1.0 + self.pct / 100.0)

    def floor(self) -> float | None:
        """跌停价，同 `ceiling`。"""
        if self.state != "bound":
            return None
        return self.prev_close * (1.0 - self.pct / 100.0)


def one_price(bar: Bar) -> bool:
    """这根K线是否只在一个价上成交过（一字）。高低价相等就够了：`Bar` 已保证
    `high ≥ max(open, close) ≥ min(open, close) ≥ low`，四价被夹成同一个数。"""
    return abs(bar.high - bar.low) <= FLAT_REL_TOL * max(bar.high, bar.low)


def locked(bar: Bar, band: PriceBand) -> str | None:
    """这一根是不是封死的板：`"up"` 买不到、`"down"` 卖不出、`None` 没封。

    判不出（`judgeable` 为假）时也返回 None，但那是两句话——调用方必须先问 `judgeable`
    再问这里，否则"不知道板在哪"就冒充了"板不存在"。
    """
    if band.state != "bound" or not one_price(bar):
        return None
    change = (bar.close - band.prev_close) / band.prev_close * 100.0
    threshold = band.pct - ALLOWANCE_PP
    if change >= threshold:
        return "up"
    if change <= -threshold:
        return "down"
    return None


def clamp_fill(price: float, band: PriceBand, side: Side) -> float:
    """把加了滑点的成交价压回板价以内（买不许高于涨停价、卖不许低于跌停价）。

    只有"有界"档才夹。不夹进K线的 `[low, high]`：那等于把滑点偷偷抹掉，而滑点是 07 §5.2
    点名要建模的现实（ADR-0010 决定 3）。
    """
    if band.state != "bound":
        return price
    if side == "buy":
        upper = band.ceiling()
        return price if upper is None else min(price, upper)
    lower = band.floor()
    return price if lower is None else max(price, lower)
