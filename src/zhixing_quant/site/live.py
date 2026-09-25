"""分钟K活取源（ADR-0026）：站点生成之路对分钟段的实时抓取——sina 主、ngw 兜底。

组合根装配（ADR-0012 决定 1）：`zx-site` 的 `main()` 里 `make_minute_source()` 造一个带
缓存与限速的闭包，作为 `minute_source` 注入 `prompt.build`——`build` 层依旧不自己起 HTTP，
联网的事实全部装在这里。测试注入假 provider；本层的抓取用可注入 transport 的离线件测
（03 §二 L2，`tests/sources/jobs/test_minute.py` 的 `_stub_ngw_client` 同一手法）。

三条实测规矩（2026-09-25 生产服务器探针，报告落数据根
reports/source-probe/2026-09-25-site-live-minute-sources.md）：

1. **akshare 内部 requests 不带 timeout**——挂住 = 站点工作线程漏光。进程级
   `socket.setdefaulttimeout` 兜底（本模块唯一一次全局副作用，只装在 zx-site 的装配路径；
   ngw 侧走自己显式超时的 transport，不受影响）。
2. **实时的降级阶梯裁短**：NgwClient 默认阶梯 (5,15,45) 最坏 ≈125s，生成请求等不起——
   这里给 client 传 (5,)：一发退避不成即弃；兜底也失败由调用方出交代（ADR-0020）。
3. **"抓不到"不只有异常和空帧**：指数/退市/北交所票 sina 会回一整帧陈旧数据（实测末根
   停在 2009/2020/上月）——不抛不空。这类行被窗口筛选自然清空（模板要的那几天一行都没有
   → 交代），本层不造第二判据；对陈旧行再抓 ngw 只会多花一秒拿回同样的旧数据。
"""

from __future__ import annotations

import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from datetime import date, datetime
from datetime import time as time_of
from typing import Protocol

from zhixing_quant.domain.bar import Bar
from zhixing_quant.site.prompt import MinuteSource
from zhixing_quant.sources.akshare import fetch as akshare_fetch
from zhixing_quant.sources.jobs.minute import NGW_PERIOD, VIA_NGW, Grabbed, drafts_for
from zhixing_quant.sources.ngw import client as ngw_client

#: sina 一次请求的墙钟上限（实测中位 0.75~1.4s，8s 是 4 倍余量）——经全局 socket 默认值生效。
SINA_SOCKET_TIMEOUT = 8.0
#: ngw kline 深页实测 ~1s，15s 上限；检索走默认 30s 的调用方不超预算（budget 见 fetch）。
NGW_KLINE_TIMEOUT = 15.0
#: 实时路的退避阶梯：一发 5s 即弃（完整阶梯是采集路的纪律，见 client.py）。
NGW_LIVE_BACKOFF: tuple[int, ...] = (5,)
#: ngw 起步限速：服务器实测连发 10 页无 429（探针 §2），0.3s 是 client 的通用起步值。
NGW_LIVE_INTERVAL = 0.3

CACHE_TTL_TRADING = 90.0
CACHE_TTL_QUIET = 3600.0
#: 帧数上限按实测体积定：sina 一帧归一后 ≈0.3MB，1500 帧 ≈450MB——服务器可用 ~960MB
#: （2026-09-25 探针 §4），留一半给进程与并发峰值。
CACHE_FRAMES = 1500
#: 全局限速：≤20 源请求/分钟（并发 4、单请求 ~1s 的吞吐上限远高于此，桶是闸不是节流）。
RATE_PER_MIN = 20.0
MAX_CONCURRENCY = 4
#: 排队预算（秒）：排不进/等不起 = 本次没取到（返回空，段出交代），不 500、不假数。
QUEUE_BUDGET = 20.0


class NgwLike(Protocol):
    """`make_minute_source` 要的全部能力：两个方法。真 `NgwClient` 与测试替身都按形状满足
    （参数名与真 `NgwClient.kline` 一致：mypy 的结构匹配认关键字名）。"""

    def innercode(self, symbol: str) -> str: ...

    def kline(self, innercode: str, **kw: object) -> ngw_client.KlinePage: ...


#: 交易时段（含开/收盘竞价尾）。判断拿不准时宁可判成交易时段——TTL 取小，缓存更保守。
_TRADING_WINDOW = (time_of(9, 20), time_of(15, 10))

#: dataset → 周期：`minute_5` → `5`。不 import layout（那是 storage 包——边界纪律
#: ADR-0012 决定 1：capability 层不碰盘）；`site/{a}_{b}` 的命名形状在 ADR-0009 定死，这里按形拆。
_MINUTE_PREFIX = "minute_"


class _Bucket:
    """令牌桶（锁内结算）：站点单进程，桶就是全局限速的唯一事实点。"""

    def __init__(
        self, rate_per_min: float, *, clock: Callable[[], float], sleeper: Callable[[float], None]
    ) -> None:
        self._rate = rate_per_min / 60.0
        self._clock = clock
        self._sleep = sleeper
        self._tokens = rate_per_min
        self._last = clock()
        self._lock = threading.Lock()

    def take(self, budget_s: float) -> bool:
        """取 1 枚令牌；不够就等到预算耗尽。True = 可以发源请求。"""
        deadline = self._clock() + budget_s
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(
                    self._rate * 60.0, self._tokens + (now - self._last) * self._rate
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 - self._tokens) / self._rate
            if now + wait > deadline:
                return False
            self._sleep(min(wait, 0.5))


def _trading_now(moment: datetime) -> bool:
    return _TRADING_WINDOW[0] <= moment.time() <= _TRADING_WINDOW[1]


def _period_of(dataset: str) -> str:
    return dataset.removeprefix(_MINUTE_PREFIX)


def make_minute_source(
    *,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = datetime.now,
    sleeper: Callable[[float], None] = time.sleep,
    ngw: NgwLike | None = None,
    rate_per_min: float = RATE_PER_MIN,
) -> MinuteSource:
    """造分钟活取源：缓存/令牌桶/并发闸/单飞都在闭包里，一次装配一份事实。

    **只联网、不碰盘**（ADR-0026 决定 1 的分工，用户拍板 2026-09-25）：这里交出的 Bar 是
    **原始价**，后复权由 `prompt.py`（组合根、干净区唯一的读者）现算——换算只许有一处，
    那处在读盘的那个文件里。`root` 参数因此不存在；`ngw`、`clock`、`now`、`sleeper`、
    `rate_per_min` 是装配与测试注入点。副作用只有一处：`socket.setdefaulttimeout`——
    akshare 不给内部请求传 timeout，全局兜底是唯一的挂死防线，本函数只应在 zx-site 的
    装配路径调用一次。
    """
    if socket.getdefaulttimeout() is None:
        socket.setdefaulttimeout(SINA_SOCKET_TIMEOUT)
    client = ngw or ngw_client.NgwClient(interval=NGW_LIVE_INTERVAL, backoff=NGW_LIVE_BACKOFF)
    bucket = _Bucket(rate_per_min, clock=clock, sleeper=sleeper)
    slots = threading.BoundedSemaphore(MAX_CONCURRENCY)
    guard = threading.Lock()
    cache: OrderedDict[tuple[str, str], tuple[float, tuple[Bar, ...]]] = OrderedDict()
    inflight: dict[tuple[str, str], threading.Event] = {}
    innercodes: dict[str, str] = {}

    def _ttl() -> float:
        return CACHE_TTL_TRADING if _trading_now(now()) else CACHE_TTL_QUIET

    def _grab(code: str, period: str) -> Grabbed:
        """一次源请求（闸内）：sina 主 → 异常/空帧降级 ngw（#64 的 via 语义原样）。"""
        if period != NGW_PERIOD:
            try:
                rows = akshare_fetch.minute_frame(code, period)
            except Exception:
                rows = []
            if rows:
                return Grabbed(rows, "akshare")
        try:
            inner = innercodes.get(code)
            if inner is None:
                inner = client.innercode(code)
                innercodes[code] = inner
            page = client.kline(
                inner,
                count=ngw_client.MAX_KLINE_COUNT,
                ktype=ngw_client.KLINE_TYPES[period],
                timeout=NGW_KLINE_TIMEOUT,
            )
        except Exception:
            return Grabbed((), VIA_NGW)
        return Grabbed(page.rows, VIA_NGW)

    def _raw_bars(code: str, period: str) -> tuple[Bar, ...]:
        with slots:
            grabbed = _grab(code, period)
        bars: list[Bar] = []
        for draft in drafts_for(grabbed, symbol=code, period=period):
            if draft.trade_date is None or draft.ts is None:
                continue  # 缺身份的行不进提示词（R010 的实时对应：这里不收，盘上也没有）
            try:
                bars.append(Bar.from_draft(draft))
            except Exception:
                continue
        return tuple(bars)

    def _window(bars: Sequence[Bar], start: date, end: date) -> list[Bar]:
        """只筛窗口、不换口径：复权是组合根的事（模块说明那条分工）。"""
        return [bar for bar in bars if start <= bar.trade_date <= end]

    def fetch_minute(code: str, dataset: str, start: date, end: date) -> list[Bar]:
        """`MinuteSource` 契约。一切失败收敛成空——生成之路不因一个组件挂掉而 500。"""
        period = _period_of(dataset)
        key = (code, period)
        if not bucket.take(QUEUE_BUDGET):
            return []
        fresh = _fresh(key)
        if fresh is not None:
            return _window(fresh, start, end)
        leader = False
        with guard:
            blocker = inflight.get(key)
            if blocker is None:
                inflight[key] = threading.Event()
                leader = True
        if not leader:
            assert blocker is not None  # 同一把锁下取的 blocker：不是leader就说明它当时在场
            if not blocker.wait(QUEUE_BUDGET):
                return []
            fresh = _fresh(key)
            return _window(fresh, start, end) if fresh is not None else []
        try:
            bars = _raw_bars(code, period)
        except Exception:
            return []  # 未预期错误同样收敛成空：生成之路不因组件挂掉而 500
        finally:
            with guard:
                done = inflight.pop(key, None)
            if done is not None:
                done.set()
        with guard:
            cache[key] = (clock(), bars)
            cache.move_to_end(key)
            while len(cache) > CACHE_FRAMES:
                cache.popitem(last=False)
        return _window(bars, start, end)

    def _fresh(key: tuple[str, str]) -> tuple[Bar, ...] | None:
        with guard:
            hit = cache.get(key)
            if hit is None:
                return None
            if clock() - hit[0] > _ttl():
                cache.pop(key, None)
                return None
            cache.move_to_end(key)
            return hit[1]

    return fetch_minute
