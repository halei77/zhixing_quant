"""TypeSafe Jev(System One)判定客户端——把"语义判断"当编程原语接进来。

Jev 不生成文本,对自然语言 state 返回**类型化答案 + 概率**(Noul 0-1 / Choice / Score)。
代码管流程与执行,模型只供"普通代码做不了的语义判断"。文档:https://docs.typesafe.ai/api.md

设计口径:
- 不加第三方依赖:项目依赖表没有 http 库,一次 POST 用 stdlib urllib 足够;真要上 SDK 再进 diff。
- key 从环境变量读,不落仓库、不进数据根——与 relay key 同一条理由(config.relay_key_dir):
  这里的 key 对应的是 typesafe 账号,跟着备份树扩散等于密钥进网盘。
- 判定函数不猜不兜底:HTTP 非 2xx / 超时直接抛,调用方决定降级策略(回测里丢一个判定
  和实盘里丢一个判定,该响的话不一样)。
"""

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

JsonBody = dict[str, Any]
PostFn = Callable[[str, bytes, str, float], JsonBody]

API_ENV = "TYPESAFE_API_KEY"
API_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"
DEFAULT_TIMEOUT = 30.0


def api_key(env: Mapping[str, str] | None = None) -> str:
    """TypeSafe API key。缺了就 ValueError——静默返回空串会变成 401 混在服务故障里。"""
    raw = (os.environ if env is None else env).get(API_ENV, "").strip()
    if not raw:
        raise ValueError(f"没配 {API_ENV}。key 在 TypeSafe 控制台,不进仓库不进数据根")
    return raw


def _http_post(url: str, body: bytes, key: str, timeout: float) -> JsonBody:
    """唯一的 HTTP 出口,单测里替换的就是它。非 2xx 把状态码带进异常话里。"""
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload: JsonBody = json.loads(resp.read().decode("utf-8"))
            return payload
    except urllib.error.HTTPError as e:
        # e.read() 在 fp 缺失时(如测试里手造的 HTTPError)会自己再抛——响应体拿不到
        # 不该盖住真正的错误话,状态码才是要紧的。
        try:
            detail = e.read()[:200]
        except Exception:
            detail = b""
        raise RuntimeError(f"Jev API 返回 HTTP {e.code}:{detail!r}") from e


def ask(
    state: object,
    questions: Mapping[str, Mapping[str, object]],
    *,
    model: str = DEFAULT_MODEL,
    key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    post: PostFn = _http_post,
) -> JsonBody:
    """一次 SystemOne 请求:同一份 state 上的多个独立判定并行返回。

    `questions` 形如 {"qid": {"type": "noul", "instructions": "…"}};choice/score 的
    criteria 等字段按原样透传。返回 answers 按 qid 取,usage 里带 token 计数。
    """
    body = json.dumps({"state": state, "model": model, "questions": dict(questions)}).encode(
        "utf-8"
    )
    resp = post(API_URL, body, key if key is not None else api_key(), timeout)
    answers = resp.get("answers")
    if not isinstance(answers, dict):
        raise RuntimeError(f"Jev 响应里没有 answers 字段:{list(resp)}")
    return resp


def breakout_confirmation(state: str, **kw: Any) -> float:
    """领域判定:这段行情描述是否构成一次**有效放量突破**(Noul 0-1)。

    只问这一件事——"是不是有效突破"与"要不要下单"是两个判定,混在一个问题里
    得到的概率既不是置信度也不是仓位。0.5 上下=两可,阈值该由调用方按数据定。
    """
    resp = ask(
        state,
        {
            "breakout_confirmed": {
                "type": "noul",
                "instructions": (
                    "根据这段 A 股行情描述,今天是否可以确认一次有效放量突破?"
                    "有效=收盘价站上参照均线且成交量显著放大,不是盘中瞬时冲高。"
                ),
            }
        },
        **kw,
    )
    answer = resp["answers"]["breakout_confirmed"]
    return float(answer["noul"])
