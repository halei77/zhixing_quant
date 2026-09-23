from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

import pytest

from zhixing_quant.jev import client

Captured = list[dict[str, Any]]
FakePost = Callable[[str, bytes, str, float], dict[str, Any]]


def _fake_post(resp: dict[str, Any], captured: Captured) -> FakePost:
    def post(url: str, body: bytes, key: str, timeout: float) -> dict[str, Any]:
        captured.append({"url": url, "body": body, "key": key, "timeout": timeout})
        return resp

    return post


def test_ask_构造请求并解析answers() -> None:
    captured: Captured = []
    resp = client.ask(
        "放量阳线突破20日线",
        {"qid": {"type": "noul", "instructions": "是否有效突破?"}},
        key="k-test",
        post=_fake_post({"answers": {"qid": {"type": "noul", "noul": 0.6}}, "usage": {}}, captured),
    )
    assert resp["answers"]["qid"]["noul"] == 0.6
    req = captured[0]
    assert req["url"] == client.API_URL
    assert req["key"] == "k-test"
    body = json.loads(req["body"])
    assert body["model"] == client.DEFAULT_MODEL
    assert body["questions"]["qid"]["type"] == "noul"


def test_breakout_confirmation_返回浮点概率() -> None:
    captured: Captured = []
    p = client.breakout_confirmation(
        "三连阳放量",
        key="k-test",
        post=_fake_post({"answers": {"breakout_confirmed": {"noul": "0.85"}}}, captured),
    )
    assert p == 0.85
    # 判定只问突破,不夹带"要不要下单"——一个问题一个语义
    q = json.loads(captured[0]["body"])["questions"]["breakout_confirmed"]
    assert q["type"] == "noul"
    assert "下单" not in q["instructions"]


def test_缺key直接响不静默(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(client.API_ENV, raising=False)
    with pytest.raises(ValueError, match="TYPESAFE_API_KEY"):
        client.ask("x", {"q": {"type": "noul", "instructions": "y"}}, post=_fake_post({}, []))


def test_非2xx把状态码带进异常话(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(_req: urllib.request.Request, **_kwargs: object) -> object:
        raise urllib.error.HTTPError(
            client.API_URL,
            401,
            "Unauthorized",
            None,  # type: ignore[arg-type]
            b"bad key",  # type: ignore[arg-type]
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="401"):
        client._http_post(client.API_URL, b"{}", "k", 30.0)


def test_响应缺answers响出来() -> None:
    with pytest.raises(RuntimeError, match="answers"):
        client.ask(
            "x",
            {"q": {"type": "noul", "instructions": "y"}},
            key="k",
            post=_fake_post({"error": "nope"}, []),
        )
