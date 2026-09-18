"""交易日历（01 Step 1 核心模型）。

日历是 R006 的判据，也是"新股第 N 个交易日"这类口径的度量尺，所以它只提供事实，
不做解释：哪几天开市是数据，缺没缺、该不该跳空由 quality 层判。

内部用排序后的日期元组 + bisect，不用 set：日历要回答"上一个交易日"，set 答不了。
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence
from datetime import date


class TradingCalendar:
    """A 股交易日集合。多源日历不一致时，取哪一个都是决策，不在这里做。"""

    __slots__ = ("_days",)

    def __init__(self, trading_days: Iterable[date]) -> None:
        self._days: tuple[date, ...] = tuple(sorted(set(trading_days)))

    @property
    def days(self) -> Sequence[date]:
        return self._days

    def is_trading_day(self, d: date) -> bool:
        idx = bisect.bisect_left(self._days, d)
        return idx < len(self._days) and self._days[idx] == d

    def trading_days_between(self, start: date, end: date) -> Sequence[date]:
        """[start, end] 闭区间内的交易日，升序。"""
        lo = bisect.bisect_left(self._days, start)
        hi = bisect.bisect_right(self._days, end)
        return self._days[lo:hi]

    def count_between(self, start: date, end: date) -> int:
        return len(self.trading_days_between(start, end))

    def next_trading_day(self, d: date) -> date | None:
        idx = bisect.bisect_right(self._days, d)
        return self._days[idx] if idx < len(self._days) else None

    def prev_trading_day(self, d: date) -> date | None:
        idx = bisect.bisect_left(self._days, d) - 1
        return self._days[idx] if idx >= 0 else None

    def missing_days(self, observed: Iterable[date]) -> Sequence[date]:
        """已观测日期覆盖不到的交易日（R006 "交易日缺失"半边）。

        只看观测集合覆盖的区间 [min, max]：日历尾部还没到的日子、数据窗口之外的日子
        都不算缺，否则任何增量抓取都会报一屏假缺失。
        """
        seen = set(observed)
        if not seen:
            return ()
        return tuple(d for d in self.trading_days_between(min(seen), max(seen)) if d not in seen)

    def nth_trading_day_onward(self, start: date, n: int) -> date | None:
        """从 start 起算的第 n 个交易日（start 非交易日时顺延到下一个）。

        04 §二 的"新股前 5 个交易日"就是它：n=1 是上市当天所在/之后的第一个交易日。
        """
        if n < 1:
            raise ValueError(f"n 从 1 起算，收到 {n}")
        idx = bisect.bisect_left(self._days, start)
        pos = idx + n - 1
        return self._days[pos] if pos < len(self._days) else None
