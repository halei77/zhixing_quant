"""每日盘后采集：抓取 → 门禁 → 日报（01 路线图 Step 2 第 4 项，验收 4）。

抓取与告警都是**参数**而不是本模块里的调用：故障注入（断网/源不可用）要能塞进重试路径，
真联网的那一层由调用方给。所以这里不 import akshare、不读环境变量、也不安装定时器——
改用户机器上的 cron 属于"先问"（05 Q1）。

两件事决定了批次的形状：

- **两日一批**。R007 的判据是"今开 vs 昨收"，昨收只能由批次里上一日的行现算，单日一批
  等于这条规则永不触发且永不说话（04 §二：R007 阈值与 R004 联动）。报告再用
  `GateOutcome.for_day` 裁回报告日，日报的分母仍是"那天"。
- **一只票一批**。`GateEngine` 本来就拒混批（`GateInconsistency`），按票分批还额外买到
  隔离性：一只票的网络失败不牵连其余几千只。同源多批由 `DataQualityReport.from_outcomes`
  并成一行（台账坑 #10）。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path

from zhixing_quant import config
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import SecurityMaster
from zhixing_quant.quality import daily_report, gate_config
from zhixing_quant.quality.engine import GateEngine, GateOutcome
from zhixing_quant.quality.report import DataQualityReport
from zhixing_quant.sources.akshare import daily as akshare_daily
from zhixing_quant.sources.rows import SourceSchemaError

Rows = Sequence[Mapping[str, object]]
#: 一组 (不复权, 后复权) 源行。抓取一次要同时带回两组：R002 的涨跌停判据看不复权价，
#: 干净区存的是后复权价，两者只能一起交进来。
Pair = tuple[Rows, Rows]

#: 抓取边界：一只票、一个闭区间。
Fetcher = Callable[[str, date, date], Pair]
#: 告警边界：标题 + 正文。iOS 推送通道接在这里，本模块只调用不实现。
Alert = Callable[[str, str], None]


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

    @property
    def ok(self) -> bool:
        """一只票都没判成 = 这次运行没成。空报告不能当"今天满分"。"""
        return bool(self.outcomes)


def stdout_alert(title: str, body: str) -> None:
    """默认告警落标准输出：定时任务的日志就是它的收件箱。"""
    print(f"[ALERT] {title}\n{body}", flush=True)


def with_retry(
    fetch: Callable[[], Pair],
    symbol: str,
    *,
    attempts: int = 3,
    backoff: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    alert: Alert = stdout_alert,
) -> Pair | Skipped:
    """重试到 `attempts` 次；只在最后一次告警。

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
    master: SecurityMaster,
    calendar: TradingCalendar,
    previous_days: int = 1,
    attempts: int = 3,
    backoff: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
    alert: Alert = stdout_alert,
) -> tuple[list[GateOutcome], list[Skipped]]:
    """抓 + 判一批票，返回逐票结果与最终没拿下來的票。

    `previous_days` 是往回带几个交易日：1 天够 R007；Step 3 做时间连续性检查时要更多。
    日历判不出上一日（样本区间的第一桶）时按单日批处理，此时 R007 不判——这一点由
    分母不变来保证可见：少了昨收的行仍然在批里，只是没有偏差可算。
    """
    start = _window_start(calendar, day, previous_days)
    engine = GateEngine(gate_config.load(config.gate_config_file()), master, calendar)
    outcomes: list[GateOutcome] = []
    skipped: list[Skipped] = []
    for symbol in symbols:
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
            continue
        raw, hfq = grabbed
        drafts = akshare_daily.daily_drafts(raw, hfq, symbol=symbol)
        outcomes.append(engine.run(drafts).for_day(day))
    return outcomes, skipped


def _window_start(calendar: TradingCalendar, day: date, previous_days: int) -> date:
    cursor: date | None = day
    for _ in range(previous_days):
        cursor = calendar.prev_trading_day(cursor) if cursor is not None else None
    return cursor if cursor is not None else day


def publish(
    day: date,
    outcomes: Sequence[GateOutcome],
    *,
    skipped: Sequence[Skipped] = (),
    directory: Path | None = None,
) -> tuple[DataQualityReport, str]:
    """渲染 + 归档 + 分数流水。打印留给调用方：日报的正文同时是定时任务的日志。"""
    reports = directory if directory is not None else config.reports_dir()
    scores = reports / "scores.csv"
    report = DataQualityReport.from_outcomes(day, outcomes)
    trends = {m.source: daily_report.history_for(scores, m.source, day) for m in report.metrics}
    body = daily_report.render(report, list(outcomes), trends)
    if skipped:
        body += _skipped_section(skipped)
    daily_report.archive(body, day, reports)
    daily_report.append_scores(report, scores)
    return report, body


def _skipped_section(skipped: Sequence[Skipped]) -> str:
    """抓取失败清单：列前 `DETAIL_TOP` 只加总数。三千只票全列出来等于把日报撑爆。"""
    cap = daily_report.DETAIL_TOP
    lines = ["", "## 抓取失败", "", f"- 重试后仍失败 {len(skipped)} 只（不计入上面的分母）："]
    lines += [f"- {item.symbol}：{item.reason}" for item in skipped[:cap]]
    if len(skipped) > cap:
        lines.append(f"- 其余 {len(skipped) - cap} 只同因，见任务日志")
    return "\n".join(lines) + "\n"


def run(
    day: date | None = None,
    *,
    fetch: Fetcher,
    master: SecurityMaster,
    calendar: TradingCalendar,
    now: datetime | None = None,
    symbols: Sequence[str] | None = None,
    limit: int | None = None,
    directory: Path | None = None,
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    alert: Alert = stdout_alert,
) -> DayResult:
    """一次盘后运行：定日 → 抓取判定 → 发布日报。

    股票池默认取主数据当天的在册名单，`symbols` / `limit` 用来先在小范围跑通——全市场还是
    指数成分股是 01 路线图留给 Step 2 的待定口径，不在代码里替用户决定。
    """
    moment = now if now is not None else datetime.now(UTC)
    target = day if day is not None else last_trading_day(calendar, moment.date())
    pool = list(symbols) if symbols is not None else list(master.universe_on(target))
    if limit is not None:
        pool = pool[:limit]
    outcomes, skipped = collect(
        target,
        pool,
        fetch=fetch,
        master=master,
        calendar=calendar,
        attempts=attempts,
        sleep=sleep,
        alert=alert,
    )
    report, body = publish(target, outcomes, skipped=skipped, directory=directory)
    if not outcomes:
        alert(
            f"今日无数据：{target}",
            f"股票池 {len(pool)} 只，一只都没判成（跳过 {len(skipped)} 只）。"
            "日报已落盘，内容是空报告——别把它当「今天一切正常」。",
        )
    return DayResult(
        day=target,
        outcomes=tuple(outcomes),
        report=report,
        markdown=body,
        skipped=tuple(skipped),
        pool=len(pool),
    )
