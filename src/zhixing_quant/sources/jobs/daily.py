"""每日盘后采集：抓取 → 门禁 → 干净区 → 日报（01 路线图 Step 2 第 4 项，验收 4）。

抓取与告警都是**参数**而不是本模块里的调用：故障注入（断网/源不可用）要能塞进重试路径，
真联网的那一层由调用方给。所以这里不 import akshare、不读环境变量、也不安装定时器——
改用户机器上的 cron 属于"先问"（05 Q1）。落盘同理是参数（`Store`）：真路径只有 CLI 知道，
库函数默认往数据根写等于任何没传 root 的测试都会污染生产的干净区。

两件事决定了批次的形状：

- **两日一批**。R007 的判据是"今开 vs 昨收"，昨收只能由批次里上一日的行现算，单日一批
  等于这条规则永不触发且永不说话（04 §二：R007 阈值与 R004 联动）。报告再用
  `GateOutcome.for_day` 裁回报告日，日报的分母仍是"那天"；**落盘裁不得**——上一日那一行
  也是今天抓到的证据，而断点续传时它少一次联网请求。
- **一只票一批**。`GateEngine` 本来就拒混批（`GateInconsistency`），按票分批还额外买到
  隔离性：一只票的网络失败不牵连其余几千只。同源多批由 `DataQualityReport.from_outcomes`
  并成一行（台账坑 #10）。落盘也按票一次：崩在中途时，已经写进去的就是已经写进去的，
  重跑靠 `store_bars` 的幂等补齐——这里没有"整批事务"那种做不到的保证要去假装提供。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path

from zhixing_quant import config
from zhixing_quant.domain.bar import Bar
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import SecurityMaster
from zhixing_quant.quality import daily_report, gate_config
from zhixing_quant.quality.engine import GateEngine, GateOutcome, QuarantinedRow
from zhixing_quant.quality.report import DataQualityReport
from zhixing_quant.sources.akshare import daily as akshare_daily
from zhixing_quant.sources.rows import Pair, SourceSchemaError
from zhixing_quant.storage.quarantine import QuarantineReport
from zhixing_quant.storage.write import WriteReport

#: 抓取边界：一只票、一个闭区间，返回 (不复权, 后复权) 两帧。
Fetcher = Callable[[str, date, date], Pair]
#: 告警边界：标题 + 正文。iOS 推送通道接在这里，本模块只调用不实现。
Alert = Callable[[str, str], None]
#: 落盘边界：一批门禁放行的行 → 落盘的账。生产上就是 `storage.write.store_bars`。
Store = Callable[[Sequence[Bar]], WriteReport]
#: 隔离区落盘边界：一批被拒收的行 + 它们所属的那次运行（报告日）→ 落盘的账。
#: 运行日由这里给而不是由适配器闭包持有：补抓昨天的数据时，条目要落进**昨天**那个文件
#: （ADR-0008 代价二），而那一天只有任务知道。
QuarantineStore = Callable[[Sequence[QuarantinedRow], date], QuarantineReport]


@dataclass(frozen=True)
class Skipped:
    """重试之后仍然没拿下来的一只票。

    必须带原因上日报：不记的话"今天判了 4400 只"和"股票池 4430 只"的差就凭空消失了，
    而缺的那 30 只恰恰最可能是源在偷偷改口。
    """

    symbol: str
    reason: str


@dataclass(frozen=True)
class DayResult:
    day: date
    outcomes: tuple[GateOutcome, ...]
    report: DataQualityReport
    markdown: str
    skipped: tuple[Skipped, ...]
    pool: int
    landed: WriteReport
    quarantined: QuarantineReport

    @property
    def ok(self) -> bool:
        """这次运行有没有判出东西。空报告不能当"今天满分"。

        两种都不算判成：一只票都没产出批次（全网络失败），以及产出了批次却一行都没有
        （源今天给的是空表——引擎里 R006 的"本批无有效日期"就是它）。后者尤其要拦：
        它带着一个 FATAL 结果，`bool(outcomes)` 会为真，退出码于是替一个宕掉的源报成功。
        """
        return any(outcome.total for outcome in self.outcomes)

    @property
    def fatal(self) -> bool:
        """有没有批次被整批拒收。

        它不改变"运行成了没"——4430 只里有一只 FATAL 是那只票的事，全 FATAL 才是源的事。
        两者在数量上差 3 个数量级，却共用同一个 FATAL 标记，所以告警里必须带数量。
        """
        return any(outcome.has_fatal for outcome in self.outcomes)


def stdout_alert(title: str, body: str) -> None:
    """默认告警落标准输出：定时任务的日志就是它的收件箱。"""
    print(f"[ALERT] {title}\n{body}", flush=True)


def with_retry[T](
    fetch: Callable[[], T],
    symbol: str,
    *,
    attempts: int = 3,
    backoff: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    alert: Alert = stdout_alert,
) -> T | Skipped:
    """重试到 `attempts` 次；只在最后一次告警。抓回来的东西是什么形状由 `fetch` 说（`T`）。

    退避是 `backoff × 2ⁿ`：免费源的失败大多是限流，等固定秒数等于用同样的节奏再撞三次。
    最终失败返回 `Skipped` 而不是抛——一只票抓不到不该让另外几千只的判定作废，但也不能
    悄悄少掉，所以它带着原因回到日报上（验收 4 的"最终告警"就是这里响的）。
    """
    last = ""
    for attempt in range(attempts):
        try:
            return fetch()
        except Exception as exc:  # 抓取边界上什么都是网络层的错：限流、超时、半截 JSON
            last = f"{type(exc).__name__}: {exc}"
            if attempt == attempts - 1:
                alert(
                    f"抓取失败：{symbol}",
                    f"重试 {attempts} 次仍失败，最后错误：{last}；"
                    "该票今天的行不进干净区，也不进日报的分母。",
                )
            else:
                sleep(backoff * 2**attempt)
    return Skipped(symbol=symbol, reason=last)


def last_trading_day(calendar: TradingCalendar, today: date) -> date:
    """盘后任务的目标日：今天（若为交易日）或最近一个已过的交易日。

    判不出就抛。日历不覆盖今天时 `prev_trading_day` 会返回一个很久以前的日子，任务于是
    "成功地"抓了三个月前的数据，而日报看起来一切正常。
    """
    # `prev_trading_day` 是"严格早于"，所以今天本身是交易日时不能走它：那样 17:00 抓的会是昨天。
    day = today if calendar.is_trading_day(today) else calendar.prev_trading_day(today)
    if day is None or (today - day).days > 7:
        raise SourceSchemaError(
            f"日历判不出 {today} 附近的交易日（最近的是 {day}）：先重抓交易日历快照"
        )
    return day


def collect(
    day: date,
    symbols: Sequence[str],
    *,
    fetch: Fetcher,
    store: Store,
    quarantine: QuarantineStore,
    master: SecurityMaster,
    calendar: TradingCalendar,
    previous_days: int = 1,
    attempts: int = 3,
    backoff: float = 5.0,
    breaker: int = 20,
    sleep: Callable[[float], None] = time.sleep,
    alert: Alert = stdout_alert,
) -> tuple[list[GateOutcome], list[Skipped], WriteReport, QuarantineReport]:
    """抓 + 判 + 落一批票：返回逐票结果、没拿下来的票、进干净区的账、进隔离区的账。

    `previous_days` 是往回带几个交易日：1 天够 R007；Step 3 做时间连续性检查时要更多。
    日历判不出上一日（样本区间的第一桶）时按单日批处理，此时 R007 不判——这一点由
    分母不变来保证可见：少了昨收的行仍然在批里，只是没有偏差可算。

    `breaker` 是连败几只就收工：源整体宕掉时，4430 只 × 3 次 × 指数退避等于用十几个小时
    去撞一堵墙，而免费源会把这种撞法当作攻击。剩下的票照样进日报的失败清单，所以收工
    不是掩盖缺数，只是把"今天这个源不行"说得更早。

    两个落盘都不兜异常：炸了（磁盘满、权限、同批冲突）意味着"今天这批没进库"，让它冒出去
    由退出码说给定时任务，比记下几条 Skipped、再出一份看起来正常的日报好。

    每只票只有三个去处：判成的行进干净区与（可能的）隔离区，请求失败的进 `Skipped`，请求成功
    却零行的也进 `Skipped`——后者不该走门禁，理由写在 `REASON_NO_ROWS` 那段。
    """
    start = _window_start(calendar, day, previous_days)
    engine = GateEngine(gate_config.load(config.gate_config_file()), master, calendar)
    outcomes: list[GateOutcome] = []
    skipped: list[Skipped] = []
    written: list[WriteReport] = []
    recorded: list[QuarantineReport] = []
    consecutive = 0
    for position, symbol in enumerate(symbols):
        if consecutive >= breaker:
            left = symbols[position:]
            skipped += [Skipped(symbol=s, reason=REASON_BREAKER) for s in left]
            break
        grabbed = with_retry(
            partial(fetch, symbol, start, day),
            symbol,
            attempts=attempts,
            backoff=backoff,
            sleep=sleep,
            alert=alert,
        )
        if isinstance(grabbed, Skipped):
            skipped.append(grabbed)
            consecutive += 1
            continue
        consecutive = 0
        raw, hfq = grabbed
        drafts = akshare_daily.daily_drafts(raw, hfq, symbol=symbol)
        if not drafts:
            # 零只票不交给门禁：见 `REASON_NO_ROWS`。也不计入熔断——熔断数的是"请求在失败"，
            # 而请求成功了；整池皆空由 `DayResult.ok` 拦住，不会静悄悄。
            skipped.append(Skipped(symbol=symbol, reason=REASON_NO_ROWS))
            continue
        judged = engine.run(drafts)
        # 两个桶都按**整批**落盘，不裁成报告日：上一日那行也是今天抓到的证据——干净区因此少一次
        # 联网重抓，隔离区因此不会在"那天到底拒收了什么"上留一个查不到的空洞。裁剪只发生在报给
        # 日报的那一侧。
        written.append(store(judged.clean_zone))
        recorded.append(quarantine(judged.quarantined, day))
        outcomes.append(judged.for_day(day))
    return outcomes, skipped, sum_writes(written), sum_quarantines(recorded)


def sum_writes(reports: Sequence[WriteReport]) -> WriteReport:
    """逐票的落盘账并成一次运行的账。

    分区数可以直接相加：一只票一年一个文件，而 `store_bars` 每次只看见一只票，重复不了。
    """
    return WriteReport(
        partitions=sum(r.partitions for r in reports),
        added=sum(r.added for r in reports),
        repaired=sum(r.repaired for r in reports),
        rewritten=sum(r.rewritten for r in reports),
    )


def sum_quarantines(reports: Sequence[QuarantineReport]) -> QuarantineReport:
    """逐票的隔离账并成一次运行的账。

    与干净区不同，这里不能相加出一个"累计"：一天只有一个隔离区文件，各次调用的账都是对同一个
    文件的读写。相加有意义的是条目数与新记数，`rewritten` 因此只表示"改了几次文件"。

    `total` 是那个文件的行数，相加会变成"同一批行被数了 N 遍"：每只票落盘时读写的都是同一个
    文件，后一批看到的行数含着前一批。合批只增不减，所以取最大就是最终那份。
    """
    return QuarantineReport(
        entries=sum(r.entries for r in reports),
        recorded=sum(r.recorded for r in reports),
        rewritten=sum(r.rewritten for r in reports),
        total=max((r.total for r in reports), default=0),
    )


#: 熔断之后没去抓的票，在日报的失败清单上写这一句。它们不是"抓不到"而是"没试"，
#: 混进同一个原因里，看日报的人就会以为源只对其中一部分失败了。
REASON_BREAKER = "熔断：源连续失败，未再抓取"
#: 请求成功、帧也拿到了，里头的行是零只（000016 *ST康佳A 与 600825 在 2026-09-18 就是这样）。
#: 它既不是"抓取失败"（网络层没出错），也不是一批可判的数据：交给门禁会得到 R006 的整批 FATAL，
#: 而那条按 04 §三 要扣整源 40 分——一天 4430 只里有两只是停牌，说不上"这个源今天不可信"。
#: 所以它记在失败清单里，不记在健康分上。整池都空时 `DayResult.ok` 仍然为假，源宕了照样响。
REASON_NO_ROWS = "抓取成功但当日无行（停牌，或源漏了这只票）"


def _window_start(calendar: TradingCalendar, day: date, previous_days: int) -> date:
    cursor: date | None = day
    for _ in range(previous_days):
        cursor = calendar.prev_trading_day(cursor) if cursor is not None else None
    return cursor if cursor is not None else day


def publish(
    day: date,
    outcomes: Sequence[GateOutcome],
    *,
    landed: WriteReport,
    quarantined: QuarantineReport,
    skipped: Sequence[Skipped] = (),
    directory: Path | None = None,
    extra: str = "",
) -> tuple[DataQualityReport, str]:
    """渲染 + 归档 + 分数流水。打印留给调用方：日报的正文同时是定时任务的日志。

    `landed` 与 `quarantined` 是这次运行留下的两本账。日报要把它们各列一行：判成多少行说的是
    门禁，落进干净区多少行说的是"明天有没有数据可用"，拒收的条目落了几条说的才是 04 §四 那句
    "全量可查"到底成不成立——三件事可以各自出岔子。

    `extra` 是调用方追加的一节（分钟任务的对账），排在"没判成的票"之前。这里只接一段已经渲染好
    的 Markdown，不接"对账结果"这类任务专属的事实：日线没有对账，共享的渲染层一旦认识它就长出了
    两种日报。
    """
    reports = directory if directory is not None else config.reports_dir()
    scores = reports / "scores.csv"
    report = DataQualityReport.from_outcomes(day, outcomes)
    trends = {m.source: daily_report.history_for(scores, m.source, day) for m in report.metrics}
    body = daily_report.render(report, list(outcomes), trends, landed, quarantined)
    body += extra
    if skipped:
        body += _skipped_section(skipped)
    daily_report.archive(body, day, reports)
    daily_report.append_scores(report, scores)
    return report, body


def _skipped_section(skipped: Sequence[Skipped]) -> str:
    """没判成的票清单：列前 `DETAIL_TOP` 只加总数。三千只票全列出来等于把日报撑爆。

    三种原因是三种处置，所以各数各的：请求失败要查网络与限流，当日无行要查那只票是不是停牌
    （或源在漏票），熔断说明源整体不行。合成一句"失败 N 只"会让人去查错方向。
    """
    cap = daily_report.DETAIL_TOP
    untouched = sum(1 for item in skipped if item.reason == REASON_BREAKER)
    empty = sum(1 for item in skipped if item.reason == REASON_NO_ROWS)
    failed = len(skipped) - untouched - empty
    headline = f"- 重试后仍失败 {failed} 只"
    if empty:
        headline += f"，当日无行 {empty} 只"
    if untouched:
        headline += f"，熔断后没再抓取 {untouched} 只"
    lines = ["", "## 没判成的票", "", f"{headline}（都不计入上面的分母）："]
    lines += [f"- {item.symbol}：{item.reason}" for item in skipped[:cap]]
    if len(skipped) > cap:
        lines.append(f"- 其余 {len(skipped) - cap} 只同因，见任务日志")
    return "\n".join(lines) + "\n"


def run(
    day: date | None = None,
    *,
    fetch: Fetcher,
    store: Store,
    quarantine: QuarantineStore,
    master: SecurityMaster,
    calendar: TradingCalendar,
    now: datetime | None = None,
    symbols: Sequence[str] | None = None,
    limit: int | None = None,
    directory: Path | None = None,
    previous_days: int = 1,
    attempts: int = 3,
    breaker: int = 20,
    sleep: Callable[[float], None] = time.sleep,
    alert: Alert = stdout_alert,
) -> DayResult:
    """一次盘后运行：定日 → 抓取判定 → 落干净区与隔离区 → 发布日报。

    股票池默认取主数据当天的在册名单，`symbols` / `limit` 用来先在小范围跑通——全市场还是
    指数成分股是 01 路线图留给 Step 2 的待定口径，不在代码里替用户决定。
    """
    moment = now if now is not None else datetime.now(UTC)
    target = day if day is not None else last_trading_day(calendar, moment.date())
    pool = list(symbols) if symbols is not None else list(master.universe_on(target))
    if limit is not None:
        pool = pool[:limit]
    outcomes, skipped, landed, quarantined = collect(
        target,
        pool,
        fetch=fetch,
        store=store,
        quarantine=quarantine,
        master=master,
        calendar=calendar,
        previous_days=previous_days,
        attempts=attempts,
        breaker=breaker,
        sleep=sleep,
        alert=alert,
    )
    report, body = publish(
        target,
        outcomes,
        landed=landed,
        quarantined=quarantined,
        skipped=skipped,
        directory=directory,
    )
    result = DayResult(
        day=target,
        outcomes=tuple(outcomes),
        report=report,
        markdown=body,
        skipped=tuple(skipped),
        pool=len(pool),
        landed=landed,
        quarantined=quarantined,
    )
    if not result.ok:
        empty = sum(1 for item in result.skipped if item.reason == REASON_NO_ROWS)
        failed = len(result.skipped) - empty
        alert(
            f"今日无数据：{target}",
            f"股票池 {len(pool)} 只，一只都没判成（抓取失败 {failed} 只、"
            f"当日无行 {empty} 只）。日报已落盘，内容是空报告——"
            "别把它当「今天一切正常」。整池都是「当日无行」就是源在给空表，与网络无关。",
        )
    elif result.fatal:
        # 判成了，但有票整批被拦：那只票今天不进干净区，而"一只"与"全部"的处置完全不同，
        # 所以告警里带数量。
        count = sum(1 for outcome in result.outcomes if outcome.has_fatal)
        alert(
            f"整批拒收：{target}",
            f"{count} / {len(result.outcomes)} 只票的批次被 FATAL 拦下，一只都没进干净区。"
            "原因见日报的 FATAL 栏（多半是源改了列名，或给出来的日期全读不出）；"
            "整批被拦的行不落隔离区——FATAL 说的是「这批读不出行」，逐条落证据等于假装它们可读。",
        )
    return result
