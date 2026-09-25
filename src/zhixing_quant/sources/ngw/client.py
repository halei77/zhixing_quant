"""牛股王（niuguwang）HTTP 客户端：全项目唯一碰 ngw 网络的地方（04 §五 抓包源专项）。

分工与 akshare 那层的抓取边界同一条：适配器一律不联网（黄金样本要在 CI 里离线重放，
03 §二 L2），联网只在这一层；本层不做任何数据判定，发完把响应体交出去就完。

四条接口全部 2026-09-24 实测过（详见数据根
`reports/source-probe/2026-09-24-ngw-niuguwang-source-access.md`）：

- 检索 `search/v1/homesearch`：代码 → `innercode`（K线接口的 `code` 参数要的是它）；
- K线 `dk/kline`：`type=11` 是 1 分钟；缺 `usertoken` 也 200 出数；
- 估值快照 `quotedata/stockshare`、三大表 `finacetab/finacereport`：基本面骨架的两个原料。

纪律（Qoute T-001 §1 与 ngw_provider 的实测遗产，封禁风险是纪律挡下来的）：

- **串行 + pacer**：默认 0.3s 起步，一把进程级锁把并发调用也排成队——429 阈值未知，
  Qoute 按 1.5s 跑一个月无事故，首周批量抓取把 `interval` 调到 1.5 再跑。
- **429/5xx 阶梯退避 5/15/45s**（Qoute `NgwHttpClient._get` 同款阶梯），走完才抛；
  `sleep`/`clock` 是注入点，测试不真等。
- **token 读盘不入库**：默认 `~/.zhixing_secrets/ngw.token`（与 rds/promax 同目录纪律），
  `NIUGUWANG_TOKEN_PATH` 覆盖。文件缺失/为空**照样发请求**——实测无 token 200 出数。
  token 不进日志、不进异常文本（异常里的 URL 只带脱敏后的查询串）。
- **`count` 上限钉死在发请求之前**：实测 `count≥1500` 源侧静默返回 0 行（不是 4xx），
  发出去只会把「被拒的参数」误读成「历史到底了」，所以这里直接拦（硬约束 3）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, NamedTuple

from zhixing_quant.domain.symbol import normalize_code

#: token 落位与覆盖（ADR-0014 决定 5 同款纪律：git 外、禁入库、禁入聊天、禁入文档）。
TOKEN_ENV = "NIUGUWANG_TOKEN_PATH"
DEFAULT_TOKEN_PATH = Path.home() / ".zhixing_secrets" / "ngw.token"

SEARCH_URL = "https://api.niuguwang.com/search/v1/homesearch"
KLINE_URL = "https://api.niuguwang.com/dk/kline"
STOCKSHARE_URL = "https://hqastock.niuguwang.com/api/quotedata/stockshare"
FINACEREPORT_URL = "https://stockdata.niuguwang.com/api/v1/finacetab/finacereport"

#: 实测返回 200 的那套头：UA 仿 Android APP，Referer/Origin 钉网页端源站。
HEADERS: Mapping[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 13; V2307A; wv) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 MQQBrowser/14.9 "
        "Mobile Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://swww.niuguwang.com/",
    "Origin": "https://swww.niuguwang.com",
}

#: 四条接口共用的协议参数（接口描述.txt 与实测 URL 逐字一致）。
APP_PARAMS: Mapping[str, str] = {"s": "QQ", "version": "7.2.1", "packtype": "1", "night": "0"}

#: 429/5xx 阶梯退避秒数（Qoute ngw_provider 同款：0 → 5 → 15 → 45）。
BACKOFF: tuple[int, ...] = (5, 15, 45)

#: pacer 起步间隔（秒）。0.3s 是本项目定的起步值；首周批量按 Qoute 纪律调 1.5s。
DEFAULT_MIN_INTERVAL = 0.3

#: 单页 K线行数上限。实测 1400/1450 通、1500 静默 0 行（硬约束 3）。
MAX_KLINE_COUNT = 1400

#: 1 分钟K 的 `type`（实测全表：11=1分、1=5分、2=15分、3=30分、4=60分、5=日、6=周、9=月；
#: 笔记写 3=60分/7=周 是错的——7 返回空）。
KLINE_TYPE_1M = "11"

#: **周期（分钟）→ `type` 的单点映射**（任务 #64，2026-09-25 实测 600519 起跨板块抽查：
#: 15分 `type=2` 也通，但 layout 没有 minute_15 dataset，收进来就是第二个"有源没处落"的洞）。
#: 分派处（`jobs.minute`）只读这一份——周期表漂成两份的那条路（抓 ngw、解析按 sina）从这里堵死。
KLINE_TYPES: Mapping[str, str] = {"1": "11", "5": "1", "30": "3", "60": "4"}

#: 传输边界：完整 URL（含查询串）+ 头 + 超时 → (状态码, 响应体)。4xx 以状态码回，不抛。
Transport = Callable[[str, Mapping[str, str], float], tuple[int, bytes]]


def _urlopen_transport(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    """默认传输：urllib 单请求（项目无 requests 直依赖，与 relay 客户端同选择）。"""
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        # 非 2xx 走状态码分支（与 429 阶梯共用一条路），body 一并交出去供诊断。
        return int(exc.code), exc.read()


class NgwError(RuntimeError):
    """ngw 侧的业务/协议失败：形状不对、检索无唯一匹配、JSON 解不出。"""


class NgwHttpError(NgwError):
    """HTTP 层失败，退避阶梯走完仍然没拿到。"""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"ngw HTTP {status}: {detail}")
        self.status = status


class SearchHit(NamedTuple):
    """检索命中的一只标的。`innercode` 才是 K线接口的 `code`，六位码不是。

    `boardname`/`tag_display` 是同码消歧的依据：000001 同时命中「上证指数」（指数、
    boardName 空、tagDisplay=指数）与「平安银行」（主板、深A），个股路径靠这两个字段挑。
    """

    innercode: str
    stockcode: str
    stockname: str
    market: str
    boardname: str = ""
    tag_display: str = ""


class KlinePage(NamedTuple):
    """一页 K线。`exhausted` 是「到底了」的**唯一**判据（硬约束 3）。

    0 行在 count≤1400 的前提下只有一种含义：源侧没有 ≤ 截止时间的更早数据——翻页回填
    可以据此收工。而 count≥1500 的静默 0 行被 `kline` 的参数校验挡在发请求之前，
    不会混进来冒充「到底了」。
    """

    rows: tuple[Mapping[str, object], ...]
    exhausted: bool


def _read_token(path: Path | None) -> str | None:
    """token 从盘上读一次。缺失/为空给 None——实测无 token 照样 200 出数，缺 token 不是错误。"""
    if path is not None:
        candidate = path
    else:
        raw = os.environ.get(TOKEN_ENV, "").strip()
        candidate = Path(raw).expanduser() if raw else DEFAULT_TOKEN_PATH
    try:
        text = candidate.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


class NgwClient:
    """串行、限速、带阶梯退避的 ngw 客户端。一只票一个方法调用，不并发。"""

    def __init__(
        self,
        *,
        token_path: Path | None = None,
        interval: float = DEFAULT_MIN_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        transport: Transport | None = None,
        backoff: tuple[int, ...] = BACKOFF,
    ) -> None:
        self._token = _read_token(token_path)
        self._interval = interval
        self._sleep = sleep
        self._clock = clock
        self._transport = transport if transport is not None else _urlopen_transport
        #: 阶梯可由装配裁短（ADR-0026 实时路：(5,) 最坏 ~20s；采集/回填保持默认 (5,15,45)）
        self._backoff = backoff
        # 一把锁贯穿「pacer 等待 → 请求 → 退避」整个周期：并发调用在这里排成队，
        # 禁并发不是靠调用方自觉（Qoute 纪律：串行是它没被封的原因）。
        self._lock = threading.Lock()
        self._last_at: float | None = None

    @property
    def has_token(self) -> bool:
        """有没有 token 只影响请求头，不影响能不能发（实测无 token 200 出数）。"""
        return self._token is not None

    def _pace(self) -> None:
        """锁内调用：距上一次请求不足 `interval` 就补睡差额。"""
        if self._last_at is None:
            return
        gap = self._interval - (self._clock() - self._last_at)
        if gap > 0:
            self._sleep(gap)

    def _get(
        self, url: str, params: Mapping[str, object], *, timeout: float, pacer: bool = True
    ) -> dict[str, Any]:
        """GET + JSON。429/5xx/网络错走 5/15/45s 阶梯；其余状态码立刻响。"""
        with self._lock:
            if pacer:
                self._pace()
            query = {key: str(value) for key, value in params.items()}
            if self._token is not None:
                query["usertoken"] = self._token
            # 异常文本只带脱敏串：带 token 的完整 URL 不许进日志与异常（ADR-0014 决定 5）。
            display = urllib.parse.urlencode({k: v for k, v in query.items() if k != "usertoken"})
            target = f"{url}?{display}"
            request_url = f"{url}?{urllib.parse.urlencode(query)}"
            last = "未知错误"
            for delay in (0, *self._backoff):
                if delay:
                    self._sleep(delay)
                    self._pace()
                try:
                    status, body = self._transport(request_url, HEADERS, timeout)
                except OSError as exc:
                    last = f"{type(exc).__name__}: {exc}"
                    continue
                if 200 <= status < 300:
                    try:
                        decoded: Any = json.loads(body.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise NgwError(f"{target} 返回的不是 JSON：{exc}") from exc
                    if not isinstance(decoded, dict):
                        raise NgwError(f"{target} 返回的 JSON 不是对象：{type(decoded).__name__}")
                    self._last_at = self._clock()
                    return decoded
                last = body[:200].decode("utf-8", "replace")
                if status == 429 or 500 <= status < 600:
                    continue
                raise NgwHttpError(status, f"{target} → {last}")
            raise NgwHttpError(0, f"{target} 阶梯走完仍失败：{last}")

    def search(self, query: str) -> tuple[SearchHit, ...]:
        """代码/名字 → 检索命中的标的列表。唯一匹配的便捷入口是 `innercode`。"""
        body = self._get(
            SEARCH_URL,
            {
                "q": query,
                "isNeedColor": "true",
                "isNeedOtcH5Url": "true",
                "market": "1,2,3,4,7,8,9,200",
                **APP_PARAMS,
            },
            timeout=30.0,
        )
        stocks = body.get("stocks") or []
        if not isinstance(stocks, list):
            raise NgwError(f"stocks 不是列表：{type(stocks).__name__}")
        hits: list[SearchHit] = []
        for item in stocks:
            if not isinstance(item, dict):
                raise NgwError(f"stocks 里混进了 {type(item).__name__}")
            hits.append(
                SearchHit(
                    innercode=str(item.get("innercode") or ""),
                    stockcode=str(item.get("stockcode") or ""),
                    stockname=str(item.get("stockname") or ""),
                    market=str(item.get("market") or ""),
                    boardname=str(item.get("boardName") or ""),
                    tag_display=str(item.get("tagDisplay") or ""),
                )
            )
        return tuple(hits)

    def innercode(self, symbol: str) -> str:
        """六位码 → 个股的唯一 `innercode`。检索不到唯一匹配就响，不拿第一个凑。

        同码双命中实测存在（000001 = 上证指数 + 平安银行）：个股先按「有板块名且 tagDisplay
        不是指数」收窄，收窄后唯一即取；指数/ETF 不在本路径（站点的指数走 `index_daily`
        参考表，ADR-0012）。
        """
        code = normalize_code(symbol)
        matches = tuple(hit for hit in self.search(code) if hit.stockcode == code)
        equity = tuple(hit for hit in matches if hit.boardname and hit.tag_display != "指数")
        pool = equity or matches
        if len(pool) != 1:
            raise NgwError(
                f"检索 {code} 得到 {len(pool)} 个可用匹配"
                f"（共 {len(matches)} 命中），没有唯一 innercode"
            )
        return pool[0].innercode

    def kline(
        self,
        innercode: str,
        *,
        count: int,
        ktype: str = KLINE_TYPE_1M,
        start: str | None = None,
        ex: str | None = None,
        timeout: float = 60.0,
    ) -> KlinePage:
        """一页 K线（`start` 是**截止**时间，向过去翻页；不传 = 从最新往回）。

        `ex` 不传 = 不复权（ADR-0009 决定 4 的口径，硬约束 2）；1=前复权、2=后复权，
        仅在显式要别的口径时才传。

        **回页归一成升序**：源的契约是倒序（新→旧，配合 `start` 向过去翻——实测首行
        最新、末行最早），而 R009 的「乱序」判据与落盘合并都按升序。倒序是契约不是故障，
        在协议这一层定序；适配器照旧「源给什么序就是什么序」（akshare 同款分工）。
        """
        if not 1 <= count <= MAX_KLINE_COUNT:
            raise ValueError(
                f"count={count} 越界（1–{MAX_KLINE_COUNT}）：实测 count≥1500 源侧静默返回 "
                "0 行（不是 4xx），发出去只会把「被拒的参数」误读成「历史到底了」"
            )
        params: dict[str, object] = {"code": innercode, "count": count, "type": ktype}
        if start is not None:
            params["start"] = start
        if ex is not None:
            params["ex"] = ex
        params.update(APP_PARAMS)
        body = self._get(KLINE_URL, params, timeout=timeout)
        raw = body.get("timedata") or []
        if not isinstance(raw, list):
            raise NgwError(f"timedata 不是列表：{type(raw).__name__}")
        if any(not isinstance(item, dict) for item in raw):
            raise NgwError("timedata 里混进了非对象行")
        rows: tuple[Mapping[str, object], ...] = tuple(
            sorted(raw, key=lambda row: str(row.get("times") or ""))
        )
        return KlinePage(rows=rows, exhausted=not rows)

    def stockshare(self, innercode: str) -> dict[str, Any]:
        """估值/股本快照（`code` 收 innercode，实测参数就这一个业务键）。"""
        return self._get(
            STOCKSHARE_URL,
            {"code": innercode, **APP_PARAMS},
            timeout=30.0,
        )

    def finacereport(self, stockcode: str, *, reporttype: str) -> dict[str, Any]:
        """三大表 + 主要指标（`tradingCode` 收六位码，不是 innercode）。"""
        return self._get(
            FINACEREPORT_URL,
            {"tradingCode": stockcode, "reporttype": reporttype, **APP_PARAMS},
            timeout=30.0,
        )
