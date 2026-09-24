"""K线表：06 §六 的格式口径，加上 ADR-0011 决定 3 那句自证口径。

渲染只认字段清单、不认 dataset：日线没有 `ts`，时间列自动退化成日期，所以这里没有"这是
日线还是分钟线"的开关要维护（与 `domain.bar.stamp_of` 同一手法）。

口径那一行是**表的一部分**，不是注释：大模型只看得到这张表里的数字，缺一句"后复权"就足以
让它把 8.88 倍的那个价当成现价引用出去。所以它由代码跟着配置生成，模板作者删不掉。
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from datetime import date
from typing import Any, NamedTuple

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
    "fwd_pe": "远期PE",
    "est_period": "预测报告期",
    "est_asof": "预测发布日",
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
    "forward_pe": (
        "远期PE口径：不复权收盘 ÷ 当日点时可见的预测EPS——取发布日不晚于当日的当时最新研报、"
        "面向最早未到期财年（quarter 年份 ≥ 当日年份的最前一个）、年内最全报告期（Q4 优先，"
        "Q1–Q3 是年内累计口径不当分母）；est_period=预测报告期、est_asof=该预测的发布日"
        "（必不晚于当日，点时可见性印在表里）；'—' 表示当日无可见预测，不回落PE(TTM)；"
        "来源转接源 rds report_rc"
    ),
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


class ForwardRow(NamedTuple):
    """`forward_pe` 表的一行：某交易日的远期 PE 及其所用预测的身份证（ADR-0022 决定 2）。

    `est_asof ≤ trade_date` 是这四列一起存在的理由——把点时可见性印在表里，模型看得到
    "22.4 倍是对哪天发布的哪个报告期的盈利说的"；缺测时三列全 None，渲染成「—」。
    """

    trade_date: date
    close: float
    fwd_pe: float | None
    est_period: str | None
    est_asof: date | None


def _quarter_parts(quarter: str) -> tuple[int, int]:
    """`2026Q4` → (2026, 4)。存储层已按此形状拒过行，这里再解不出就是接线 bug，当场响。"""
    shape_ok = (
        len(quarter) == 6 and quarter[4] == "Q" and quarter[:4].isdigit() and quarter[5] in "1234"
    )
    if not shape_ok:
        raise ValueError(f"quarter {quarter!r} 不是 YYYYQn：远期 PE 的财年选择解不出这个报告期")
    return int(quarter[:4]), int(quarter[5])


def align_forward_pe(bars: Sequence[Bar], forecasts: Sequence[Any]) -> list[ForwardRow]:
    """逐日点时对齐：窗口内每个交易日 t 各自取「report_date ≤ t 的当时最新预测」的 FY1
    分母，现算 fwd_pe（ADR-0022 决定 2 的完整规则，服务端现算、不落派生表）。

    `bars` 是窗口内的**不复权**日线（close 即行情软件上的价，与 daily_basic 口径行一致）；
    `forecasts` 是 `report_rc` 行（duck 类型：`.report_date/.org_name/.quarter/.eps`，
    来自干净区全史读出）。规则逐步：

    1. **可见集** = `report_date ≤ t` 的行。窗口前的老研报也算——窗口起点那天的"当时最新"
       可能发布于三个月前，只读窗口内会把那段历史读成"无预测"。
    2. **取当时最新**：可见集里 `report_date` 最大的那一天（"最新一份"，ADR 原文）。该日
       没有面向未来财年的行 → 当日「—」（不回落更早的旧报——那会把旧预期冒充成当前最新）。
    3. **FY1 = 财年 ≥ t.year 的最前一个**（"面向 t 时点的未来年度"）。等价于"财年末 ≥ t"：
       当年的全年盈利要到次年 3–4 月才披露完，当年就是面向未来的年度；跨年后自动换挡到下一年。
    4. **年内取最全报告期**：同财年多 quarter 行取 Qn 最大者（Q4=全年；实测 Q1–Q3 标签是
       **年内累计** EPS——国泰君安 20240403 一报四行 18.9/33.4/49.2/69.58 逐级累计——拿累计
       当分母会把 PE 放大数倍）。
    5. **同日多研报**（实测 41% 的 (date, quarter) 是同日多券商）：字典序取首个 org——
       发布日只有日期粒度、日内先后无从分辨，确定性即可，不发明一致预期口径（那要新 ADR）。
    6. **eps ≤ 0 的行不作分母**（负/零盈利的 PE 无意义）：同档还有正 EPS 的行就用它，全负
       则当日「—」。eps 为 null 的行在解析器就已拒行，不会出现在这里。
    7. **fwd_pe = close(t) ÷ eps**；任何一步取不到 → 三个伴随格全 None，渲染「—」。

    **未来函数禁令（03-L4）的落点**：本函数只依赖 `(t, 截至 t 的可见集)`，没有任何"as_of"
    参数——想用"as_of 那天的预测回填全窗"必须绕开本函数另写，而 tests/test_site_forward_pe
    的对值断言会把那种实现测红。
    """
    by_date: dict[date, list[Any]] = {}
    for row in forecasts:
        by_date.setdefault(row.report_date, []).append(row)
    publish_days = sorted(by_date)
    rows: list[ForwardRow] = []
    for bar in sorted(bars, key=lambda item: item.trade_date):
        t = bar.trade_date
        # 可见集的"最新发布日"：bisect 定位 ≤ t 的最后一个（O(log n)，全市场回填后每票几千行）
        cut = bisect.bisect_right(publish_days, t)
        if cut == 0:
            rows.append(ForwardRow(t, bar.close, None, None, None))
            continue
        latest = publish_days[cut - 1]
        picked = _fy1_of(by_date[latest], t)
        if picked is None or picked.eps <= 0:
            rows.append(ForwardRow(t, bar.close, None, None, None))
            continue
        rows.append(
            ForwardRow(
                t,
                bar.close,
                bar.close / picked.eps,
                picked.quarter,
                picked.report_date,
            )
        )
    return rows


def _fy1_of(day_rows: Sequence[Any], t: date) -> Any | None:
    """某发布日的行里挑 t 视角的 FY1 分母（规则 2–6），没有可用行给 None。"""
    future_years = sorted(
        {year for row in day_rows if (year := _quarter_parts(row.quarter)[0]) >= t.year}
    )
    if not future_years:
        return None
    fy1 = future_years[0]
    same_year = [row for row in day_rows if _quarter_parts(row.quarter)[0] == fy1]
    best_quarter = max(_quarter_parts(row.quarter)[1] for row in same_year)
    fullest = [row for row in same_year if _quarter_parts(row.quarter)[1] == best_quarter]
    usable = [row for row in fullest if row.eps > 0]
    if not usable:
        return None
    return min(usable, key=lambda row: row.org_name)


def render_forward_pe(
    rows: Sequence[ForwardRow],
    *,
    fields: Sequence[str],
    format: Format = "markdown",
) -> str:
    """`forward_pe` 序列 → 表（估值表家族的序列形态，ADR-0022 决定 2 的 fields 四列）。

    口径行由代码给（`reference_label`），模板删不掉；空行集拒——空表会被大模型读成
    "那段时间远期 PE 一直是 0"（`IncompleteComponent` 原顾虑，ADR-0020 说的空表不出在这里，
    空段由取数侧出 `NO_DATA_NOTE`）。`est_period`/`est_asof` 是字符串/日期列，走自己的
    格子函数——`_metric_cell` 对非数值一律「—」，会把预测期也吃掉。
    """
    if not rows:
        raise IncompleteComponent("一张都没有的远期PE表不进提示词：那是组件没取到数，不是没有变化")
    label = reference_label("forward_pe")
    headers = ["时间", *(VALUATION_HEADERS.get(name, name) for name in fields)]
    body_rows = [
        [row.trade_date.isoformat(), *(_forward_cell(name, row) for name in fields)] for row in rows
    ]
    if format == "csv":
        lines = [f"# {label}", ",".join(headers)]
        lines += [",".join(cells) for cells in body_rows]
        return "\n".join(lines)
    lines = [f"> {label}", "", f"| {' | '.join(headers)} |", f"|{'---|' * len(headers)}"]
    lines += [f"| {' | '.join(cells)} |" for cells in body_rows]
    return "\n".join(lines)


def _forward_cell(name: str, row: ForwardRow) -> str:
    """远期 PE 表的一个格子。缺测一律「—」——不是 0、更不是 pe_ttm（两条不同定义的线）。"""
    if name == "close":
        return f"{row.close:.2f}"
    if name == "fwd_pe":
        return "—" if row.fwd_pe is None else f"{row.fwd_pe:.2f}"
    if name == "est_period":
        return row.est_period or "—"
    if name == "est_asof":
        return row.est_asof.isoformat() if row.est_asof else "—"
    return "—"  # 字段清单装载时已校验，走到这里是绕过装载的直构调用——不假装它是数


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
