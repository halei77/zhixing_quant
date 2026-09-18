"""证券主数据，point-in-time 可查询（01 Step 1；03 §二 L4.6 的地基）。

存的是"带生效区间的事实"，不是"今天的答案"：上市/退市、ST、停牌、更名都是区间。
查询一律带 as_of 日期，答的是"那天知道什么"——回测按历史当时的股票池还原靠的就是
这一层，所以本模块不接受没有 as_of 的问法：那种签名一旦被回测调用，幸存者偏差就
以函数名的形式写进了代码。

装载时校验并排序（重叠区间、重复上市记录、退市早于上市都拒绝），查询就只做二分与
线性扫，不给脏主数据留"看起来正常"的机会。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.symbol import Board, board_of, normalize_code

_OPEN_END = date.max


class MasterConflict(ValueError):
    """主数据自身不可信：装载时就拒绝，不留到查询时给个错答案。"""


def _listed_between(listed_on: date, delisted_on: date | None, as_of: date) -> bool:
    """退市当天算在册：交易所在那天出最后一片数据，门禁得能判它。"""
    return listed_on <= as_of and (delisted_on is None or as_of <= delisted_on)


@dataclass(frozen=True)
class Listing:
    code: str
    name: str
    listed_on: date
    delisted_on: date | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", normalize_code(self.code))
        if self.delisted_on is not None and self.delisted_on < self.listed_on:
            raise MasterConflict(f"{self.code} 退市日早于上市日")

    def listed_during(self, as_of: date) -> bool:
        return _listed_between(self.listed_on, self.delisted_on, as_of)


@dataclass(frozen=True)
class Interval:
    """一段状态历史（ST、停牌）。end 为 None 表示持续中。"""

    code: str
    start: date
    end: date | None = None

    def covers(self, as_of: date) -> bool:
        return self.start <= as_of and (self.end is None or as_of <= self.end)


@dataclass(frozen=True)
class Rename:
    code: str
    effective_on: date
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", normalize_code(self.code))


@dataclass(frozen=True)
class SecurityState:
    """某只票在某天的时点状态。门禁的 R003/R004 与豁免判定都读它。"""

    code: str
    as_of: date
    name: str
    listed_on: date
    delisted_on: date | None
    is_st: bool
    is_suspended: bool

    @property
    def board(self) -> Board:
        return board_of(self.code)

    @property
    def is_listed(self) -> bool:
        return _listed_between(self.listed_on, self.delisted_on, self.as_of)


def _reject_overlap(kind: str, items: Sequence[Interval]) -> None:
    """同类区间重叠 → 同一时点会同时答"是 ST"和"不是 ST"，没有判得出来的道理。"""
    for code in sorted({x.code for x in items}):
        spans = sorted((x for x in items if x.code == code), key=lambda x: x.start)
        for prev, cur in pairwise(spans):
            if cur.start <= (prev.end or _OPEN_END):
                raise MasterConflict(f"{kind} {code} 区间重叠：{prev} 与 {cur}")


class SecurityMaster:
    """全市场主数据。"""

    def __init__(
        self,
        listings: Iterable[Listing] = (),
        st_periods: Iterable[Interval] = (),
        suspensions: Iterable[Interval] = (),
        renames: Iterable[Rename] = (),
    ) -> None:
        self.listings: tuple[Listing, ...] = tuple(
            sorted(listings, key=lambda x: (x.listed_on, x.code))
        )
        codes = [x.code for x in self.listings]
        if len(codes) != len(set(codes)):
            dup = sorted({c for c in codes if codes.count(c) > 1})
            raise MasterConflict(f"同一代码重复登记上市记录：{dup}")
        self.st_periods: tuple[Interval, ...] = tuple(sorted(st_periods, key=lambda x: x.start))
        self.suspensions: tuple[Interval, ...] = tuple(sorted(suspensions, key=lambda x: x.start))
        self.renames: tuple[Rename, ...] = tuple(sorted(renames, key=lambda x: x.effective_on))
        _reject_overlap("ST", self.st_periods)
        _reject_overlap("停牌", self.suspensions)
        self._by_code: dict[str, Listing] = {x.code: x for x in self.listings}

    def listing(self, code: str) -> Listing:
        normalized = normalize_code(code)
        if normalized not in self._by_code:
            raise KeyError(f"主数据无 {normalized}：股票池与主数据不同源时会静默漏判，宁可抛错")
        return self._by_code[normalized]

    def universe_on(self, as_of: date) -> tuple[str, ...]:
        """as_of 当天在册的全部代码，含"当时上市、后来退市"的那些（03 L4.6）。"""
        return tuple(x.code for x in self.listings if x.listed_during(as_of))

    def name_on(self, code: str, as_of: date) -> str:
        """as_of 当时叫什么。改名前后的名字不能互相污染。"""
        listing = self.listing(code)
        history = [r for r in self.renames if r.code == listing.code and r.effective_on <= as_of]
        return max(history, key=lambda r: r.effective_on).name if history else listing.name

    def st_on(self, code: str, as_of: date) -> bool:
        normalized = normalize_code(code)
        return any(s.covers(as_of) for s in self.st_periods if s.code == normalized)

    def suspended_on(self, code: str, as_of: date) -> bool:
        normalized = normalize_code(code)
        return any(s.covers(as_of) for s in self.suspensions if s.code == normalized)

    def state_on(self, code: str, as_of: date) -> SecurityState:
        listing = self.listing(code)
        return SecurityState(
            code=listing.code,
            as_of=as_of,
            name=self.name_on(listing.code, as_of),
            listed_on=listing.listed_on,
            delisted_on=listing.delisted_on,
            is_st=self.st_on(listing.code, as_of),
            is_suspended=self.suspended_on(listing.code, as_of),
        )

    def listing_day_index(self, code: str, as_of: date, calendar: TradingCalendar) -> int | None:
        """as_of 是该股上市后的第几个交易日（1 起）；数不出来返回 None。

        用"上市日之后（含当天）的交易日个数"而不是日历日差：新股的涨跌幅窗口是按
        交易日数的，节假日不占额度。

        日历没覆盖到上市日时也返回 None 而不是给出一个偏小的数：增量抓取通常只装最近
        几十天的日历，2001 年上市的票在这份日历上"数得出 3 个交易日"，于是被当成新股
        豁免掉 R004 的涨跌幅检查——门禁最坏的一种失效，因为它看起来像正常放行。
        """
        listing = self.listing(code)
        if not calendar.is_trading_day(as_of) or as_of < listing.listed_on:
            return None
        first_day = calendar.days[0]
        if first_day > listing.listed_on:
            return None
        return calendar.count_between(listing.listed_on, as_of)

    def no_limit_period(
        self, code: str, as_of: date, calendar: TradingCalendar, trading_days: int
    ) -> bool:
        """是否处于"上市后前 trading_days 个交易日不设涨跌幅"的豁免窗口（04 §二）。

        天数由配置给：注册制下主板/创业板/科创板是 5，北交所口径另算（首日不设、此后
        30%）。把 5 写死在这里就得为每个板块开一个分支，而板块差异本就在配置表里。
        """
        idx = self.listing_day_index(code, as_of, calendar)
        return idx is not None and idx <= trading_days
