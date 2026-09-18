"""门禁判定的输入形状：谓词函数能看到什么（04 §二）。

`RowFacts` 刻意只放**事实**，不放任何规则的结论：哪天前收盘、因子是多少、这条是不是
重复键、输入顺序乱了没。规则要判的东西一律从事实自己推——否则每加一条规则都得回引擎
里塞一个新字段，"新增规则不改引擎代码"（Step 1 验收 3）当天就破了。

`master` / `calendar` 整批只装配一次、逐行共享：谓词要问"那天它是不是 ST""上市第几个
交易日"，直接问主数据，不必引擎替每条规则预先把答案抄一遍。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from zhixing_quant.domain.bar import BarDraft

if TYPE_CHECKING:
    from zhixing_quant.domain.calendar import TradingCalendar
    from zhixing_quant.domain.security import SecurityMaster, SecurityState


@dataclass(frozen=True)
class RowFacts:
    draft: BarDraft
    state: SecurityState | None
    prev_close: float | None
    prev_factor: float | None
    already_present: bool
    out_of_order: bool
    master: SecurityMaster | None
    calendar: TradingCalendar | None


@dataclass(frozen=True)
class BatchFacts:
    """整批级规则（FATAL）的输入：逐行判不出"这批缺了哪几个交易日"。"""

    drafts: tuple[BarDraft, ...]
    master: SecurityMaster | None
    calendar: TradingCalendar | None


@dataclass(frozen=True)
class Violation:
    rule_id: str
    level: str
    reason: str
