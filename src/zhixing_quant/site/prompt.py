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
from datetime import date, timedelta
from pathlib import Path

from zhixing_quant import config
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.site import table, templates, tokens
from zhixing_quant.site.compose import Section, assemble
from zhixing_quant.site.templates import Selection, Template
from zhixing_quant.sources.akshare.master import read_master
from zhixing_quant.storage import layout
from zhixing_quant.storage.query import read_bars
from zhixing_quant.storage.tables import read_table

#: `minute_` 这个前缀从 `layout.MINUTE_5` 反推，不再抄一遍字符串：分钟线加一个周期（ADR-0009
#: 决定 7 那个目录）时，这一层不需要第二处改动。
_MINUTE = f"{layout.MINUTE_5.rsplit('_', 1)[0]}_"

#: 大盘基准：模板里的 `index_daily` 段读**这只**指数，不是当前个股。个股代码与指数代码的
#: 六位空间重叠（000001 既是平安银行也是上证指数），不换 symbol 就会把个股名字下不存在的
#: 指数行全读成空。钉死上证指数——民间"大盘"的默认所指，`search.INDEXES` 名册首行同一口径；
#: 小标题把名字与代码一起印出来，模型看得到基准是哪条（ADR-0012 决定 4：标题不编）。
BENCHMARK = "000001.SH"
BENCHMARK_NAME = "上证指数"

#: `forward_pe` 读 report_rc 全史的左界：实测最早研报 2019-03-29 起（2026-09-25 窗口探测），
#: 1990 是"早于任何 A 股研报史"的兜底——读到的都是盘上真有的分区，空年份直接跳过。
_REPORT_RC_HISTORY_START = date(1990, 1, 1)


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


def read_kline(
    code: str, *, days: int, kind: str = "stock", root: Path | None = None
) -> list[dict[str, object]]:
    """K线查询（站点 2026-09-22 用户要求"能查个股与指数"）。**不复权口径**——查行情的
    人要看的是当日真实价，与提示词模板的后复权口径（ADR-0011 决定 3）是两个用途两个口径。

    `kind="index"` 时 `code` 是带市场后缀的指数代码（000001.SH，搜索结果的 kind=index
    行给的就是它），数据出自参考表 `index_daily`（ADR-0015）；个股出自干净区日线。
    返回按日期升序的 dict 序列，直接是 /api/kline 的 payload 形状。
    """
    end = date.today()
    start = end - timedelta(days=days * 2 + 10)  # 日历天多留一倍：节假日吃掉近一半
    if kind == "index":
        rows = read_table("index_daily", code, start, end, root=root)
        return [
            {
                "date": row.trade_date.isoformat(),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "amount": row.amount,
            }
            for row in rows[-days:]
        ]
    bars = read_bars(code, start, end, adjust="raw", dataset=layout.DAILY, root=root)
    return [
        {
            "date": bar.trade_date.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "amount": bar.amount,
        }
        for bar in bars[-days:]
    ]


def _num(value: object) -> float | None:
    """把 bar 里的数值字段读成 float；`bool` 不算数（`isinstance(True, int)` 为真）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _price(value: object) -> str:
    number = _num(value)
    return "—" if number is None else f"{number:.2f}"


def quote_of(bars: list[dict[str, object]]) -> dict[str, object] | None:
    """行情面板那一行的展示值。**涨跌幅是口径、金额是格式化，两样都在服务端做完**
    （ADR-0013 决定 5、ADR-0018 决定 4、docs/11 §六-2）——壳只搬不改，不许自己再推一遍。

    不足两根、或昨收非正时涨跌幅给 `—`（不编一个 0 出来）；空列表返回 None，前端不画那一行。
    """
    if not bars:
        return None
    last = bars[-1]
    close = _num(last.get("close"))
    if close is None:
        return None
    prev = _num(bars[-2].get("close")) if len(bars) > 1 else None
    change = None if prev is None or prev <= 0 else (close - prev) / prev * 100
    volume = _num(last.get("volume"))
    return {
        "date": last.get("date"),
        "close": f"{close:.2f}",
        "change_pct": "—" if change is None else f"{change:+.2f}%",
        "up": change is None or change >= 0,
        "open": _price(last.get("open")),
        "high": _price(last.get("high")),
        "low": _price(last.get("low")),
        "volume": "—" if volume is None else f"{round(volume):,}",
    }


def title_of(dataset: str) -> str:
    """一段数据的小标题。周期就是名字里那个数，第二份"盘上有哪几个 dataset"的名单不写。"""
    if dataset == layout.DAILY:
        return "日K"
    if dataset.startswith(_MINUTE):
        return f"{dataset[len(_MINUTE) :]} 分K"
    if dataset in templates.TABLE_DATASETS:
        # 参考表（ADR-0016）：小标题由表自己的中文名给，不编。大盘那条把指数名与代码一起
        # 印出来——不写清是哪只指数，模型会拿它当任意大盘（BENCHMARK 的注释是同一条理由）。
        return {
            templates.FUNDAMENTAL_HEAD: "最新基本面",
            "daily_basic": "估值（每日指标）",
            "forecast": "业绩预告",
            "stk_limit": "涨跌停价",
            "index_daily": f"大盘（{BENCHMARK_NAME} {BENCHMARK}）",
            "forward_pe": "远期PE（逐日点时）",
        }.get(dataset, dataset)
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


#: 一个组件在区间里一行都没有时的正文。**不给空表**——空表会被大模型读成"那天没涨跌"——
#: 只给一句"这里没有数"，让它按反幻觉第 3 条写「数据未提供」（table.IncompleteComponent
#: 那条原顾虑原样保留，只是从"拒掉整条"改成"出一节交代"）。
NO_DATA_NOTE = (
    "本节无数据：该区间在盘上一行都没有——是组件没取到数，不是没有变化。\n"
    "下面不提供任何数字；判断若需要这部分数据，明确写「数据未提供」。"
)


def _name_of(code: str) -> str | None:
    """证券简称：主数据快照的 listings（06 §四"基本信息"）。查无此码给 None，渲染成「—」。

    与壳（`api.py`）那份名单同源——都出自 `read_master()`，不另立第二份答案；这里是
    固定头取数的落点，读盘收在组合根（ADR-0012 决定 1）。
    """
    for listing in read_master().listings:
        if listing.code == code:
            return listing.name
    return None


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

    **一个组件一行都没有** → 那一节出 `NO_DATA_NOTE` 交代，不给空表、也不拖垮整条：5565 只
    票里只有分钟采集池那几只有 minute_*，`短期投资` 四段里三段空就整条拒的话，全市场只剩池内
    五票能用默认模板（2026-09-24 实测 300308 就是这么变成"没数据"的）。**每个组件都空** →
    照旧拒（`table.IncompleteComponent`）：一份数字都没有的提示词正是 06 §一 要防的那种东西。

    **固定头**（ADR-0022 决定 1）：数据选择先过 `templates.with_always_data`——经 YAML 装载
    的模板已在 `parse` 里合过（这里原样跳过），直构的 `Template`（自定义组合，ADR-0017）在
    这里补上，两条路殊途同归到"每条 ready 模板都带最新基本面"。补的是选择不是正文，取数照旧
    走下面的逐段循环、空了照旧出交代。
    """
    if template.status != "ready":
        raise TemplateNotReady(f"「{template.name}」要的数据组件还没落地：{template.waiting_on}")
    sections: list[Section] = []
    with_data = 0
    for selection in templates.with_always_data(template.data):
        start, end = window(calendar, as_of, selection.days)
        if selection.dataset == templates.FUNDAMENTAL_HEAD:
            # 固定头（ADR-0022 决定 1）：快照 = 各序列在窗口内的最新可见行，生成时现拼，
            # 不落派生表——落盘会造第二个「最新」事实源。daily_basic 那天没行 → 整节交代
            # （ADR-0020），不给一张只有表头的空表；日线缺行只让收盘/日期两格变「—」。
            basic = read_table("daily_basic", code, start, end, root=config.parquet_dir())
            if not basic:
                sections.append(Section(title=heading(selection, 0), body=NO_DATA_NOTE))
                continue
            bars = read_bars(
                code, start, end, adjust="raw", dataset=layout.DAILY, root=config.parquet_dir()
            )
            latest = bars[-1] if bars else None
            with_data += 1
            sections.append(
                Section(
                    title=heading(selection, 1),
                    body=table.render_fundamental_head(
                        name=_name_of(code),
                        code=code,
                        close_date=None if latest is None else latest.trade_date,
                        close=None if latest is None else latest.close,
                        row=basic[-1],
                        fields=selection.fields or (),
                        format=template.format,
                    ),
                )
            )
            continue
        if selection.dataset == "forecast":
            # 事件型表（ADR-0016）：渲染列固定，行按 ann_date 点时筛——未来函数禁令的落点。
            rows = [
                row
                for row in read_table("forecast", code, start, end, root=config.parquet_dir())
                if row.ann_date <= as_of
            ]
            if not rows:
                sections.append(Section(title=heading(selection, 0), body=NO_DATA_NOTE))
                continue
            with_data += 1
            sections.append(
                Section(
                    title=heading(selection, len(rows)),
                    body=table.render_forecast(rows, format=template.format),
                )
            )
            continue
        if selection.dataset == "forward_pe":
            # 远期 PE（ADR-0022 决定 2）：服务端现算、不落派生表——落盘会把 as_of 钉死在回填
            # 那晚。日线读**不复权**（close 与 daily_basic 口径行一致，不与后复权K混口径）；
            # report_rc 读**全史**：窗口起点那天的"当时最新"预测可能发布在窗口之前，只读窗内
            # 会把那段读成"无预测"。逐日点时对齐（report_date ≤ t）在纯层 `table.align_forward_pe`。
            bars_raw = read_bars(
                code, start, end, adjust="raw", dataset=layout.DAILY, root=config.parquet_dir()
            )
            forecasts = read_table(
                "report_rc", code, _REPORT_RC_HISTORY_START, end, root=config.parquet_dir()
            )
            aligned = table.align_forward_pe(bars_raw, forecasts)
            if not aligned or all(row.fwd_pe is None for row in aligned):
                # 窗口里一天都算不出远期 PE（没研报 / 都取不到未来财年分母）→ 整节交代
                # （ADR-0020），不给一张全「—」的表冒充"那段时间没有波动"。
                sections.append(Section(title=heading(selection, 0), body=NO_DATA_NOTE))
                continue
            with_data += 1
            sections.append(
                Section(
                    title=heading(selection, len(aligned)),
                    body=table.render_forward_pe(
                        aligned,
                        fields=selection.fields or (),
                        format=template.format,
                    ),
                )
            )
            continue
        if selection.dataset in templates.TABLE_DATASETS:
            # 参考表条目（ADR-0016）：不走 Bar 查询，也没有复权口径。大盘那只指数在这换
            # symbol——index_daily 的行按指数代码分区，拿个股代码去读必空（BENCHMARK 注释）。
            symbol = BENCHMARK if selection.dataset == "index_daily" else code
            rows = read_table(selection.dataset, symbol, start, end, root=config.parquet_dir())
            if not rows:
                sections.append(Section(title=heading(selection, 0), body=NO_DATA_NOTE))
                continue
            with_data += 1
            sections.append(
                Section(
                    title=heading(selection, len(rows)),
                    body=table.render_valuation(
                        rows,
                        fields=selection.fields or (),
                        dataset=selection.dataset,
                        format=template.format,
                    ),
                )
            )
            continue
        bars = read_bars(
            code,
            start,
            end,
            adjust=template.adjust,
            dataset=selection.dataset,
            root=config.parquet_dir(),
        )
        if not bars:
            sections.append(Section(title=heading(selection, 0), body=NO_DATA_NOTE))
            continue
        with_data += 1
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
    if with_data == 0:
        # 每一段都空：交出去就是"指令齐全、数字全靠编"，拒。消息沿用原句，两个既有测试
        # （test_a_request_nothing_can_answer_is_refused_not_emptied / api 的 300750 那条）
        # 判的就是这个形状。
        raise table.IncompleteComponent(
            "一张都没有的K线表不进提示词：那是组件没取到数，不是没有涨跌"
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
