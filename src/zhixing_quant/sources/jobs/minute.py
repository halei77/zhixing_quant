"""分钟线采集任务：抓取 → 门禁 → `minute_*` dataset → 日报（01 Step 4；ADR-0009 决定 6/8）。

与 `daily.py` 共用的部分直接 import（重试与熔断、失败清单的措辞、落盘账的合并、日报渲染）：
这条路径上的每一次复制迟早都会漂成两种"今天到底算成功没有"。四处不同决定了这个文件单独存在：

- **没有回填旋钮**（`--previous-days` 不存在），而"尾巴有多长、漏一天有多急"**按源分两半**：
  5/30/60 主源 akshare/sina（抓空/抓错降级 ngw，任务 #64），sina 固定回最近 1970 根（决定 6），
  交回来的是一条尾巴、报告日只是尾巴的右端；1 分钟走 ngw（ADR-0022 决定 3 的源），单页
  count≤1400（约 5.8 个交易日）且有 `start` 截止参数能向过去翻页（B 刀 2026-09-24 实测翻到
  2019）。两半的日报与告警口径因此分开写——sina 的"1970 根窗口"句挂到 ngw 的报告上是替源
  传错了话。
- **一只票 × 一个周期 = 一批**。两种周期混成一批会在 `(symbol, ts)` 上撞键（10:00 那根在 5min
  与 30min 里都存在），R008 会把后到的判成重复；而它们本来就要落进两个 dataset。同一批内不混源：
  降级发生在"这一批整个交给 ngw"的粒度上，`GateEngine` 的混源拒批因此永远碰不到兜底路径。
- **跑多大由人说**（决定 8）。`symbols` 与 `limit` 至少给一个，都不给就抛 `ScopeNotConfigured`。
  全市场三周期 ≈ 13290 请求 ≈ 2–3 小时/日，替用户按下这个按钮不在自主工作的边界之内。
- **它要读盘**。日线任务只看自己刚抓的那一批；分钟线存在的意义之一是跟另一本账（日线）比同一天，
  所以这一层多一个读盘的边界（`reconcile_pool`），而它是门禁之外的一条规则（04 §二 的表外规则）。

尾巴意味着"每天多攒一点"一旦停下就少一截（代价三）：停一天，第二天的整段重落盘还补得回来；
对 sina 三周期，停到那天滑出 1970 根的窗口就永久没了（ngw 有 `start` 翻页、漏的天补得回来，
是另一种急法）。所以这里比日线多报两样东西——**这次见到的最后一根K线是哪天**
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
from typing import NamedTuple

from zhixing_quant import config
from zhixing_quant.domain.bar import BarDraft
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import SecurityMaster
from zhixing_quant.quality import gate_config
from zhixing_quant.quality.engine import GateEngine, GateOutcome
from zhixing_quant.quality.reconcile import Finding, Recon, counts, reconcile_day
from zhixing_quant.quality.report import DataQualityReport
from zhixing_quant.sources.akshare import fetch as akshare_fetch
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
from zhixing_quant.sources.ngw import minute as ngw_minute
from zhixing_quant.sources.ngw.client import KLINE_TYPES, MAX_KLINE_COUNT, NgwClient
from zhixing_quant.sources.rows import Rows
from zhixing_quant.storage import layout, query
from zhixing_quant.storage.quarantine import QuarantineReport
from zhixing_quant.storage.write import WriteReport

#: 抓取边界：一只票、一个周期 → 源行 + **是谁交来的**（`Grabbed.via`）。范围由 `stores` 的键说，
#: 而不是由一个窗口参数说；源则必须由交行的那一刻亲口说——坑 #18/#20 的同一条纪律。
MinuteFetcher = Callable[[str, str], "Grabbed"]


class Grabbed(NamedTuple):
    """一次抓取的结果：源行 + 经由的源（`VIA_AKSHARE` / `VIA_NGW`）。

    `via` 不是元数据装饰，它决定**行解析选哪个适配器**（`drafts_for` 的唯一分派键）：
    降级接线后同一个周期可能来自两个源（5 分：sina 挂了 ngw 顶），行形状完全不同
    （sina `day/open/...`·元 vs ngw `times/openp/...`·分）。还按「周期 → 源」的老键分派，
    就会把 ngw 的 `timedata` 送进 sina 解析器——整批挂在 R010 批级 FATAL 上，而日报看起来
    像源改了列名。抓取说了谁，解析就信谁。
    """

    rows: Rows
    via: str


#: 对账边界：报告日 + 股票池 + 周期 → 两本账的差，顺带每个周期在盘上覆盖了多久。
#: 生产上就是 `reconcile_pool`。
Reconciler = Callable[[date, Sequence[str], Sequence[str]], Recon]

#: 分钟日报的子目录名。与日线报告分开归档的理由见模块说明最后一段。
MINUTE_REPORTS = "minute"
#: 对账每类坏法在日报上列几条。按类各取而不是全局取：一种坏法一天来 300 条就会把另外四种
#: 挤出日报，而"今天既有断档又有价差"恰恰是最需要一眼看见的那种日子（`daily_report` 的
#: "每个源 Top10 而不是全局 Top10"是同一条理由）。
KIND_SAMPLE = 3

#: **`via` 的两个取值**——一次抓取由谁交行，写进 `Grabbed.via` 与 `drafts_for` 的解析分派。
#: 这两个字符串不是源标识（那是 `akshare_minute_5`/`ngw_minute_5` 那种，由适配器按周期生成），
#: 而是"哪一家把行交来"的粗分类，恰好对应两个适配器。别拿源标识当分派键：一个源可能供多个
#: 周期，反过来同一个周期降级时会换源（5 分：sina 空了走 ngw）——只有"谁交的行"永不错。
VIA_AKSHARE = "akshare"
VIA_NGW = "ngw"

#: **周期 → 主源**：`"1"` 的主源就是 ngw（源标识 `ngw_minute_1`，无降级——它本身就是兜底源），
#: 其余（5/30/60）主源 akshare/sina（`akshare_minute_*`，ADR-0009 决定 1、ADR-0021 决定 2 的
#: 口径，主路径一个字节不改），**抓空或抓错则降级到 ngw**（`ngw_minute_*`）。一个周期一个主源
#: 不是口味：04 §三 按源打分，ngw 的接口挂了不该扣 sina 分钟的健康分，反向同理（ADR-0009 把
#: 5/30/60 拆成三个源名的同款理由）。降级后的行由 ngw 适配器按各自周期打 `ngw_minute_*` 标，
#: 于是"今天哪些票的 5 分是 ngw 补的"在日报各源表里点名可查。抓取分派（`live_fetcher`）与
#: 行解析分派（`drafts_for`）都只看 `Grabbed.via` 这一个键——两处各列一份周期表迟早漂成
#: "抓的是 ngw、解析按 sina"，那会让整批挂在 R010（批级 FATAL 扣健康分），而真相只是接线错了。
NGW_PERIOD = "1"

#: 降级到 ngw 时各周期的 K线 `type` 查表住在 `ngw.client.KLINE_TYPES`（协议事实归协议层，
#: 1/5/30/60 一张表；实测见数据根 `reports/source-probe/2026-09-25-ngw-minute-periods.md`）——
#: 这里再列一份「周期→type」就是本模块反复制原则要避开的第二种漂移。

#: ngw pacer 的起步间隔（秒）。**选 1.5s**：Qoute T-001 的纪律原文是"串行 ≥1.5s 起步，
#: 429 阈值未知，Qoute 按 1.5s 跑一个月无事故"（B 刀 client.py 抄了同一条并注明"首周批量
#: 把 interval 调到 1.5 再跑"）。`client.py` 的 `DEFAULT_MIN_INTERVAL=0.3` 是通用起步值
#: （B 刀实测 0.3s 也通），本刀是首周批量接线，按纪律取 1.5：10 个请求 ≈ 15s 的代价换
#: "不知道阈值时不逼近阈值"。限速住在这个装配常量而不是 CLI 旋钮——能随手调低的限速不是纪律。
NGW_MIN_INTERVAL = 1.5

#: **一轮运行最多降级多少次**。降级是"这只票这个周期 sina 没给"的补法，不是"sina 整源倒了
#: 由 ngw 顶班"的换路——Qoute 的封禁史都是拿全市场节奏撞出来的，5558 只 × 3 周期真让 ngw
#: 全接，1.5s 串行 ≈ 12+ 小时且把封禁风险整个转嫁给它，那是换 ADR 的事不是配额的量级。
#: 300 的量级依据：正常日降级是个位数到几十（个别票 sina 缺行）；超过 300 只说明 sina 侧
#: 系统性出问题了，此时要的是**停下来让人看见**（告警 + 日报点名），不是 ngw 无声扛住整源。
#: 触顶后本轮回退纯主源语义（akshare 空/错就 Skipped），下一次运行重置。
NGW_FALLBACK_BUDGET = 300

#: ngw 一个完整交易日的 1 分钟根数（B 刀实测 241：09:30 开盘竞价栏 + 240 根标准右端点）。
#: 只用于把单页根数换算成"约几个交易日"写进日报/告警，不参与任何判定。
BARS_PER_DAY_1M = 241


class ScopeNotConfigured(ValueError):
    """`symbols` 与 `limit` 都没给：这次要跑多大，代码不替用户决定（ADR-0009 决定 8）。"""


@dataclass(frozen=True)
class Tally:
    """一次跑完的账。`newest` 是这次见到的最后一根K线属于哪天——尾巴的右端，日报判不出它。

    `downgrades` 是走了 ngw 兜底的 (票/周期) 清单（只数 5/30/60——1 分的主源本来就是 ngw，
    不叫降级）。它是从 `via` 数出来的而不是从抓取闭包里数：落没落盘、判没判成以这里为准，
    抓取层报"我降级了"而门禁把整批拒了的话，日报上那行就成了空头账。
    """

    outcomes: tuple[GateOutcome, ...]
    skipped: tuple[Skipped, ...]
    landed: WriteReport
    quarantined: QuarantineReport
    newest: date | None
    downgrades: tuple[str, ...] = ()


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


def drafts_for(grabbed: Grabbed, *, symbol: str, period: str) -> list[BarDraft]:
    """源行 → 待判定草稿。**`via` 决定用哪个适配器**——本任务唯一的行解析分派。

    与 `live_fetcher` 共用 `Grabbed.via` 这一个分派键：抓取时谁交的行、解析就按谁，两处不再
    各列一份周期表，于是漂成"抓回 ngw 的 `timedata` 却按 sina 的 `day` 列解析"的路被彻底
    堵死（那种漂移会整批挂在 R010 批级 FATAL 扣整源 40 分，而日报看起来像源改了列名，查的
    方向整个是错的）。`period` 只负责给选中的适配器定源标识与粒度，不参与选解析器。
    """
    if grabbed.via == VIA_NGW:
        return ngw_minute.minute_drafts(grabbed.rows, symbol=symbol, period=period)
    return akshare_minute.minute_drafts(grabbed.rows, symbol=symbol, period=period)


def live_fetcher(
    periods: Sequence[str],
    *,
    client: NgwClient | None = None,
    fallback_budget: int = NGW_FALLBACK_BUDGET,
    alert: Alert = stdout_alert,
) -> MinuteFetcher:
    """真网络抓取的**主源 + 降级**分派（生产装配在 `minute_cli`，它调这一个函数）。

    - `NGW_PERIOD`（1 分）→ 直接 ngw：`client.innercode`（六位码换 K 线接口要的 innercode）+
      `client.kline` 一页（`count=MAX_KLINE_COUNT`，单页最深；`start` 不传 = 从最新往回，
      每日增量要的就是这条尾巴）。1 分本就是 ngw 专属，没有"降级"可言。
    - 5/30/60 → 先 `akshare_fetch.minute_frame`（与接线前逐字同一条老路径）；**它抛异常或交回
      空帧**，就降级到 ngw 同周期（`KLINE_TYPES[period]`，`via=VIA_NGW`，源标识落 `ngw_minute_*`）。

    四件装配事实：

    - **client 整轮共享**：pacer 的 `_last_at` 活在 client 上，每只票新建一个等于没有限速
      （Qoute 纪律：串行是它没被封的原因）。一次 `zx-minute` 运行 = 一个 client。
    - **client 懒构造**：`periods` 不含 1 分且 sina 全程健康时根本不建 ngw client——纯
      5/30/60 的老路径一个字节都不变、不摸 token 路径、不建没用的锁；只有真要降级时才
      `ensure_ngw` 建一个并整轮复用（回归钉在测试里）。
    - **降级有预算**（`fallback_budget`，常量注释里算了账）：一轮降级次数触顶后不再降级，
      退回"主源空/错就响"的语义并响一次。降级是补个别票的缺行，不是替 sina 整源顶班。
    - **`client` 是测试注入点**（可注入 transport 的真 client，03 §二 L2 离线重放），
      生产不传、在这里按 `NGW_MIN_INTERVAL` 构造。
    """
    ngw: NgwClient | None = client
    fallbacks = 0

    def ensure_ngw() -> NgwClient:
        nonlocal ngw
        if ngw is None:
            ngw = NgwClient(interval=NGW_MIN_INTERVAL)
        return ngw

    def ngw_grab(symbol: str, period: str) -> Grabbed:
        c = ensure_ngw()
        return Grabbed(
            c.kline(c.innercode(symbol), count=MAX_KLINE_COUNT, ktype=KLINE_TYPES[period]).rows,
            VIA_NGW,
        )

    def downgrade() -> None:
        """记一次降级；恰好在触顶那一发响一次。逐次不响——几十个降级是常态噪声，
        它们的账由 `collect` 从 `via` 数进日报，这里只喊"这不再是个别票缺行"。"""
        nonlocal fallbacks
        fallbacks += 1
        if fallbacks == fallback_budget:
            alert(
                "分钟线降级预算耗尽",
                f"akshare→ngw 降级累计 {fallbacks} 次（预算 {fallback_budget}）：这不再是"
                "个别票缺行，是 sina 侧系统性出问题了。后续批次退回主源语义（空/错就响），"
                "先查 reports/minute/ 日报与 sina，别让 ngw 无声扛整源。",
            )

    def fetch(symbol: str, period: str) -> Grabbed:
        if period == NGW_PERIOD:
            if NGW_PERIOD not in periods:
                # 谁调谁错：`live_fetcher` 的 periods 与 `stores` 的键在 CLI 里是同一份清单，
                # 这条只可能出自装配 bug。响在这里比把 sina 的 1 分钟行送进 ngw 解析便宜。
                raise ValueError(
                    f"周期 {NGW_PERIOD} 要走 ngw，但 live_fetcher 建轮时 periods={tuple(periods)}"
                    " 不含它：抓取分派与 stores 的键必须是同一份清单"
                )
            return ngw_grab(symbol, period)
        try:
            rows = akshare_fetch.minute_frame(symbol, period)
        except Exception:
            if fallbacks >= fallback_budget:
                raise  # 预算耗尽：把主源的原始错交出去，不悄悄吞
            downgrade()
            return ngw_grab(symbol, period)
        if rows:
            return Grabbed(rows, VIA_AKSHARE)
        if fallbacks >= fallback_budget:
            return Grabbed(rows, VIA_AKSHARE)  # 空帧照旧：主源给了空就是空，预算不再降级
        downgrade()
        return ngw_grab(symbol, period)

    return fetch


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
    downgrades: list[str] = []
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
            drafts = drafts_for(grabbed, symbol=symbol, period=period)
            if not drafts:
                # 请求成功、帧是空的：不是"抓取失败"，也不是一批可判的数据。理由同 `daily`：
                # 交给门禁会得到 R006 的整批 FATAL，而那条要扣整源 40 分。
                skipped.append(Skipped(symbol=label(symbol, period), reason=REASON_NO_ROWS))
                continue
            if period != NGW_PERIOD and grabbed.via == VIA_NGW:
                # 判成了才算降级：抓取层说"我兜了底"而门禁把整批拒了的账，不进日报那一节。
                downgrades.append(label(symbol, period))
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
        downgrades=tuple(downgrades),
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
    刚落的行。这个参数因此没有默认值。深度读的是同一个 root：传错的报法是"盘上没有一行分钟线"，
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


def _downgrade_section(tally: Tally) -> str:
    """「今天哪些票靠 ngw 兜底」单独成节。降级不藏进各源表：那张表说的是分数，这节说的是账。

    没降级就整节省掉——日报的存在感来自异常，零异常的节每天占两行，看的人学会跳过它之后，
    真出事那天他也会跳过。
    """
    if not tally.downgrades:
        return ""
    shown = "、".join(tally.downgrades[:KIND_SAMPLE])
    more = (
        f"（其余 {len(tally.downgrades) - KIND_SAMPLE} 组未列）"
        if len(tally.downgrades) > KIND_SAMPLE
        else ""
    )
    return (
        "\n\n## 源降级（akshare→ngw）\n\n"
        f"- {len(tally.downgrades)} 组判成：{shown}{more}\n"
        "- 行的源标识是 `ngw_minute_*`；sina 恢复重跑同一天会按主键覆盖回主源口径。\n"
    )


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
            # 一句话覆盖两种"没读到行"：一个文件都没有，和文件在而行是零。分开写是给报告的人多
            # 一条他并不需要的分支——真要查，看一眼 minute_60/ 目录就知道是哪一种。
            lines.append(f"- {dataset}：盘上没有一行分钟线，报不出区间")
            continue
        lines.append(
            f"- {dataset}：{cover.first.isoformat()} .. {cover.last.isoformat()}"
            f"，{cover.days} 个有行交易日 × {cover.symbols} 只票"
        )
    # 「起点那天不足全天」的注脚按源说：1970 根窗口是 sina 三周期的口径，挂到 ngw 的
    # minute_1 上是替源传错话（ngw 是单页截断 + start 翻页，B 刀实测）。
    ngw_dataset = layout.minute_dataset(NGW_PERIOD)
    covered = [name for name, cover in recon.covers.items() if cover is not None]
    if any(name != ngw_dataset for name in covered):
        lines.append(
            "- 起点那天是从源的 1970 根窗口里掉出来的，通常不足全天：算段长别把它当完整的一天"
        )
    if ngw_dataset in covered:
        lines.append(
            f"- {ngw_dataset} 的起点那天是 ngw 单页（count={MAX_KLINE_COUNT}，约 "
            f"{MAX_KLINE_COUNT / BARS_PER_DAY_1M:.1f} 个交易日）截出来的，通常不足全天："
            "算段长别把它当完整的一天"
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
        extra=_disk_sections(recon, tuple(stores)) + _downgrade_section(tally),
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


def _tail_urgency(periods: Sequence[str]) -> str:
    """“这一天的尾巴漏了有多急”按本次跑的源分开说——两种急法处置不同。

    - sina 三周期：固定 1970 根 tail，滑出窗口的日子**主源**再也给不出（ADR-0009 代价三）。
      但任务 #64 起这不是终点：ngw 侧有 `start` 翻页，显式重跑 `zx-minute` 就能按同口径补回
      （降级在抓取层自动发生，事后回填则要人发起）——要补，只是不会自己补上。
    - ngw（`NGW_PERIOD`）：单页 `count=MAX_KLINE_COUNT` 截一段最近的尾巴，另有 `start`
      截止参数能向过去翻页（B 刀实测翻到 2019）——漏的这几天**补得回来**，先查源与网络，
      不必按"主源已滑窗"的节奏处置。

    只跑其中一半就只说那一半：把 sina 窗口句挂到纯 ngw 的告警上，会把"翻页可补"误报成
    "只剩主源这一家"；反向挂则会把"显式重跑才有"说成"自动兜底"。
    """
    parts: list[str] = []
    if any(period != NGW_PERIOD for period in periods):
        parts.append(
            "分钟线（sina 主源）只能取回源窗口内的最近 1970 根：这一天的尾巴滑出窗口，主源"
            "就再也给不出了；ngw 兜底有 start 翻页，重跑 zx-minute 按同口径补得回来（#64）。"
        )
    if NGW_PERIOD in periods:
        parts.append(
            f"1 分钟（ngw）单页 count={MAX_KLINE_COUNT}（约 "
            f"{MAX_KLINE_COUNT / BARS_PER_DAY_1M:.1f} 个交易日），且有 start 截止参数可向过去"
            "翻页（B 刀实测翻到 2019）：漏的这几天补得回来，先查源与网络。"
        )
    return "".join(parts)


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
            f"{result.day} 到底在不在盘上，看日报的 R011 那一节。" + _tail_urgency(result.periods),
        )
    elif result.fatal:
        count = sum(1 for outcome in result.tally.outcomes if outcome.has_fatal)
        alert(
            f"分钟线整批拒收：{result.day}",
            f"{count} / {len(result.tally.outcomes)} 个批次被 FATAL 拦下，一行都没进干净区。"
            "多半是源改了列名或日期读不出；原因见 reports/minute/ 的日报 FATAL 栏。",
        )
