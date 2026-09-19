"""模板 → 提示词：`site/` 下唯一碰盘的那个文件（ADR-0012 决定 1）。

上游是纯层四件套（`templates` / `table` / `tokens` / `compose`），它们之所以能被属性测试在无盘
环境下轰一万次，代价就是这里：所有"从盘上读"的活收在一处，收在一处才查得清哪行代码动过数据
（与 `backtest/cli.py` 同一手法，边界由 `tests/test_site_boundaries.py` 扫 import 判）。

它也不碰 HTTP、不碰大模型（ADR-0012 候选方案 B）：06 §一 明写一期不自动调用模型，而取数失败
是"报错"、调模型失败是"挂住"——站点是给人用的东西，两种失败不该混在同一个函数里。

于是这一层只剩三件事：把"要 N 个交易日"换算成日期区间、按模板声明的顺序读出每一段、把实际
取到的天数写进标题。第三件事是这一层唯一的防幻觉动作（决定 3）：一份声称 120 天、实际 42 天
的提示词，模型没有任何办法发现自己被骗。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from zhixing_quant import config
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.site import table, tokens
from zhixing_quant.site.compose import Section, assemble
from zhixing_quant.site.templates import Selection, Template
from zhixing_quant.storage import layout
from zhixing_quant.storage.query import read_bars

#: `minute_` 这个前缀从 `layout.MINUTE_5` 反推，不再抄一遍字符串：分钟线加一个周期（ADR-0009
#: 决定 7 那个目录）时，这一层不需要第二处改动。
_MINUTE = f"{layout.MINUTE_5.rsplit('_', 1)[0]}_"


class TemplateNotReady(ValueError):
    """`status: pending` 的模板：它要的数据组件还没落地（决定 5）。

    不是"少给一列照样出提示词"。少一列的表看起来和满列的一样完整，而模型不会因为少了一列就
    写「数据未提供」——它会把那一列编出来。
    """


class CalendarTooShort(ValueError):
    """日历里没有 ≤ `as_of` 的交易日：区间起点无从定起（决定 2 的代价一）。"""


class UnnamedDataset(ValueError):
    """盘上有这个 dataset 而这里不知道它该叫什么：小标题不许编（决定 4）。"""


@dataclass(frozen=True)
class Prompt:
    """一份能直接复制走的提示词，加上给人看的两个数（决定 6）。

    `warn` 不进 `text`：把"这段太长了"写进给模型的指令里，它会当成内容读。
    """

    text: str
    tokens: int
    warn: str | None


def title_of(dataset: str) -> str:
    """一段数据的小标题。周期就是名字里那个数，第二份"盘上有哪几个 dataset"的名单不写。"""
    if dataset == layout.DAILY:
        return "日K"
    if dataset.startswith(_MINUTE):
        return f"{dataset[len(_MINUTE) :]} 分K"
    raise UnnamedDataset(f"{dataset!r} 不是这一层认得的干净区 dataset：标题不知道该叫什么，不许编")


def window(calendar: TradingCalendar, as_of: date, days: int) -> tuple[date, date]:
    """`as_of`（含）往回数第 `days` 个交易日，返回 `(start, end)`。

    `end` 是 ≤ `as_of` 的最后一个交易日，不是 `as_of` 本身：周末生成的提示词，末行应该是周五
    那根K线，而不是一个根本没有K线的日子。

    日历比要的天数还短时不报错、只到它最早的一天——差额由标题说出来（决定 3），而不是由这里
    拒一次生成：新票上市 10 天与"要 120 天"并不矛盾，那是这只票的真实历史。
    """
    eligible = [day for day in calendar.days if day <= as_of]
    if not eligible:
        raise CalendarTooShort(
            f"日历里没有任何早于或等于 {as_of} 的交易日，`days={days}` 的区间无从定起"
        )
    return (eligible[-days] if len(eligible) >= days else eligible[0], eligible[-1])


def heading(selection: Selection, delivered: int) -> str:
    """小标题里的那个天数是**实际取到的**，不是模板要的（决定 3）。"""
    label = title_of(selection.dataset)
    if delivered == selection.days:
        return f"{label}（{selection.days} 个交易日）"
    return f"{label}（盘上 {delivered} 个交易日，模板要 {selection.days}）"


def build(
    template: Template,
    code: str,
    as_of: date,
    *,
    calendar: TradingCalendar,
    token_warn_above: int,
    float_shares: float | None = None,
) -> Prompt:
    """按模板组装一条提示词。数据一段都不补：读回来是什么就是什么。

    `float_shares` 只有模板请求 `turnover` 时才用得上，缺它由 `table.render` 拒（ADR-0011
    决定 5）。日历由调用方给（决定 2）：这一层不联网抓日历，"生成提示词"的路上不该藏着 HTTP。
    """
    if template.status != "ready":
        raise TemplateNotReady(f"「{template.name}」要的数据组件还没落地：{template.waiting_on}")
    sections: list[Section] = []
    for selection in template.data:
        start, end = window(calendar, as_of, selection.days)
        bars = read_bars(
            code,
            start,
            end,
            adjust=template.adjust,
            dataset=selection.dataset,
            root=config.parquet_dir(),
        )
        sections.append(
            Section(
                title=heading(selection, len({bar.trade_date for bar in bars})),
                body=table.render(
                    bars,
                    format=template.format,
                    fields=template.fields,
                    adjust=template.adjust,
                    float_shares=float_shares,
                ),
            )
        )
    text = assemble(template, sections)
    count = tokens.estimate(text)
    warn = (
        None
        if not tokens.over(text, token_warn_above)
        else f"约 {count} token，超过阈值 {token_warn_above}（06 §八-5）。"
        "省 token 的两条路都在模板里：换 `format: csv` 紧凑模式，或砍天数（06 §六）。"
    )
    return Prompt(text=text, tokens=count, warn=warn)
