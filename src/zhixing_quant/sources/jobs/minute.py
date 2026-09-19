"""分钟线采集任务：抓取 → 门禁 → `minute_*` dataset → 日报（01 Step 4；ADR-0009 决定 6/8）。

与 `daily.py` 共用的部分直接 import（重试与熔断、失败清单的措辞、落盘账的合并、日报渲染）：
这条路径上的每一次复制迟早都会漂成两种"今天到底算成功没有"。三处不同决定了这个文件单独存在：

- **没有窗口**。源固定回最近 1970 根（决定 6），所以"抓哪几天"这个问题在这里没有对应物——
  交回来的是一条尾巴，报告日只是尾巴的右端。`--previous-days` 那套往回带的旋钮因此不存在。
- **一只票 × 一个周期 = 一批**。两种周期混成一批会在 `(symbol, ts)` 上撞键（10:00 那根在 5min
  与 30min 里都存在），R008 会把后到的判成重复；而它们本来就要落进两个 dataset。
- **跑多大由人说**（决定 8）。`symbols` 与 `limit` 至少给一个，都不给就抛 `ScopeNotConfigured`。
  全市场三周期 ≈ 13290 请求 ≈ 2–3 小时/日，替用户按下这个按钮不在自主工作的边界之内。

尾巴意味着"每天多攒一点"一旦停下就永久少一截（代价三），所以这里比日线多报一样东西：**这次见到
的最后一根K线是哪天**（`Tally.newest`）。没有它，"源不更新了"和"网络炸了"在日报上是同一句话。

日报落在 `reports/minute/` 而不是 `reports/`：归档文件名是 `<报告日>.md`，与日线同一个目录等于
每天的盘后报告被分钟任务覆盖掉一份，而两份说的是两件不同的事。
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
from zhixing_quant.quality import gate_config
from zhixing_quant.quality.engine import GateEngine, GateOutcome
from zhixing_quant.quality.report import DataQualityReport
from zhixing_quant.sources.akshare import minute as akshare_minute
from zhixing_quant.sources.jobs.daily import (
    REASON_BREAKER,
    REASON_NO_ROWS,
    Alert,
    QuarantineStore,
    Skipped,
    Store,
    last_trading_day,
    publish,
    stdout_alert,
    sum_quarantines,
    sum_writes,
    with_retry,
)
from zhixing_quant.sources.rows import Rows
from zhixing_quant.storage.quarantine import QuarantineReport
from zhixing_quant.storage.write import WriteReport

#: 抓取边界：一只票、一个周期 → 源行。范围由 `stores` 的键说，而不是由一个窗口参数说。
MinuteFetcher = Callable[[str, str], Rows]

#: 分钟日报的子目录名。与日线报告分开归档的理由见模块说明最后一段。
MINUTE_REPORTS = "minute"


class ScopeNotConfigured(ValueError):
    """`symbols` 与 `limit` 都没给：这次要跑多大，代码不替用户决定（ADR-0009 决定 8）。"""


@dataclass(frozen=True)
class Tally:
    """一次跑完的账。`newest` 是这次见到的最后一根K线属于哪天——尾巴的右端，日报判不出它。"""

    outcomes: tuple[GateOutcome, ...]
    skipped: tuple[Skipped, ...]
    landed: WriteReport
    quarantined: QuarantineReport
    newest: date | None


@dataclass(frozen=True)
class MinuteResult:
    day: date
    periods: tuple[str, ...]
    pool: int
    tally: Tally
    markdown: str
    report: DataQualityReport

    @property
    def ok(self) -> bool:
        """这次有没有判出东西。判成 0 行的报告不是"今天满分"（与 `daily.DayResult.ok` 同一条）。"""
        return any(outcome.total for outcome in self.tally.outcomes)

    @property
    def fatal(self) -> bool:
        return any(outcome.has_fatal for outcome in self.tally.outcomes)


def label(symbol: str, period: str) -> str:
    """一只票的一个周期在失败清单上的名字。

    带周期是必须的：不带的话，一只票的三个周期失败会写成三行一模一样的"600519：超时"，
    而日报的"重试后仍失败 3 只"就把一只票算成了三只。
    """
    return f"{symbol}/{period}min"


def collect(
    day: date,
    symbols: Sequence[str],
    *,
    fetch: MinuteFetcher,
    stores: Mapping[str, Store],
    quarantine: QuarantineStore,
    master: SecurityMaster,
    calendar: TradingCalendar,
    attempts: int = 3,
    backoff: float = 5.0,
    breaker: int = 20,
    sleep: Callable[[float], None] = time.sleep,
    alert: Alert = stdout_alert,
) -> Tally:
    """抓 + 判 + 落一批票的多个周期。`stores` 的键就是要跑的周期，没有第二个旋钮。

    熔断按**请求**数而不是按票数：一次运行里一只票有 `len(stores)` 个请求，源整体宕掉时
    要停的是请求，不是"这只票的三个周期里的前一个"。剩下的组合照样进失败清单——收工不是
    掩盖缺数，只是把"今天这个源不行"说得更早。

    落盘不裁成报告日：尾巴上的每一天都是今天抓到的证据（日线任务同一条理由），裁剪只发生在
    报给日报的那一侧。两个落盘都不兜异常，理由见 `daily.collect`。
    """
    engine = GateEngine(gate_config.load(config.gate_config_file()), master, calendar)
    outcomes: list[GateOutcome] = []
    skipped: list[Skipped] = []
    written: list[WriteReport] = []
    recorded: list[QuarantineReport] = []
    newest: date | None = None
    consecutive = 0
    for position, symbol in enumerate(symbols):
        if consecutive >= breaker:
            skipped += [
                Skipped(symbol=label(symbol, period), reason=REASON_BREAKER)
                for symbol in symbols[position:]
                for period in stores
            ]
            break
        for period, store in stores.items():
            grabbed = with_retry(
                partial(fetch, symbol, period),
                label(symbol, period),
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
            drafts = akshare_minute.minute_drafts(grabbed, symbol=symbol, period=period)
            if not drafts:
                # 请求成功、帧是空的：不是"抓取失败"，也不是一批可判的数据。理由同 `daily`：
                # 交给门禁会得到 R006 的整批 FATAL，而那条要扣整源 40 分。
                skipped.append(Skipped(symbol=label(symbol, period), reason=REASON_NO_ROWS))
                continue
            judged = engine.run(drafts)
            written.append(store(judged.clean_zone))
            recorded.append(quarantine(judged.quarantined, day))
            outcomes.append(judged.for_day(day))
            seen = max((bar.trade_date for bar in judged.clean_zone), default=None)
            if seen is not None and (newest is None or seen > newest):
                newest = seen
    return Tally(
        outcomes=tuple(outcomes),
        skipped=tuple(skipped),
        landed=sum_writes(written),
        quarantined=sum_quarantines(recorded),
        newest=newest,
    )


def run(
    day: date | None,
    *,
    master: SecurityMaster,
    calendar: TradingCalendar,
    fetch: MinuteFetcher,
    stores: Mapping[str, Store],
    quarantine: QuarantineStore,
    directory: Path,
    symbols: Sequence[str] | None = None,
    limit: int | None = None,
    now: datetime | None = None,
    attempts: int = 3,
    breaker: int = 20,
    sleep: Callable[[float], None] = time.sleep,
    alert: Alert = stdout_alert,
) -> MinuteResult:
    """一次运行：定日 → 定池 → 抓取判定落盘 → 发布分钟日报。

    `directory` 没有默认值：默认往 `config.reports_dir()` 写等于每天覆盖掉一份日线日报，
    而那正是模块说明里要避免的事——让调用方必须说一句话，比让它猜错便宜。

    股票池：`symbols` 给了就用它，否则取主数据当天的在册名单再 `[:limit]`。两者都不给就抛
    `ScopeNotConfigured`（决定 8）。
    """
    if symbols is None and limit is None:
        raise ScopeNotConfigured(
            "分钟线不知道这次该跑多大：给 --symbols 或 --limit。采几个周期、票池取全市场还是"
            "指数成分股是用户口径（ADR-0009 决定 8），默认值不在代码里替人定"
        )
    moment = now if now is not None else datetime.now(UTC)
    target = day if day is not None else last_trading_day(calendar, moment.date())
    if symbols is not None:
        pool = list(symbols)
    else:
        universe = list(master.universe_on(target))
        pool = universe[:limit] if limit is not None else universe
    tally = collect(
        target,
        pool,
        fetch=fetch,
        stores=stores,
        quarantine=quarantine,
        master=master,
        calendar=calendar,
        attempts=attempts,
        breaker=breaker,
        sleep=sleep,
        alert=alert,
    )
    report, body = publish(
        target,
        tally.outcomes,
        landed=tally.landed,
        quarantined=tally.quarantined,
        skipped=tally.skipped,
        directory=directory,
    )
    result = MinuteResult(
        day=target,
        periods=tuple(stores),
        pool=len(pool),
        tally=tally,
        markdown=body,
        report=report,
    )
    _alert(result, alert)
    return result


def _alert(result: MinuteResult, alert: Alert) -> None:
    """跑坏了才响。两种坏法分开说，因为处置不同。

    "一只都没判成"可能是网络，也可能是源在给空表；带上尾巴右端，是因为只有它能区分
    "源停更了 N 天"与"源今天还没出数据"——前者要换源，后者只是跑早了。
    """
    empty = sum(1 for item in result.tally.skipped if item.reason == REASON_NO_ROWS)
    breaker = sum(1 for item in result.tally.skipped if item.reason == REASON_BREAKER)
    if not result.ok:
        requests = result.pool * len(result.periods)
        alert(
            f"分钟线今日无数据：{result.day}",
            f"股票池 {result.pool} 只 × {'/'.join(result.periods)} 分钟 = {requests} 个请求，"
            f"一只都没判成（无行 {empty}、熔断 {breaker}、其余是请求失败）。"
            f"这次见到的最后一根K线属于 {result.tally.newest}——报告日 {result.day} 的行不在盘上，"
            "而分钟线没有回填（ADR-0009 决定 6）：这一天的尾巴滑出源窗口就永久没了。",
        )
    elif result.fatal:
        count = sum(1 for outcome in result.tally.outcomes if outcome.has_fatal)
        alert(
            f"分钟线整批拒收：{result.day}",
            f"{count} / {len(result.tally.outcomes)} 个批次被 FATAL 拦下，一行都没进干净区。"
            "多半是源改了列名或日期读不出；原因见 reports/minute/ 的日报 FATAL 栏。",
        )
