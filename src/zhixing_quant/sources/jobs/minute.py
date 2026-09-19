"""分钟线采集任务：抓取 → 门禁 → `minute_*` dataset → 日报（01 Step 4；ADR-0009 决定 6/8）。

与 `daily.py` 共用的部分直接 import（重试与熔断、失败清单的措辞、落盘账的合并、日报渲染）：
这条路径上的每一次复制迟早都会漂成两种"今天到底算成功没有"。四处不同决定了这个文件单独存在：

- **没有窗口**。源固定回最近 1970 根（决定 6），所以"抓哪几天"这个问题在这里没有对应物——
  交回来的是一条尾巴，报告日只是尾巴的右端。`--previous-days` 那套往回带的旋钮因此不存在。
- **一只票 × 一个周期 = 一批**。两种周期混成一批会在 `(symbol, ts)` 上撞键（10:00 那根在 5min
  与 30min 里都存在），R008 会把后到的判成重复；而它们本来就要落进两个 dataset。
- **跑多大由人说**（决定 8）。`symbols` 与 `limit` 至少给一个，都不给就抛 `ScopeNotConfigured`。
  全市场三周期 ≈ 13290 请求 ≈ 2–3 小时/日，替用户按下这个按钮不在自主工作的边界之内。
- **它要读盘**。日线任务只看自己刚抓的那一批；分钟线存在的意义之一是跟另一本账（日线）比同一天，
  所以这一层多一个读盘的边界（`reconcile_pool`），而它是门禁之外的一条规则（04 §二 的表外规则）。

尾巴意味着"每天多攒一点"一旦停下就少一截（代价三）：停一天，第二天的整段重落盘还补得回来；
停到那天滑出 1970 根的窗口，就永久没了。所以这里比日线多报两样东西——**这次见到的最后一根K线是哪天**
（`Tally.newest`）与**报告日两本账对不对得上**（R011）。没有它们，"源不更新了"和"网络炸了"在日报上
是同一句话。

日报落在 `reports/minute/` 而不是 `reports/`：归档文件名是 `<报告日>.md`，与日线同一个目录等于
每天的盘后报告被分钟任务覆盖掉一份，而两份说的是两件不同的事。

R011 的对账也挂在这个任务上。判据（价相等、量额留容差）是纯比较，住在 `quality.reconcile`；而
"对哪一天、对哪些票、从哪块盘读"是任务的事实，所以 `reconcile_pool` 住在这里。同一次读盘顺带报出
**盘上深度**（07 §5.1 要的可回测区间）：起点不另立档案——盘上最早的那个分区就是它，多一份记录就多
一份和盘不一致的可能。
"""

from __future__ import annotations

import time
from collections import Counter
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
from zhixing_quant.quality.reconcile import Finding, Recon, counts, reconcile_day
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
from zhixing_quant.storage import layout, query
from zhixing_quant.storage.quarantine import QuarantineReport
from zhixing_quant.storage.write import WriteReport

#: 抓取边界：一只票、一个周期 → 源行。范围由 `stores` 的键说，而不是由一个窗口参数说。
MinuteFetcher = Callable[[str, str], Rows]
#: 对账边界：报告日 + 股票池 + 周期 → 两本账的差，顺带每个周期在盘上覆盖了多久。
#: 生产上就是 `reconcile_pool`。
Reconciler = Callable[[date, Sequence[str], Sequence[str]], Recon]

#: 分钟日报的子目录名。与日线报告分开归档的理由见模块说明最后一段。
MINUTE_REPORTS = "minute"
#: 对账每类坏法在日报上列几条。按类各取而不是全局取：一种坏法一天来 300 条就会把另外四种
#: 挤出日报，而"今天既有断档又有价差"恰恰是最需要一眼看见的那种日子（`daily_report` 的
#: "每个源 Top10 而不是全局 Top10"是同一条理由）。
KIND_SAMPLE = 3


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
    #: 对账的账。None = 这次没给对账边界，日报上那一节会照实写"这次没查"。
    recon: Recon | None = None

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


def reconcile_pool(
    day: date,
    symbols: Sequence[str],
    periods: Sequence[str],
    *,
    root: Path,
) -> Recon:
    """R011 的读盘那一半：把盘上两本账按（票 × 周期）各对一遍。

    只查报告日那一天，不回头查尾巴上的每一天：尾巴的每一天都已经在它自己那天被报过（这就是每天
    跑一次的意义），而重查 42~493 天 × 池子 × 三个周期会让对账成为整个任务最慢的一段——它排在
    落盘之后，多花的一秒都是从明天的抓取窗口里扣的。

    一只票的日线只读一次，几个周期共用：`read_bars` 每次一个 DuckDB 连接，池子 300 只时这是
    300 次与 1200 次的区别。

    两边都读**不复权**价（`query` 的默认口径）：这条规则比的是"同一天同一笔成交"，而因子只挂在
    日线上——复权一遍就等于把同一个因子只乘到其中一本账上，那种偏差比它要抓的任何一种都大。

    `root` 是干净区那一层（`config.parquet_dir()`），不是数据根：传成后者的话两次读都落在一个不存在
    的目录上，返回空列表，于是"两边都没数据"= 什么都不报——对账从此永远全绿，而盘上好好的装着今天
    刚落的行。这个参数因此没有默认值。深度读的是同一个 root：传错的报法是"这个周期从没落过盘"，
    那是一句响的错话，比静默全绿好，但也不对，所以别传错。
    """
    findings: list[Finding] = []
    tolerance = _tolerance()  # 装一次就够：池子 300 只时这是 1 次与 900 次 TOML 解析的区别
    for symbol in symbols:
        daily = query.read_bars(symbol, day, day, dataset=layout.DAILY, root=root)
        line = daily[0] if daily else None
        for period in periods:
            dataset = layout.minute_dataset(period)
            bars = query.read_bars(symbol, day, day, dataset=dataset, root=root)
            findings += reconcile_day(symbol, day, dataset, bars, line, tolerance_pct=tolerance)
    # 深度在 findings 之后读：同一个 dataset，一次是"今天对不对得上"，一次是"一共攒了多少"。
    # 放在落盘之后而不是之前，报出来的起点/末点才算包含今天这批。
    covers = {
        layout.minute_dataset(period): query.depth(layout.minute_dataset(period), root=root)
        for period in periods
    }
    return Recon(groups=len(symbols) * len(periods), findings=tuple(findings), covers=covers)


def _tolerance() -> float:
    """量额容差：与 R004/R007 同一个数，从 `gate.toml` 的 `[defaults]` 读。

    在这里读而不是由调用方传：容差是判据的一部分，能传就有两种"多少算噪声"的写法，而日报上那行
    偏差说的到底是哪一种就没人知道了。
    """
    return float(gate_config.load(config.gate_config_file()).defaults["tolerance_pct"])


def _disk_sections(recon: Recon | None, periods: Sequence[str]) -> str:
    """日报末尾两节：R011 的账（01 Step 4 验收 2）与盘上深度（07 §5.1 的可回测区间）。

    两节共用同一个 `recon`，因为它们本来就是同一次读盘读出来的：分成两个边界就会有两个时刻，
    而"今天有没有断档"与"一共攒了多少天"必须是同一天早上的两份账。

    `recon` 为 None 或 `covers` 为空说的是"这次没查/没读盘"，写成"五种坏法全 0"或编一个起点
    就是把没做报成做完了——那正是 04 §四 反复要避开的那种日报。
    """
    if recon is None:
        absent = [
            "",
            "## 分钟 ↔ 日线对账（R011）",
            "",
            "- 这次没查：对账要读盘上两个 dataset 的同一日，调用方没给对账边界",
            "",
            "## 盘上深度",
            "",
            "- 这次没读盘：深度与对账共用同一次读盘，没有对账边界就没有区间",
        ]
        return "\n".join(absent) + "\n"
    tally = "、".join(f"{kind} {n}" for kind, n in counts(recon.findings).items())
    lines = [
        "",
        "## 分钟 ↔ 日线对账（R011）",
        "",
        f"- 查了 {recon.groups} 组（票 × {'/'.join(periods)} 分钟）：{tally}",
    ]
    seen: Counter[str] = Counter()
    for finding in recon.findings:
        if seen[finding.kind] >= KIND_SAMPLE:
            continue
        seen[finding.kind] += 1
        lines.append(
            f"- {finding.symbol} {finding.day.isoformat()} {finding.dataset}：{finding.detail}"
        )
    for kind, total in counts(recon.findings).items():
        if total > KIND_SAMPLE:
            hidden = total - KIND_SAMPLE
            lines.append(f"- 其余 {hidden} 条 {kind} 未列（日报每类只列 {KIND_SAMPLE} 条）")
    lines += ["", "## 盘上深度", ""]
    if not recon.covers:
        lines.append("- 这次没读到：深度要扫一遍分区目录，调用方给的对账边界没填这一项")
        return "\n".join(lines) + "\n"
    for dataset, cover in recon.covers.items():
        if cover is None:
            lines.append(f"- {dataset}：盘上一个文件都没有，这个周期的落盘从没成功过")
            continue
        lines.append(
            f"- {dataset}：{cover.first.isoformat()} .. {cover.last.isoformat()}"
            f"，{cover.days} 个有行交易日 × {cover.symbols} 只票"
        )
    if any(cover is not None for cover in recon.covers.values()):
        lines.append(
            "- 起点那天是从源的 1970 根窗口里掉出来的，通常不足全天：算段长别把它当完整的一天"
        )
    return "\n".join(lines) + "\n"


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
    reconcile: Reconciler | None = None,
    attempts: int = 3,
    breaker: int = 20,
    sleep: Callable[[float], None] = time.sleep,
    alert: Alert = stdout_alert,
) -> MinuteResult:
    """一次运行：定日 → 定池 → 抓取判定落盘 → 对账 → 发布分钟日报。

    `directory` 没有默认值：默认往 `config.reports_dir()` 写等于每天覆盖掉一份日线日报，
    而那正是模块说明里要避免的事——让调用方必须说一句话，比让它猜错便宜。

    `reconcile` 也没有默认值，但理由相反：读盘这件事要一个数据根，而这里连 root 都不知道（root
    由 `stores` 的那些 partial 闭包握着）。不给就对不上账、也报不出深度，日报上那两节会照实写
    "这次没查"与"这次没读盘"。

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
    recon = reconcile(target, pool, tuple(stores)) if reconcile is not None else None
    report, body = publish(
        target,
        tally.outcomes,
        landed=tally.landed,
        quarantined=tally.quarantined,
        skipped=tally.skipped,
        directory=directory,
        extra=_disk_sections(recon, tuple(stores)),
    )
    result = MinuteResult(
        day=target,
        periods=tuple(stores),
        pool=len(pool),
        tally=tally,
        markdown=body,
        report=report,
        recon=recon,
    )
    _alert(result, alert)
    return result


def _alert(result: MinuteResult, alert: Alert) -> None:
    """跑坏了才响。两种坏法分开说，因为处置不同。

    "一只都没判成"可能是网络，也可能是源在给空表；带上尾巴右端，是因为只有它能区分
    "源停更了 N 天"与"源今天还没出数据"——前者要换源，后者只是跑早了。

    对账查出来的那些偏差**不**走这里，只进日报（验收 2 要的就是"进日报"）。理由是这里没有一条
    可信的阈值：几条断档算多？我没有噪声基线——今天 300 只的池子里究竟有几只是源真不给分钟行的，
    要等日报攒够几天才知道。凭感觉定一个数的结果是要么天天响、要么永不响，两种都比不响糟。
    """
    empty = sum(1 for item in result.tally.skipped if item.reason == REASON_NO_ROWS)
    breaker = sum(1 for item in result.tally.skipped if item.reason == REASON_BREAKER)
    if not result.ok:
        requests = result.pool * len(result.periods)
        alert(
            f"分钟线今日无数据：{result.day}",
            f"股票池 {result.pool} 只 × {'/'.join(result.periods)} 分钟 = {requests} 个请求，"
            f"一只都没判成（无行 {empty}、熔断 {breaker}、其余是请求失败）。"
            f"这次见到的最后一根K线属于 {result.tally.newest}——那只是尾巴的右端。报告日 "
            f"{result.day} 到底在不在盘上，看日报的 R011 那一节。"
            "分钟线只能取回源窗口内的最近 1970 根：这一天的尾巴滑出窗口就永久没了。",
        )
    elif result.fatal:
        count = sum(1 for outcome in result.tally.outcomes if outcome.has_fatal)
        alert(
            f"分钟线整批拒收：{result.day}",
            f"{count} / {len(result.tally.outcomes)} 个批次被 FATAL 拦下，一行都没进干净区。"
            "多半是源改了列名或日期读不出；原因见 reports/minute/ 的日报 FATAL 栏。",
        )
