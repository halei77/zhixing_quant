"""转接源客户端（ADR-0014 决定 2、5）：GET + `X-API-Key`，限流退避，双源切换。

全项目唯一碰转接源网络的地方——适配器与装载器不联网（与 akshare 那层的同一分工：
黄金样本要在 CI 里离线重放，会自己联网的适配器重放不出任何断言）。

退避与切换是 ADR-0014 决定 2 定死的：429/503 按首选源 1→5→30 分钟阶梯退避（429 的
`Retry-After` 比阶梯值大时取大者），本源阶梯走完切下一个源重试同一请求，全部走完才报
"没开始"。`sleep` 是注入点——测试不能真等 36 分钟，真跑也不该由调用方关心等了多久。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from zhixing_quant import config

#: 双源入口（ADR-0014 决定 2；与 tools/probe_sources.py 同源——那边是探测工具自带的
#: 一份，改 URL 两处都要改，探测与真跑打不同服务的代价值得记着）。
RELAYS: Mapping[str, str] = {
    "rds": "http://datahubco.com/app-api/openapi/v1/tushare",
    "promax": "https://pcd.mobcvb.cn/tushare/pro",
}

#: 退避阶梯（秒）。1 → 5 → 30 分钟，之后换源。
BACKOFF_SECONDS: Sequence[int] = (60, 300, 1800)


class RelayUnavailable(RuntimeError):
    """所有源全走完仍然没拿到——调用方按"今天没开始"处置，不是数据问题。"""


class RelayHttpError(RuntimeError):
    """一次 HTTP/业务层失败，带重试头。`fetch` 按它决定退避多久还是换源。"""

    def __init__(self, relay: str, status: int, retry_after: str | None, detail: object) -> None:
        super().__init__(f"{relay} HTTP {status}: {detail}")
        self.relay = relay
        self.status = status
        self.retry_after = retry_after


def _key(relay: str) -> str:
    text = config.relay_key_file(f"{relay}.key").read_text(encoding="utf-8").strip()
    if not text:
        raise RelayUnavailable(f"{relay} 的 key 文件是空的：配上再跑")
    return text


def _get(relay: str, api: str, params: Mapping[str, object], timeout: float) -> dict[str, Any]:
    query = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
    request = urllib.request.Request(
        f"{RELAYS[relay]}/{api}?{query}", headers={"X-API-Key": _key(relay)}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RelayHttpError(
            relay, exc.code, exc.headers.get("Retry-After"), exc.read()[:200]
        ) from exc
    if body.get("code") != 0:
        # code=0 但 items 为空不是错（那天就是没数据）；code!=0 是接口级失败，如实报。
        raise RelayHttpError(relay, 200, None, f"code={body.get('code')} msg={body.get('msg')}")
    return body


def fetch(
    api: str,
    params: Mapping[str, object],
    *,
    relays: Sequence[str] = ("rds", "promax"),
    timeout: float = 90.0,
    sleep: Callable[[float], None] = time.sleep,
    backoff: Sequence[int] = BACKOFF_SECONDS,
) -> tuple[str, dict[str, Any]]:
    """拿一张表的一页。返回 (源名, 响应体)——源名进报告，哪一源供的数要留痕。

    对每个源按阶梯退避，走完切下一个源重试同一请求；全走完抛 `RelayUnavailable`。
    """
    errors: list[str] = []
    for relay in relays:
        for attempt in range(len(backoff) + 1):
            try:
                return relay, _get(relay, api, params, timeout)
            except RelayHttpError as exc:
                errors.append(f"{exc}（第 {attempt + 1} 次）")
                if attempt == len(backoff):
                    break
                wait = backoff[attempt]
                given = exc.retry_after
                if given is not None and given.isdigit():
                    wait = max(wait, int(given))  # Retry-After 取其与阶梯值的较大者（Qoute §7）
                sleep(wait)
            except (OSError, ValueError) as exc:
                errors.append(
                    f"{relay}: {type(exc).__name__}: {str(exc)[:120]}（第 {attempt + 1} 次）"
                )
                if attempt == len(backoff):
                    break
                sleep(backoff[attempt])
    raise RelayUnavailable(
        f"{api} 在 {'、'.join(relays)} 上全部走完退避阶梯仍失败：{'；'.join(errors[-4:])}"
    )
