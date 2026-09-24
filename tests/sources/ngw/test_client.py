"""ngw 客户端 L1：pacer 串行、429/5xx 阶梯、token 缺失不炸、count 上限显式分支。

传输整体注入（`transport`）：这些测试一步网络都不发。判的东西是**纪律**——限速有没有真睡、
阶梯是不是 5/15/45、token 会不会漏进异常——这些在真网络上永远测不出红。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from zhixing_quant.sources.ngw import client as ngw


def _json(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


class ScriptedTransport:
    """按脚本吐 (status, body)，把每次调用的 URL 记下来供断言。"""

    def __init__(self, *responses: tuple[int, bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[str] = []

    def __call__(
        self, _url: str, _headers: Mapping[str, str], _timeout: float
    ) -> tuple[int, bytes]:
        self.calls.append(_url)
        if not self.responses:
            raise AssertionError(f"脚本用完了还来一发：{_url}")
        return self.responses.pop(0)


def _client(
    transport: ScriptedTransport,
    *,
    sleeps: list[float] | None = None,
    interval: float = 0.0,
    token_path: Path | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> ngw.NgwClient:
    if token_path is None:
        if monkeypatch is not None:
            # 走 env 覆盖这条路：把 NIUGUWANG_TOKEN_PATH 指到不存在的文件。
            monkeypatch.setenv(ngw.TOKEN_ENV, "/nonexistent/ngw.token")
        else:
            # 没给注入点时也必须隔离：真机 ~/.zhixing_secrets/ngw.token 是存在的，
            # 本机文件状态不许替测试做决定（有 token 的那条路由显式 token_path 测）。
            token_path = Path("/nonexistent/ngw.token")
    recorded = sleeps if sleeps is not None else []

    def sleep(seconds: float) -> None:
        recorded.append(seconds)

    return ngw.NgwClient(
        token_path=token_path,
        interval=interval,
        sleep=sleep,
        clock=lambda: 100.0,  # 冻结时钟：pacer 的差额计算因此是确定值
        transport=transport,
    )


# --- 检索 / innercode ---------------------------------------------------------


def test_search_maps_the_stock_entry_to_hits() -> None:
    transport = ScriptedTransport(
        (
            200,
            _json(
                {
                    "stocks": [
                        {
                            "innercode": "3143",
                            "stockcode": "600519",
                            "stockname": "贵州茅台",
                            "market": "1",
                        }
                    ]
                }
            ),
        )
    )
    client = _client(transport)
    (hit,) = client.search("600519")
    assert hit == ngw.SearchHit(
        innercode="3143", stockcode="600519", stockname="贵州茅台", market="1"
    )
    assert "usertoken" not in transport.calls[0]  # token 缺失时连参数都不带（缺失不是错误）


def test_innercode_disambiguates_the_index_from_the_stock_with_one_code() -> None:
    """000001 同码双命中（上证指数 + 平安银行，2026-09-24 实测）：个股路径靠板块名/tag 收窄。"""
    both = _json(
        {
            "stocks": [
                {
                    "innercode": "2318",
                    "stockcode": "000001",
                    "stockname": "上证指数",
                    "market": "3",
                    "boardName": "",
                    "tagDisplay": "指数",
                },
                {
                    "innercode": "1",
                    "stockcode": "000001",
                    "stockname": "平安银行",
                    "market": "2",
                    "boardName": "主板",
                    "tagDisplay": "深A",
                },
            ]
        }
    )
    assert _client(ScriptedTransport((200, both))).innercode("000001") == "1"


def test_innercode_requires_a_unique_exact_match() -> None:
    two = _json(
        {
            "stocks": [
                {"innercode": "1", "stockcode": "600519", "stockname": "贵州茅台", "market": "1"},
                {"innercode": "2", "stockcode": "600519", "stockname": "贵州茅台", "market": "1"},
            ]
        }
    )
    with pytest.raises(ngw.NgwError, match="没有唯一 innercode"):
        _client(ScriptedTransport((200, two))).innercode("600519")
    none = _json({"stocks": [{"innercode": "9", "stockcode": "000001", "stockname": "平安银行"}]})
    with pytest.raises(ngw.NgwError, match="0 个可用匹配"):
        _client(ScriptedTransport((200, none))).innercode("600519")


# --- token --------------------------------------------------------------------


def test_a_missing_token_file_still_sends_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺 token 继续跑是实测事实（无 usertoken 照样 200 出数），不是将就。"""
    transport = ScriptedTransport((200, _json({"stocks": []})))
    client = _client(transport, monkeypatch=monkeypatch)
    assert client.has_token is False
    client.search("600519")
    assert transport.calls and "usertoken" not in transport.calls[0]


def test_a_token_file_is_attached_but_never_logged(tmp_path: Path) -> None:
    token_file = tmp_path / "ngw.token"
    token_file.write_text("secret-token-for-test\n", encoding="utf-8")
    transport = ScriptedTransport((200, _json({"stocks": []})))
    client = _client(transport, token_path=token_file)
    client.search("600519")
    assert client.has_token is True
    assert "usertoken=secret-token-for-test" in transport.calls[0]
    # 异常文本只带脱敏查询串：token 不进日志/异常（ADR-0014 决定 5 同款纪律）。
    failing = ScriptedTransport((500, b"boom"), (500, b"boom"), (500, b"boom"), (500, b"boom"))
    with pytest.raises(ngw.NgwHttpError) as caught:
        _client(failing, token_path=token_file).search("600519")
    assert "secret-token-for-test" not in str(caught.value)


# --- 退避阶梯 -----------------------------------------------------------------


def test_429_walks_the_5_15_45_ladder_then_succeeds() -> None:
    sleeps: list[float] = []
    transport = ScriptedTransport(
        (429, b"slow down"),
        (429, b"slow down"),
        (429, b"slow down"),
        (200, _json({"stocks": []})),
    )
    client = _client(transport, sleeps=sleeps)
    client.search("600519")
    assert sleeps == [5.0, 15.0, 45.0], "阶梯必须是 5→15→45，首请求不许先睡"
    assert len(transport.calls) == 4


def test_5xx_walks_the_same_ladder_and_then_raises() -> None:
    sleeps: list[float] = []
    transport = ScriptedTransport(*[(503, b"unavailable")] * 4)
    client = _client(transport, sleeps=sleeps)
    with pytest.raises(ngw.NgwHttpError, match="阶梯走完仍失败"):
        client.search("600519")
    assert sleeps == [5.0, 15.0, 45.0]


def test_a_non_retryable_status_fails_on_the_first_shot() -> None:
    sleeps: list[float] = []
    transport = ScriptedTransport((404, b"not found"))
    with pytest.raises(ngw.NgwHttpError) as caught:
        _client(transport, sleeps=sleeps).search("x")
    assert caught.value.status == 404
    assert sleeps == [], "404 不是限流，睡完再撞三次只会把小故障拖成大故障"
    assert len(transport.calls) == 1


def test_network_errors_are_retried_like_a_5xx() -> None:
    sleeps: list[float] = []

    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(
            self, _url: str, _headers: Mapping[str, str], _timeout: float
        ) -> tuple[int, bytes]:
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("timed out")
            return 200, _json({"stocks": []})

    flaky = Flaky()
    client = ngw.NgwClient(
        token_path=Path("/nonexistent/ngw.token"),
        interval=0.0,
        sleep=sleeps.append,
        clock=lambda: 100.0,
        transport=flaky,
    )
    client.search("600519")
    assert flaky.calls == 2
    assert sleeps == [5.0]


def test_broken_json_is_an_error_not_a_retry() -> None:
    transport = ScriptedTransport((200, b"<html>"))
    with pytest.raises(ngw.NgwError, match="不是 JSON"):
        _client(transport).search("x")
    assert len(transport.calls) == 1


# --- pacer / 串行 -------------------------------------------------------------


def test_the_pacer_pays_the_interval_between_two_requests() -> None:
    sleeps: list[float] = []
    transport = ScriptedTransport((200, _json({"stocks": []})), (200, _json({"stocks": []})))
    client = _client(transport, sleeps=sleeps, interval=0.3)
    client.search("600519")
    client.search("600519")
    # 冻结时钟下差额恒为整段 interval：第一次不睡，第二次补 0.3s。
    assert sleeps == [0.3], sleeps


def test_concurrent_callers_are_serialized_by_the_lock() -> None:
    """禁并发是结构性的：两个线程同时进，也只能一个一个过。"""
    overlaps: list[int] = []
    active = 0
    guard = threading.Lock()

    def transport(_url: str, _headers: Mapping[str, str], _timeout: float) -> tuple[int, bytes]:
        nonlocal active
        with guard:
            active += 1
            overlaps.append(active)
        threading.Event().wait(0.02)
        with guard:
            active -= 1
        return 200, _json({"stocks": []})

    client = ngw.NgwClient(
        token_path=Path("/nonexistent/ngw.token"),
        interval=0.0,
        transport=transport,
    )
    threads = [threading.Thread(target=client.search, args=("600519",)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert max(overlaps) == 1, f"同时在飞的请求超过了 1：{overlaps}"


# --- kline：count 上限与 0 行显式分支 -----------------------------------------


def test_count_above_the_measured_ceiling_never_leaves_the_process() -> None:
    """硬约束 3：count≥1500 源侧静默 0 行——发出去就会把被拒的参数误读成「到底了」。"""
    transport = ScriptedTransport()  # 脚本为空：谁发请求谁先炸
    client = _client(transport)
    with pytest.raises(ValueError, match="静默返回"):
        client.kline("3143", count=1500)
    with pytest.raises(ValueError, match="越界"):
        client.kline("3143", count=0)
    assert transport.calls == []


def test_an_empty_page_is_exhausted_not_an_error() -> None:
    """0 行是显式分支：count 合法时它只有一个含义——没有更早的了。"""
    transport = ScriptedTransport((200, _json({"timedata": []})))
    page = _client(transport).kline("3143", count=1400)
    assert page.rows == ()
    assert page.exhausted is True


def test_a_page_with_rows_is_not_exhausted() -> None:
    row: dict[str, Any] = {"times": "20260924093100", "nowv": "123700"}
    transport = ScriptedTransport((200, _json({"timedata": [row]})))
    page = _client(transport).kline("3143", count=1400)
    assert len(page.rows) == 1
    assert page.exhausted is False


def test_ex_is_omitted_unless_asked_and_start_passes_through() -> None:
    transport = ScriptedTransport((200, _json({"timedata": []})), (200, _json({"timedata": []})))
    client = _client(transport)
    client.kline("3143", count=10)
    assert "ex=" not in transport.calls[0]  # 不复权口径：ex 不传（ADR-0009 决定 4）
    assert "start=" not in transport.calls[0]
    client.kline("3143", count=10, start="20260924150000", ex="1")
    assert "start=20260924150000" in transport.calls[1]
    assert "ex=1" in transport.calls[1]


def test_a_descending_page_is_delivered_ascending() -> None:
    """源契约是倒序回页（实测首行最新）：客户端归一成升序再交下游——R009 与落盘合并按升序。"""
    newer = {"times": "20260924150000", "nowv": "123700"}
    older = {"times": "20260924093100", "nowv": "125500"}
    transport = ScriptedTransport((200, _json({"timedata": [newer, older]})))
    page = _client(transport).kline("3143", count=10)
    assert [row["times"] for row in page.rows] == ["20260924093100", "20260924150000"]


def test_timedata_of_the_wrong_shape_is_a_protocol_error() -> None:
    transport = ScriptedTransport((200, _json({"timedata": {"not": "a list"}})))
    with pytest.raises(ngw.NgwError, match="timedata 不是列表"):
        _client(transport).kline("3143", count=10)


# --- fundamentals 两个方法的参数形状 ------------------------------------------


def test_stockshare_sends_innercode_and_finacereport_sends_six_digit_code() -> None:
    transport = ScriptedTransport((200, _json({})), (200, _json({})))
    client = _client(transport)
    client.stockshare("3143")
    client.finacereport("600519", reporttype="4")
    assert "code=3143" in transport.calls[0] and "tradingCode" not in transport.calls[0]
    assert "tradingCode=600519" in transport.calls[1] and "reporttype=4" in transport.calls[1]
