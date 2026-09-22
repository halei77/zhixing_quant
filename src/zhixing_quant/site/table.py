"""K线表：06 §六 的格式口径，加上 ADR-0011 决定 3 那句自证口径。

渲染只认字段清单、不认 dataset：日线没有 `ts`，时间列自动退化成日期，所以这里没有"这是
日线还是分钟线"的开关要维护（与 `domain.bar.stamp_of` 同一手法）。

口径那一行是**表的一部分**，不是注释：大模型只看得到这张表里的数字，缺一句"后复权"就足以
让它把 8.88 倍的那个价当成现价引用出去。所以它由代码跟着配置生成，模板作者删不掉。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from zhixing_quant.domain.bar import Bar, stamp_of
from zhixing_quant.site.templates import Adjust, Format

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


#: 估值表（ADR-0016）：参考表 daily_basic 的字段 → 表头。比率两位、金额取整、
#: 空值渲染为 "—"（源未提供 ≠ 数为零——银行/保险的科目常态，禁 0 填充的渲染侧）。
VALUATION_HEADERS: dict[str, str] = {
    "time": "时间",
    "close": "收盘",
    "pe": "PE",
    "pe_ttm": "PE(TTM)",
    "pb": "PB",
    "ps_ttm": "PS(TTM)",
    "dv_ttm": "股息率(%)",
    "total_mv": "总市值(万元)",
    "circ_mv": "流通市值(万元)",
    "turnover_rate": "换手率(%)",
}

VALUATION_LABEL = (
    "估值口径：不复权收盘；pe/pb/ps 为倍数，dv_ttm 与换手率为 %，"
    "total_mv/circ_mv 为万元，来源转接源 rds；'—' 表示源未提供"
)


def render_valuation(
    rows: Sequence[Any],
    *,
    fields: Sequence[str],
    format: Format = "markdown",
) -> str:
    """daily_basic 参考表 → 估值表（ADR-0016）。口径行由代码给，模板删不掉（决定 4 同款）。"""
    columns = ["time", *fields]
    headers = [VALUATION_HEADERS.get(c, c) for c in columns]
    body_rows = []
    for row in rows:
        cells = [row.trade_date.isoformat()]
        for name in fields:
            value = getattr(row, name, None)
            if value is None:
                cells.append("—")
            elif name in ("total_mv", "circ_mv"):
                # 万元的量级到 1e8，科学计数法会让大模型把 1.9 万亿读丢
                cells.append(f"{value:,.0f}")
            else:
                cells.append(f"{value:.2f}")
        body_rows.append(cells)
    if format == "csv":
        lines = [f"# {VALUATION_LABEL}", ",".join(headers)]
        lines += [",".join(r) for r in body_rows]
        return "\n".join(lines)
    lines = [f"> {VALUATION_LABEL}", "", f"| {' | '.join(headers)} |", f"|{'---|' * len(headers)}"]
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
