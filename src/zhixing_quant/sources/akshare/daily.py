"""akshare 日线适配器：源行 → `BarDraft`（04 §五 接入清单第 1 项；ADR-0004 的第一个源）。

这里不碰网络。抓取留在调用方（`tools/` 里的采集脚本与 Step 2b 的定时任务），因为黄金
样本要在 CI 里离线重放（03 §二 L2）——会自己联网的适配器没法重放，"源字段漂移立刻
测红"也就无从谈起。适配器收到的行就是快照里那些行，一列不多一列不少。

字段映射用 `.get()` 而不是 `row["列名"]`：源改了列名时，缺列会变成"该字段为 None"，
由 R010 整批 FATAL 报出来（04 §二）——比在适配器里抛 KeyError 好：前者在日报上看得见、
且把整批拦在干净区外，后者会把采集任务炸成"半批已入库"。

取值归一（`to_float`/`to_date`）在 `sources/rows.py`：每个源都要判"这个值能不能变成一个
字段"，写两遍迟早漂。本模块只管源特有的那一半——哪一列进哪一个字段、两帧怎么对齐。

口径（写在这里是因为它决定跨源对账怎么算，04 §五）：

- `volume` 单位**股**、`amount` 单位**元**（`stock_zh_a_daily` 原样）。东财口径的"手"要
  ×100 之后再进来，跨源对账按这个口径比。
- 价格存**不复权**：前/后复权在查询时由 `domain.adjust` 算，全项目只有那一处换算式。
- `adj_factor` = 后复权收盘 ÷ 不复权收盘。取不到 hfq 时为 None——R005 因此报"没有因子"，
  而不是"没有复权事件"，这两件事在日报上不是一回事。
- 停牌没有源字段可依据（sina 不给），`is_suspended` 一律 False；零成交的行由 R003 报
  WARN 进待核清单，不在这儿猜。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from zhixing_quant.domain.bar import BarDraft, not_finite
from zhixing_quant.sources.rows import to_date, to_float

#: 源标识（04 §三 按源打分、04 §四 日报按源分行）。改名等于给历史分数换了主语，不许随手改。
SOURCE = "akshare_daily"


def _factor(raw_close: float | None, hfq_close: float | None) -> float | None:
    """后复权 ÷ 不复权 = 当日累计因子。任何一个数不成立就不给因子，不用 1.0 兜底。

    1.0 不是"不知道"，是"知道它从没除权过"——拿它兜底会把"源没给 hfq"洗成"这只票历史
    干净"，R005 与回测的复权口径都会跟着错。
    """
    if raw_close is None or hfq_close is None:
        return None
    if not_finite(raw_close, hfq_close) or raw_close <= 0:
        return None
    factor = hfq_close / raw_close
    return factor if factor > 0 else None


def daily_drafts(
    raw_rows: Sequence[Mapping[str, object]],
    hfq_rows: Sequence[Mapping[str, object]] = (),
    *,
    symbol: str,
    source: str = SOURCE,
) -> list[BarDraft]:
    """源行 → 待判定的K线行。`raw_rows` 是不复权帧，`hfq_rows` 是后复权帧（可缺）。

    两帧按日期对齐而不是按行号：源偶尔只回一部分日子（停牌、增量窗口错位），按行号配对
    会把 A 日的收盘与 B 日的因子拼成一行——那正是 03 §二 L3 复权恒等式要抓的形状。
    """
    hfq_by_day = {
        day: to_float(row.get("close"))
        for row in hfq_rows
        if (day := to_date(row.get("date"))) is not None
    }
    out: list[BarDraft] = []
    for row in raw_rows:
        day = to_date(row.get("date"))
        close = to_float(row.get("close"))
        # 日期都对不上就没有"同一天"可言：宁可缺因子，不拿别日的因子来凑。
        hfq = hfq_by_day.get(day) if day is not None else None
        out.append(
            BarDraft(
                source=source,
                symbol=symbol,
                trade_date=day,
                open=to_float(row.get("open")),
                high=to_float(row.get("high")),
                low=to_float(row.get("low")),
                close=close,
                volume=to_float(row.get("volume")),
                amount=to_float(row.get("amount")),
                adj_factor=_factor(close, hfq),
                is_suspended=False,
            )
        )
    return out
