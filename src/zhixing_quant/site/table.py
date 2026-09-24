"""K线表：06 §六 的格式口径，加上 ADR-0011 决定 3 那句自证口径。

渲染只认字段清单、不认 dataset：日线没有 `ts`，时间列自动退化成日期，所以这里没有"这是
日线还是分钟线"的开关要维护（与 `domain.bar.stamp_of` 同一手法）。

口径那一行是**表的一部分**，不是注释：大模型只看得到这张表里的数字，缺一句"后复权"就足以
让它把 8.88 倍的那个价当成现价引用出去。所以它由代码跟着配置生成，模板作者删不掉。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from zhixing_quant.domain.bar import Bar, stamp_of
from zhixing_quant.site.templates import FUNDAMENTAL_HEAD, Adjust, Format

#: 三种口径各自那句话（ADR-0011 决定 3）。不带 markdown 强调：同一段文字要同时进 Markdown
#: 表的引用行与 CSV 的注释行，`**` 在后者里只会变成噪音。
ADJUST_LABELS: dict[Adjust, str] = {
    "raw": "不复权：价即行情软件上的价，但除权日会呈现为一根真实的下跌",
    "backward": "后复权：趋势可读，这些数不是现价",
    "forward": "前复权：基准是本段末日，末日那天的价即现价",
}

#: 列名 → 表头。中文表头对大模型更友好，也更省 token（一列一个词，不用它再猜 `o/h/l/c`）。
#: `time` 不在模板的可选字段里（06 §四），它是每张表的第零列，选不掉。
HEADERS: dict[str, str] = {
    "time": "时间",
    "open": "开盘",
    "high": "最高",
    "low": "最低",
    "close": "收盘",
    "volume": "成交量(股)",
    "amount": "成交额(元)",
    "turnover": "换手率(%)",
}

#: 06 §六：价格 2 位小数、量额取整。换手率在 `_cell` 里单独走两位（它是比率不是钱）。
PRICE_FIELDS = ("open", "high", "low", "close")


class IncompleteComponent(ValueError):
    """组件要的数据没给全：宁可拒生成，也不给大模型一张"看起来完整"的缺列表。"""


def render(
    bars: Sequence[Bar],
    *,
    format: Format,
    fields: Sequence[str],
    adjust: Adjust,
    float_shares: float | None = None,
) -> str:
    """一张K线表：口径行 + 表头 + 正序数据行。

    `float_shares` 只有请求 `turnover` 时才用得上（缺它就拒，见 ADR-0011 决定 5）。
    """
    if not bars:
        raise IncompleteComponent("一张都没有的K线表不进提示词：那是组件没取到数，不是没有涨跌")
    symbols = {bar.code for bar in bars}
    if len(symbols) > 1:
        raise IncompleteComponent(
            f"这张表混了 {len(symbols)} 只票（{'、'.join(sorted(symbols))}）："
            "一期不做多股票对比（06 §九）"
        )
    columns: tuple[str, ...] = ("time", *fields)
    denominator = _denominator(columns, float_shares)
    titles = tuple(HEADERS[column] for column in columns)
    rows = [[_cell(bar, column, denominator) for column in columns] for bar in _ordered(bars)]
    if format == "csv":
        # 紧凑模式：没有分隔行、没有引用块，表头之外一行一根（06 §六 的"token 紧张时启用"）。
        compact = [f"# 价格口径：{ADJUST_LABELS[adjust]}", ",".join(titles)]
        compact.extend(",".join(row) for row in rows)
        return "\n".join(compact)
    lines = [
        f"> 价格口径：{ADJUST_LABELS[adjust]}",
        "",
        f"| {' | '.join(titles)} |",
        f"|{'---|' * len(titles)}",
    ]
    lines.extend(f"| {' | '.join(row)} |" for row in rows)
    return "\n".join(lines)


#: 参考表的字段 → 表头（ADR-0016：共享表头字典，K线表的 `HEADERS` 与这张各自一份）。
#: 比率两位、金额/量额取整、空值渲染为 "—"（源未提供 ≠ 数为零——禁 0 填充的渲染侧）。
#: 涨跌停与指数的列在这张字典里补：它们同走 `render_valuation` 这一个渲染入口，
#: 表头与口径行跟着** dataset **走，不另立第三套。
VALUATION_HEADERS: dict[str, str] = {
    "time": "时间",
    "close": "收盘",
    "open": "开盘",
    "high": "最高",
    "low": "最低",
    "volume": "成交量(手)",
    "amount": "成交额(千元)",
    "pe": "PE",
    "pe_ttm": "PE(TTM)",
    "pb": "PB",
    "ps_ttm": "PS(TTM)",
    "dv_ttm": "股息率(%)",
    "total_mv": "总市值(万元)",
    "circ_mv": "流通市值(万元)",
    "turnover_rate": "换手率(%)",
    "up_limit": "涨停价(元)",
    "down_limit": "跌停价(元)",
}

VALUATION_LABEL = (
    "估值口径：不复权收盘；pe/pb/ps 为倍数，dv_ttm 与换手率为 %，"
    "total_mv/circ_mv 为万元，来源转接源 rds；'—' 表示源未提供"
)

#: 每张参考表自己的口径行（ADR-0016 决定 2 的延伸：接一张表就配一句，代码生成、模板删不掉）。
#: 单位抄的是源给的量纲：stk_limit 是不复权绝对价（ADR-0015），index_daily 的 vol=手、
#: amount=千元（`sources.relay.tables.IndexDailyRow` 2026-09-22 实测注记）。
STK_LIMIT_LABEL = (
    "涨跌停口径：up_limit/down_limit 为当日涨跌停绝对价（元，不复权），"
    "来源转接源 rds；'—' 表示源未提供"
)
INDEX_DAILY_LABEL = (
    "指数日线口径：点位为不复权指数值，成交量为手、成交额为千元（源单位），"
    "来源转接源 rds；'—' 表示源未提供"
)

#: dataset → 口径行。`forecast` 不在列：事件型表走 `render_forecast` 自己那句
#: （`FORECAST_LABEL`），硬塞进这张表等于让它按序列型的形状被渲染。
REFERENCE_LABELS: dict[str, str] = {
    FUNDAMENTAL_HEAD: VALUATION_LABEL,
    "daily_basic": VALUATION_LABEL,
    "stk_limit": STK_LIMIT_LABEL,
    "index_daily": INDEX_DAILY_LABEL,
}


def reference_label(dataset: str) -> str:
    """这张参考表的口径行。**没有登记就响**：口径不许编（ADR-0011 决定 4 手法）。"""
    try:
        return REFERENCE_LABELS[dataset]
    except KeyError:
        raise ValueError(
            f"参考表 {dataset!r} 没定过口径行：一句都没有就渲染，等于交出去一张不自证单位的表"
        ) from None


def render_valuation(
    rows: Sequence[Any],
    *,
    fields: Sequence[str],
    dataset: str,
    format: Format = "markdown",
) -> str:
    """参考表（序列型）→ 表（ADR-0016）。口径行按 dataset 取、由代码给，模板删不掉。

    `dataset` 必传：同一套列渲染成哪种口径是**取数侧声明的**，渲染层不从行形状去猜——
    猜错的表现是「数都对、单位没说」，那正是口径行要防的东西。
    """
    label = reference_label(dataset)
    columns = ["time", *fields]
    headers = [VALUATION_HEADERS.get(c, c) for c in columns]
    body_rows = []
    for row in rows:
        cells = [row.trade_date.isoformat()]
        cells.extend(_metric_cell(name, getattr(row, name, None)) for name in fields)
        body_rows.append(cells)
    if format == "csv":
        lines = [f"# {label}", ",".join(headers)]
        lines += [",".join(r) for r in body_rows]
        return "\n".join(lines)
    lines = [f"> {label}", "", f"| {' | '.join(headers)} |", f"|{'---|' * len(headers)}"]
    lines += [f"| {' | '.join(row)} |" for row in body_rows]
    return "\n".join(lines)


def render_fundamental_head(
    *,
    name: str | None,
    code: str,
    close_date: date | None,
    close: float | None,
    row: Any,
    fields: Sequence[str],
    format: Format = "markdown",
) -> str:
    """最新基本面固定头（ADR-0022 决定 1）：「口径行 + 两列表 + —」，估值表家族的键值形态。

    与 `render_valuation` 共享同一句口径行（`reference_label`）、同一本表头字典与同一条
    空值/格式纪律（`_metric_cell`）——ADR-0016 后果里"共享表头字典与格式常量"的那条线，
    不是第三套渲染入口。身份三行（名称/代码/收盘日期）选不掉，与K线表的 `time` 列同理。

    `close`/`close_date` 来自日线序列末行（不复权，ADR-0016 决定 5 两行口径各行其表），
    **不**取 `row.close`——两者不一致时这里印的是日线那个数，测试钉住这条来源。
    """
    label = reference_label(FUNDAMENTAL_HEAD)
    body: list[list[str]] = [
        ["名称", name if name else "—"],
        ["代码", code],
        ["收盘日期", close_date.isoformat() if close_date else "—"],
    ]
    for field in fields:
        value = close if field == "close" else getattr(row, field, None)
        body.append([VALUATION_HEADERS.get(field, field), _metric_cell(field, value)])
    if format == "csv":
        lines = [f"# {label}", "项目,数值"]
        lines += [f"{item},{cell}" for item, cell in body]
        return "\n".join(lines)
    lines = [f"> {label}", "", "| 项目 | 数值 |", "|---|---|"]
    lines += [f"| {item} | {cell} |" for item, cell in body]
    return "\n".join(lines)


def _metric_cell(name: str, value: object) -> str:
    """参考表的一个格子：null →「—」（禁 0 填充），市值与量额取整带千分位，其余两位小数。

    量额取整带千分位而不学K线表的裸整数：这两列只出现在 `index_daily`（手/千元量级到 1e9），
    「1007995610」与「1,007,995,610」差的不是 token，是可读性；06 §六 说的"量额取整"两者都满足。
    """
    if value is None:
        return "—"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        # 字段清单里不该有非数值（装载时按 dataset 校验过），真来了也不假装它是 0。
        return "—"
    if name in ("total_mv", "circ_mv", "volume", "amount"):
        # 万元的量级到 1e8，科学计数法会让大模型把 1.9 万亿读丢
        return f"{value:,.0f}"
    return f"{value:.2f}"


FORECAST_LABEL = (
    "业绩预告口径：公告日与报告期为行身份，净利区间单位万元（源为元，此处换算），"
    "只含公告日不晚于截止日的预告——点时可见性（03-L4 的未来函数禁令在提示词侧的落点）"
)


def render_forecast(rows: Sequence[Any], *, format: Format = "markdown") -> str:
    """forecast 参考表 → 业绩预告事件列表（ADR-0016 的表形态，事件型而非序列型）。

    点时可见性在这里兑现：rows 由查询层按 ann_date ≤ 截止日筛过，渲染层不放宽——
    一条"下个月才公告"的预告出现在提示词里，就是给大模型递未来函数。
    """
    headers = ["公告日", "报告期", "类型", "净利下限(万元)", "净利上限(万元)", "摘要"]
    body_rows = []
    for row in rows:
        lo = "—" if row.net_profit_min is None else f"{row.net_profit_min / 1e4:,.0f}"
        hi = "—" if row.net_profit_max is None else f"{row.net_profit_max / 1e4:,.0f}"
        body_rows.append(
            [row.ann_date.isoformat(), row.end_date.isoformat(), row.type, lo, hi, row.summary[:80]]
        )
    if format == "csv":
        lines = [f"# {FORECAST_LABEL}", ",".join(headers)]
        lines += [",".join(r) for r in body_rows]
        return "\n".join(lines)
    lines = [f"> {FORECAST_LABEL}", "", f"| {' | '.join(headers)} |", f"|{'---|' * len(headers)}"]
    lines += [f"| {' | '.join(row)} |" for row in body_rows]
    return "\n".join(lines)


def _denominator(columns: Sequence[str], float_shares: float | None) -> float:
    """换手率的分母。没请求这一列时返回 0.0（无人使用），请求了就必须是正数。

    判"给了个 0 或负数"与判"没给"是同一条：`volume / 0` 会炸出一个 ZeroDivisionError，
    而炸在算术里与炸在配置里，后者是一句话、前者是一段栈。
    """
    if "turnover" not in columns:
        return 0.0
    if float_shares is None or float_shares <= 0:
        raise IncompleteComponent(
            "模板请求了换手率，但没有流通股本这个分母：换手率算不出来（ADR-0011 决定 5）"
        )
    return float_shares


def _ordered(bars: Sequence[Bar]) -> list[Bar]:
    """时间正序（06 §六：便于趋势阅读）。查询层已经升序，这里再排一次是为了让"正序"成为这张表
    的性质，而不是"上游恰好给了有序数组"的巧合——单测直接喂一份乱序数组进来时，表也必须是正序的。
    """
    return sorted(bars, key=lambda bar: stamp_of(bar.trade_date, bar.ts))


def _cell(bar: Bar, column: str, denominator: float) -> str:
    if column == "time":
        return _stamp(bar)
    if column == "turnover":
        return f"{bar.volume * 100.0 / denominator:.2f}"
    value: float = getattr(bar, column)
    return f"{value:.2f}" if column in PRICE_FIELDS else f"{value:.0f}"


def _stamp(bar: Bar) -> str:
    """日线只有日期；分钟线带时刻（`ts` 是这根的收盘时刻，ADR-0009 决定 2）。"""
    return bar.trade_date.isoformat() if bar.ts is None else bar.ts.strftime("%Y-%m-%d %H:%M")
