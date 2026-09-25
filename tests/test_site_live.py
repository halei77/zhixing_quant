"""`site/live.py` 分钟K活取源（ADR-0026）的离线测试：假 sina/假 ngw 全注入，零网络。

判据形状各一条：主路径、降级（异常与空帧两种"拿不到"）、陈旧行被窗口筛空、TTL 缓存、
单飞（同键并发只发一次源请求）、令牌桶排队预算耗尽、后复权换算走盘上日线因子那同一式。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import bar, snapshot_root
from zhixing_quant.domain.bar import Bar
from zhixing_quant.site import live
from zhixing_quant.site.prompt import MinuteSource
from zhixing_quant.sources.akshare import fetch as akshare_fetch
from zhixing_quant.sources.ngw.client import KlinePage
from zhixing_quant.storage import layout
from zhixing_quant.storage.write import store_bars

DAY = date(2026, 9, 24)
TRADING_NOON = datetime(2026, 9, 25, 11, 0)
QUIET_NIGHT = datetime(2026, 9, 25, 22, 0)


def sina_rows(*stamps: str, close: str = "100.0") -> list[dict[str, Any]]:
    """sina 帧形状（实测全字符串）：`day` 带日期与时刻的分隔符（坑 #23 的判据形）。"""
    return [
        {
            "day": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}:00",
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": "1000",
            "amount": str(1000 * int(close.split(".")[0])),
        }
        for stamp in stamps
    ]


def ngw_rows(*stamps: str, fen: int = 10000) -> list[Mapping[str, object]]:
    return [
        {
            "times": stamp,
            "openp": str(fen),
            "highp": str(fen + 20),
            "lowp": str(fen - 20),
            "nowv": str(fen),
            "curvol": "1000",
            "curvalue": str(fen // 100 * 1000),
        }
        for stamp in stamps
    ]


class FakeNgw:
    """注入点替身：数着 innercode/kline 各几次，返回脚本页。"""

    def __init__(self, page: KlinePage | Exception) -> None:
        self.page = page
        self.kline_calls = 0

    def innercode(self, symbol: str) -> str:
        return f"IC{symbol}"

    def kline(self, innercode: str, **kw: object) -> KlinePage:  # noqa: ARG002 名字须与 NgwLike 协议一致
        self.kline_calls += 1
        if isinstance(self.page, Exception):
            raise self.page
        return self.page


@pytest.fixture
def disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """干净区放一天日线（因子 8.0）：活取分钟的后复权必须经它换算（ADR-0009 决定 4）。"""
    base = snapshot_root(tmp_path, monkeypatch)
    store_bars([bar(DAY, 100.0)], dataset=layout.DAILY, root=base / "data")
    return base / "data"


def make(*, now: datetime = QUIET_NIGHT) -> MinuteSource:
    return live.make_minute_source(clock=time.monotonic, now=lambda: now, sleeper=lambda _s: None)


# ── 主路径与降级 ─────────────────────────────────────────────────────────────


def test_sina_primary_bars_leave_the_provider_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(akshare_fetch, "minute_frame", lambda *_a: frames[0])
    frames.append(sina_rows("20260924103000"))
    source = make()
    bars = source("600519", "minute_60", DAY, DAY)
    assert len(bars) == 1
    assert bars[0].close == 100.0  # 原始价——复权是 prompt.py 的事（ADR-0026 决定 1 的分工）
    assert bars[0].ts is not None and bars[0].ts.hour == 10


def test_sina_exception_falls_back_to_ngw(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object) -> list[dict[str, Any]]:
        raise ConnectionError("sina 不理人")

    monkeypatch.setattr(akshare_fetch, "minute_frame", boom)
    ngw = FakeNgw(KlinePage(rows=tuple(ngw_rows("20260924150000")), exhausted=False))
    source = live.make_minute_source(
        clock=time.monotonic,
        now=lambda: QUIET_NIGHT,
        sleeper=lambda _s: None,
        ngw=ngw,
    )
    bars = source("600519", "minute_60", DAY, DAY)
    assert [b.close for b in bars] == [100.0]  # ngw 分→元 100.00，原样
    assert ngw.kline_calls == 1


def test_sina_empty_frame_also_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(akshare_fetch, "minute_frame", lambda *_a: [])
    ngw = FakeNgw(KlinePage(rows=tuple(ngw_rows("20260924150000")), exhausted=False))
    source = live.make_minute_source(
        clock=time.monotonic,
        now=lambda: QUIET_NIGHT,
        sleeper=lambda _s: None,
        ngw=ngw,
    )
    assert source("600519", "minute_5", DAY, DAY)


def test_both_sources_empty_is_a_quiet_none_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """两源都拿不到 → 空列表（build 出交代），异常不外溢。"""
    monkeypatch.setattr(akshare_fetch, "minute_frame", lambda *_a: [])
    source = live.make_minute_source(
        clock=time.monotonic,
        now=lambda: QUIET_NIGHT,
        sleeper=lambda _s: None,
        ngw=FakeNgw(KlinePage(rows=(), exhausted=True)),
    )
    assert source("600519", "minute_30", DAY, DAY) == []


def test_stale_tail_outside_window_filters_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """探针 §3 的形状：源回一整帧"真数据"但全在窗口之外（退市/北交所旧尾）→ 筛空。"""
    monkeypatch.setattr(akshare_fetch, "minute_frame", lambda *_a: sina_rows("20200106150000"))
    assert make()("000018", "minute_60", DAY, DAY) == []


# ── 缓存与并发 ───────────────────────────────────────────────────────────────


def test_ttl_cache_serves_second_call_without_second_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = [0]

    def counting(*_a: object) -> list[dict[str, Any]]:
        calls[0] += 1
        return sina_rows("20260924103000")

    monkeypatch.setattr(akshare_fetch, "minute_frame", counting)
    source = make()
    source("600519", "minute_60", DAY, DAY)
    source("600519", "minute_60", DAY, DAY)
    assert calls[0] == 1, "收盘后 TTL 1h：第二次必须吃缓存"


def test_stale_cache_is_refetched(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = [0]
    clock_now = [1000.0]

    def counting(*_a: object) -> list[dict[str, Any]]:
        calls[0] += 1
        return sina_rows("20260924103000")

    monkeypatch.setattr(akshare_fetch, "minute_frame", counting)
    source = live.make_minute_source(
        clock=lambda: clock_now[0],
        now=lambda: QUIET_NIGHT,
        sleeper=lambda _s: None,
    )
    source("600519", "minute_60", DAY, DAY)
    clock_now[0] += live.CACHE_TTL_QUIET + 1
    source("600519", "minute_60", DAY, DAY)
    assert calls[0] == 2


def test_single_flight_one_fetch_for_concurrent_same_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = [0]
    release = threading.Event()

    def slow(*_a: object) -> list[dict[str, Any]]:
        calls[0] += 1
        release.wait(2.0)
        return sina_rows("20260924103000")

    monkeypatch.setattr(akshare_fetch, "minute_frame", slow)
    source = make()
    results: list[Sequence[Bar]] = []
    first = threading.Thread(target=lambda: results.append(source("600519", "minute_60", DAY, DAY)))
    first.start()
    time.sleep(0.1)  # 让 first 成为 leader 并卡在源里
    second = threading.Thread(
        target=lambda: results.append(source("600519", "minute_60", DAY, DAY))
    )
    second.start()
    time.sleep(0.1)
    release.set()
    first.join(3)
    second.join(3)
    assert calls[0] == 1, "同键并发 = 一发源请求，另一发等同样的结果"
    assert len(results) == 2 and all(len(bars) == 1 for bars in results)


def test_bucket_budget_exhausted_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """排队预算耗尽 → 空（交代）而非挂住。低速率下令牌间隔 30s > 预算 20s，必然等不上；
    假时钟由 sleeper 推进——真实世界里没有"冻住的等待"，预算检查才有意义。"""
    monkeypatch.setattr(akshare_fetch, "minute_frame", lambda *_a: sina_rows("20260924103000"))
    clock_now = [0.0]
    source = live.make_minute_source(
        clock=lambda: clock_now[0],
        now=lambda: QUIET_NIGHT,
        sleeper=lambda seconds: clock_now.__setitem__(0, clock_now[0] + seconds),
        rate_per_min=2.0,
    )
    assert source("600519", "minute_5", DAY, DAY)  # 满桶第 1 枚
    assert source("600519", "minute_30", DAY, DAY)  # 第 2 枚（缓存键按 票×周期，桶按源请求计）
    assert source("600519", "minute_60", DAY, DAY) == []  # 余量 30s > 预算 20s：立刻放弃不挂队
    assert clock_now[0] < live.QUEUE_BUDGET, "等不上的预算不该烧掉——排队要有上限，不是耗满才走"


# ── 装配副作用 ───────────────────────────────────────────────────────────────


def test_make_sets_socket_default_timeout_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    monkeypatch.setattr(socket, "getdefaulttimeout", lambda: None)
    recorded: list[float] = []
    monkeypatch.setattr(socket, "setdefaulttimeout", recorded.append)
    live.make_minute_source(ngw=FakeNgw(KlinePage(rows=(), exhausted=True)))
    assert recorded == [live.SINA_SOCKET_TIMEOUT]


def test_make_keeps_explicit_socket_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """已有显式全局超时（别的装配设的）不覆盖——一次装配一份事实，先到先得。"""
    import socket

    monkeypatch.setattr(socket, "getdefaulttimeout", lambda: 3.0)
    recorded: list[float] = []
    monkeypatch.setattr(socket, "setdefaulttimeout", recorded.append)
    live.make_minute_source(ngw=FakeNgw(KlinePage(rows=(), exhausted=True)))
    assert recorded == []


# ── build 集成：注入的 provider 真的驱动模板分钟段 ────────────────────────────


def test_build_minute_section_consumes_the_injected_provider(
    disk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # disk 供 prompt 层复权因子读盘
    assert disk.is_dir()  # 夹具把 ZX_DATA_ROOT 钉进 env：prompt 层复权因子读的就是这块盘
    from zhixing_quant.domain.calendar import TradingCalendar
    from zhixing_quant.site.prompt import NO_DATA_NOTE, build
    from zhixing_quant.site.templates import Selection, Template

    monkeypatch.setattr(akshare_fetch, "minute_frame", lambda *_a: sina_rows("20260924103000"))
    template = Template(
        name="t",
        role="r",
        task="k",
        data=(Selection(dataset=layout.DAILY, days=5), Selection(dataset="minute_60", days=5)),
        output="o",
        format="markdown",
        fields=("open", "close", "volume", "amount"),
        adjust="backward",
        status="ready",
        waiting_on="",
    )
    calendar = TradingCalendar([date(2026, 9, 23), DAY, date(2026, 9, 25)])
    text = build(
        template,
        "600519",
        DAY,
        calendar=calendar,
        token_warn_above=100_000,
        minute_source=make(),
    ).text
    assert "| 2026-09-24 10:30 |" in text and "800.00" in text

    # 两源皆空 = 该节出 ADR-0020 交代，不 500、不假数
    monkeypatch.setattr(akshare_fetch, "minute_frame", lambda *_a: [])
    dead = live.make_minute_source(
        clock=time.monotonic,
        now=lambda: QUIET_NIGHT,
        sleeper=lambda _s: None,
        ngw=FakeNgw(KlinePage(rows=(), exhausted=True)),
    )
    text2 = build(
        template,
        "600519",
        DAY,
        calendar=calendar,
        token_warn_above=100_000,
        minute_source=dead,
    ).text
    assert NO_DATA_NOTE.splitlines()[0] in text2
