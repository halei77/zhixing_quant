"""停牌区间：双源行 → `Interval`，并集合并去重（01 Step 1 主数据；04 §二 R006 豁免通道）。

两个源（实测 2026-09-24）：

- **东财** `stock_tfp_em`：**区间**形态（`停牌时间 → 停牌截止时间`，含头含尾），全市场含
  北交所、回溯到 2016。单用它只覆盖 71% 的 R006 FATAL 票（50 只抽样重抓）。
- **百度** `news_trade_notify_suspend_baidu`：按日事件、自带 `复牌时间`，展开成
  `[停牌时间, 复牌时间 − 1]`——复牌**当天**有分钟行，所以 end 必须减一（002762 实测：
  停牌 2025-04-23 → 复牌 2025-04-24，分钟盘只缺 04-23）。EM 漏掉的日子它有，合并后
  抽样 49/49 = 100%。两个都要。

**合并是硬要求，不是优化**：`SecurityMaster._reject_overlap` 同代码区间**相接即抛
`MasterConflict`**，而 EM ∩ 百度 的同一次停市必然重叠、百度自己就有首尾相接的两条
（603159：06-15→06-16 与 06-16→06-23）。不合并就 `read_master` 全线退出码 2
（zx-daily / zx-site 六个入口一起断）。

百度行混有港/三板代码（`01428,01939,838879…`）：**不能盲目 zfill(6)**——`'01939'→
'001939'` 会被 `domain.symbol` 的 `001` 前缀当成主板 A 股。按原始 6 位数字 +
`交易所代码/证券类型/市场类型` 过滤；三板码 `872707` 长得像北交所（`87` 前缀），只有
交易所列分得开。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import timedelta

from zhixing_quant.domain.security import Interval
from zhixing_quant.domain.symbol import UnknownCode, board_of
from zhixing_quant.sources.rows import pick, to_date

#: 百度行里属于沪深京 A 股的交易所列。`NQ`（三板）的 `市场类型` 也是 `ab`，单靠它分不开；
#: `872707` 这类三板码按前缀会被 `board_of` 判成北交所，只有交易所列拦得住。
A_SHARE_EXCHANGES = frozenset({"SH", "SZ", "BJ"})


def _a_share_code(raw: object) -> str | None:
    """原始代码 → 合法 A 股 6 位码；港/三板/短码一律拒。

    拒 5 位码是防 zfill 陷阱：`'01428'.zfill(6)` = `'001428'` 落在 `001` 主板前缀上，
    一条港股停牌会变成"某只主板票当天停牌"，R006 就此豁免掉一只从没停过牌的 A 股。
    """
    text = str(raw or "").strip()
    if len(text) != 6 or not text.isdigit():
        return None
    try:
        board_of(text)
    except UnknownCode:
        return None  # B股 900xxx / 表外代码：A 股门禁对它们没有判据
    return text


def intervals_from_em(rows: Sequence[Mapping[str, object]]) -> tuple[Interval, ...]:
    """东财 `stock_tfp_em` 行 → 区间，**含头含尾**（*ST高鸿 000851 佐证：截止 2025-11-10 /
    预计复牌 2025-11-11）。`停牌截止时间` 缺失（实测 10/865）按持续中（`end=None`）。

    起始日解析不出来的行丢掉：没有起点就摆不出区间，而少一段区间的后果是 R006 照旧计
    缺失（fail-closed），不会悄悄放行。
    """
    out: list[Interval] = []
    for row in rows:
        code = _a_share_code(pick(row, "代码", "证券代码", "code"))
        if code is None:
            continue
        start = to_date(pick(row, "停牌时间"))
        if start is None:
            continue
        out.append(Interval(code, start, to_date(pick(row, "停牌截止时间"))))
    return tuple(out)


def intervals_from_baidu(rows: Sequence[Mapping[str, object]]) -> tuple[Interval, ...]:
    """百度按日事件行 → 区间：`[停牌时间, 复牌时间 − 1]`；`复牌时间` 为 NaN/缺 → 当日一段
    （多为盘中临时停牌）。按 `交易所代码 ∈ SH/SZ/BJ` + 证券/市场类型 + 6 位码三重过滤。
    """
    out: list[Interval] = []
    for row in rows:
        code = _a_share_code(pick(row, "股票代码"))
        if code is None:
            continue
        exchange = str(pick(row, "交易所代码") or "").strip().upper()
        if exchange not in A_SHARE_EXCHANGES:
            continue
        if str(pick(row, "证券类型") or "").strip().lower() != "stock":
            continue
        if str(pick(row, "市场类型") or "").strip().lower() != "ab":
            continue
        start = to_date(pick(row, "停牌时间"))
        if start is None:
            continue
        resume = to_date(pick(row, "复牌时间"))  # 浮点 NaN / "nan" / 空串都落到 None
        # resume 缺 → 盘中临时停牌，就停在当天；有 → 复牌日本身不算停牌（end = 复牌 − 1）
        end = start if resume is None else max(resume - timedelta(days=1), start)
        out.append(Interval(code, start, end))
    return tuple(out)


def merge_intervals(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """同代码的相接/重叠/包含区间并成一段（并集合并）；**相邻不相接的不合并**；空段丢弃。

    「相接」必须合并：`_reject_overlap` 判的是 `cur.start <= prev.end`，首尾相接也算冲突
    ——百度自己就产得出这种（603159 的两条）。`end=None`（持续中）吞掉该代码之后的一切。
    """
    by_code: dict[str, list[Interval]] = {}
    for item in intervals:
        if item.end is not None and item.end < item.start:
            continue  # 空段/倒挂段：摆不出区间，丢掉（少豁免 = fail-closed 方向）
        by_code.setdefault(item.code, []).append(item)
    merged: list[Interval] = []
    for code in sorted(by_code):
        spans = sorted(by_code[code], key=lambda x: (x.start, x.end is None, x.end or x.start))
        current = spans[0]
        for nxt in spans[1:]:
            horizon = current.end  # None = 持续中，吞掉后面所有
            if horizon is None or nxt.start <= horizon:
                # 任一侧持续中 → 合并后仍持续中；否则取两端较晚的 end
                end = None if current.end is None or nxt.end is None else max(current.end, nxt.end)
                current = Interval(code, current.start, end)
            else:
                merged.append(current)
                current = nxt
        merged.append(current)
    return tuple(merged)
