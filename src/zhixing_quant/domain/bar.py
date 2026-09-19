"""K线契约（01 Step 1 核心模型）。

两个模型不是冗余，是门禁能成立的前提：

- `BarDraft` 是**门禁的输入**（04 §一 里"适配器统一为标准契约"的那一步）。字段一律
  可空、可负、可为 NaN——R001/R002/R010 判的就是这些东西，模型若先替门禁把它们拒了，
  隔离区永远是空的、违规条目永远拿不到规则编号，04 §四 的日报明细也就没有来源。
- `Bar` 是**干净区的形状**，也就是策略层/回测层唯一允许读的东西（04 §一 关键不变量）。
  它在构造时就断言全部业务不变量，是对已通过门禁之数据的第二道锁：万一有人在门禁之外
  造数据，这里兜住。

同一个不变量在两处判，因此判定式只写一遍（`ohlc_violations` / `nonpositive_prices`），
两处共用——写两份迟早漂。它由 hypothesis 轰炸验证（03 §二 L3）。

`ts` 是 ADR-0009 决定 2 给分钟线留的那一个字段，含义是**这根K线的收盘时刻**（右端点：5 分钟
的 09:35 那根覆盖 09:30–09:35）。日线为 None。加上它之后，全系统的主键是
`(symbol, trade_date, ts)`：日线 `ts=None`，它自然退化回 ADR-0003 的 `(symbol, trade_date)`，
所以两种粒度共用一条判定路径，没有"这是日线还是分钟线"的开关要维护。
"""

import math
from collections.abc import Sequence
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from zhixing_quant.domain.symbol import Board, board_of, normalize_code


def _ge(a: float, b: float) -> bool:
    """a ≥ b。NaN 参与比较恒为假，于是 `not _ge(...)` 就是"这条不成立"。

    写成 `not (a < b)` 会漏：NaN 时两个方向都是假，"没通过"和"通过"就分不开了。
    """
    return a >= b


def not_finite(*values: float) -> bool:
    """NaN 与 ±inf 一锅判。

    NaN 用自等式判（任何比较对它都成立不了）；inf 更要判——`inf >= inf` 是成立的，
    所以"高不低于开收"这种不等式对全 inf 的四元组会判"合法"，而一条价格为无穷的K线
    进干净区之后，收益率、复权价全会跟着变 inf，且一声不响。
    """
    return not all(math.isfinite(v) for v in values)


def ohlc_violations(open_: float, high: float, low: float, close: float) -> Sequence[str]:
    """R001 判定式：`high ≥ max(open,close) ≥ min(open,close) ≥ low` 不成立的具体原因。

    NaN 单列一条先行返回：不能指望它被比较式顺手抓出来——`max(10.0, nan)` 与
    `max(nan, 10.0)` 结果不同（后者是 NaN），所以 close=NaN、其余合法的四元组会让
    三条比较全部"看起来通过"。属性测试抓到过这个形状，所以判 NaN 用自等式而不是
    比较式，并且不判成"通过"：不可判定的数据一律不进干净区。
    """
    prices = (open_, high, low, close)
    if not_finite(*prices):
        return ("价格含 NaN/无穷，OHLC 无法判定",)
    out: list[str] = []
    if not _ge(high, max(open_, close)):
        out.append("high 低于 open/close 较高者")
    if not _ge(min(open_, close), low):
        out.append("low 高于 open/close 较低者")
    if not _ge(high, low):
        out.append("high 低于 low")
    return out


def nonpositive_prices(open_: float, high: float, low: float, close: float) -> bool:
    """R002 的价格半边：任一价格为 0 或负即违规（NaN 不算，由 R001 抓）。"""
    return any(x <= 0 for x in (open_, high, low, close))


#: `(日期, 时刻)` 的排序键：两种粒度共用一个比较式，靠的是缺字段补成最小值而不是分支。
#: 日线 `ts=None` 因此退化回原来的按日排序，不需要"这是哪种粒度"的开关。缺日期的行排到
#: 全体最前——R010 正会因为它拒收，排序这里只保证不炸。
Stamp = tuple[date, datetime]


def stamp_of(when_date: date | None, when_ts: datetime | None) -> Stamp:
    return (when_date or date.min, when_ts or datetime.min)


class BarDraft(BaseModel):
    """适配器产出、门禁入口。可空表示"有待判定"，不是"允许缺失入库"。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = ""
    symbol: str
    trade_date: date | None = None
    ts: datetime | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float | None = None
    amount: float | None = None
    adj_factor: float | None = None
    is_suspended: bool = False

    @property
    def code(self) -> str:
        return normalize_code(self.symbol)

    @property
    def identity(self) -> tuple[str, date | None, datetime | None]:
        """这一行"主张的是哪一刻"：日线 `ts=None`，于是它退化回 `(symbol, trade_date)`。"""
        return (self.code, self.trade_date, self.ts)

    @property
    def stamp(self) -> Stamp:
        """可比较的时间位置：R009 的乱序判定与"上一行"的装配都按它排，不看 `identity`——
        一刻只能有一个主张，而排在它前面的行未必是同一刻。"""
        return stamp_of(self.trade_date, self.ts)

    def missing_fields(self) -> Sequence[str]:
        """R010：必需字段清单。source/adj_factor/is_suspended 不在必需之列。"""
        required = ("trade_date", "open", "high", "low", "close", "volume", "amount")
        return tuple(f for f in required if getattr(self, f) is None)


class Bar(BaseModel):
    """干净区 K 线：字段齐全、价格严格、OHLC 有序。构造不出违规实例。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = ""
    symbol: str
    trade_date: date
    ts: datetime | None = None
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0, allow_inf_nan=False)
    amount: float = Field(ge=0, allow_inf_nan=False)
    # adj_factor 故意不加约束：04 §二 没有任何 REJECT 级规则管它（R005 是 WARN，管的是
    # "突变"不是"有没有值"），在这里收紧只会让门禁放行、契约拒收。NaN 因子由
    # domain.adjust 在用的那一刻拒（AdjustmentFactor 要求严格为正）。
    adj_factor: float | None = None
    is_suspended: bool = False

    @model_validator(mode="after")
    def _check_ohlc(self) -> "Bar":
        if bad := ohlc_violations(self.open, self.high, self.low, self.close):
            raise ValueError("；".join(bad))
        return self

    @field_validator("symbol")
    @classmethod
    def _canonicalize_symbol(cls, value: str) -> str:
        """干净区的代码一律存 6 位规范形：入库时不归一，下游每个 join 都得各归一次。"""
        return normalize_code(value)

    @property
    def code(self) -> str:
        return normalize_code(self.symbol)

    @property
    def board(self) -> Board:
        """板块由代码现判，不落库：落库就有两份事实，改前缀表时必有一份不更新。"""
        return board_of(self.symbol)

    @classmethod
    def from_draft(cls, draft: BarDraft) -> "Bar":
        """门禁放行后转成干净区契约。脏数据到这里必然抛 ValidationError。"""
        return cls.model_validate(draft.model_dump())
