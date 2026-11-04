"""`site/api.py` 的 `/api/quote`：Muse 用行情快照（查看用，不进干净区）。

口径住 `prompt.build_quote`：`site/` 下新加一个纯模块要先改 ADR-0013 的清单
（`test_site_boundaries` 钉着它），而面板口径 `quote_of` 本来就在组合根里，
机器快照跟它放一处才是“口径只有一处”，不是为快照单开一个模块。

判的全是**口径与回落**，不是价格本身：分钟价是哪一根、昨收取哪一根、
分钟没今日数据时回落到哪、两边都没数时是不是空着不编——这四件事错了，
Muse 拿到的就是一个看起来很真、其实口径错了的价。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes import bar, minute_bar, snapshot_root
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Listing
from zhixing_quant.site import api, prompt
from zhixing_quant.site.templates import Config, Selection, Template
from zhixing_quant.storage import layout
from zhixing_quant.storage.write import store_bars

TODAY = date.today()
YESTERDAY = TODAY - timedelta(days=1)
DAY_BEFORE = TODAY - timedelta(days=2)
CALENDAR = TradingCalendar([DAY_BEFORE, YESTERDAY, TODAY])
CFG = Config(
    token_warn_above=100_000,
    templates=(
        Template(
            name="短期投资",
            role="你是资深 A 股分析师",
            task="判断未来 1–4 周的机会与风险",
            data=(Selection(dataset=layout.DAILY, days=120),),
            output="先给结论，再给依据。",
            format="markdown",
            fields=("open", "high", "low", "close", "volume", "amount"),
            adjust="backward",
            status="ready",
            waiting_on="",
        ),
    ),
)
BOOK = (Listing("600519", "贵州茅台", date(2001, 8, 27)),)


def _daily() -> list[dict[str, object]]:
    return [
        {"date": DAY_BEFORE.isoformat(), "open": 11.0, "high": 11.0, "low": 11.0,
         "close": 11.0, "volume": 1000.0, "amount": 11000.0},
        {"date": YESTERDAY.isoformat(), "open": 12.0, "high": 12.0, "low": 12.0,
         "close": 12.0, "volume": 1000.0, "amount": 12000.0},
    ]


def _minute(close: float = 13.0, when: datetime | None = None) -> dict[str, object]:
    moment = when or datetime(TODAY.year, TODAY.month, TODAY.day, 10, 0)
    return {
        "date": moment.date().isoformat(), "ts": moment.isoformat(),
        "open": close, "high": close, "low": close, "close": close,
        "volume": 100.0, "amount": close * 100.0,
    }


def _build(
    daily: list[dict[str, object]] | None = None,
    minute: list[dict[str, object]] | None = None,
    *,
    minute_failed: bool = False,
) -> dict[str, object]:
    return prompt.build_quote(
        code="600519",
        name="贵州茅台",
        daily_bars=_daily() if daily is None else daily,
        minute_bars=[] if minute is None else minute,
        as_of=TODAY,
        fetched_at=datetime.now(UTC),
        minute_failed=minute_failed,
    )


def test_minute_price_uses_the_day_before_as_prev_close() -> None:
    body = _build(minute=[_minute(13.0)])
    assert body["mode"] == "minute"
    assert body["source"] == "minute_5_live"
    assert body["price"] == 13.0
    assert body["prev_close"] == 12.0
    assert body["change_pct"] == pytest.approx(8.3333, abs=1e-4)
    assert body["bar_date"] == TODAY.isoformat()
    assert body["bar_time"] is not None and "10:00" in str(body["bar_time"])


def test_daily_published_today_is_not_its_own_prev_close() -> None:
    daily = [
        *_daily(),
        {"date": TODAY.isoformat(), "open": 13.0, "high": 13.0, "low": 13.0,
         "close": 13.0, "volume": 1000.0, "amount": 13000.0},
    ]
    body = _build(daily=daily, minute=[_minute(13.5)])
    assert body["prev_close"] == 12.0
    assert body["change_pct"] == pytest.approx(12.5, abs=1e-4)


def test_stale_minute_bars_are_ignored_not_presented_as_today() -> None:
    old = _minute(99.0, when=datetime(YESTERDAY.year, YESTERDAY.month, YESTERDAY.day, 15, 0))
    body = _build(minute=[old])
    assert body["mode"] == "daily"
    assert body["price"] == 12.0
    assert body["prev_close"] == 11.0


def test_stale_daily_prev_close_is_flagged_not_presented_as_yesterday() -> None:
    # 生产实测：服务器干净区日线停在 2026-10-21，而今天是 2026-11-04——
    # 昨收字段若不带日期、不带陈旧说明，-18% 这种涨跌幅会被误读成当日涨跌幅。
    old_daily = [
        {"date": "2026-10-20", "open": 650.0, "high": 650.0, "low": 650.0,
         "close": 650.0, "volume": 1000.0, "amount": 650000.0},
        {"date": "2026-10-21", "open": 646.0, "high": 646.0, "low": 646.0,
         "close": 646.77, "volume": 1000.0, "amount": 646770.0},
    ]
    body = _build(daily=old_daily, minute=[_minute(530.16)])
    assert body["prev_close"] == 646.77
    assert body["prev_close_date"] == "2026-10-21"
    assert "2026-10-21" in str(body["note"])
    assert "不代表昨日收盘" in str(body["note"])


def test_no_minute_falls_back_to_daily_close_not_a_made_up_price() -> None:
    body = _build()
    assert body["mode"] == "daily"
    assert body["price"] == 12.0
    assert body["change_pct"] == pytest.approx(9.0909, abs=1e-4)
    assert "非实时" in str(body["note"])


def test_nothing_anywhere_is_unavailable_with_null_price() -> None:
    body = _build(daily=[], minute=[])
    assert body["mode"] == "unavailable"
    assert body["price"] is None
    assert body["change_pct"] is None
    assert body["source"] is None


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    base = snapshot_root(tmp_path, monkeypatch)
    store_bars(
        [bar(DAY_BEFORE, 11.0), bar(YESTERDAY, 12.0)],
        dataset=layout.DAILY,
        root=base / "data",
    )
    return TestClient(
        api.create_app(
            listings=BOOK,
            cfg=CFG,
            calendar=CALENDAR,
            recent=api.Recent(tmp_path / "site" / "recent.json"),
            minute_source=lambda *_a: [
                minute_bar(datetime(TODAY.year, TODAY.month, TODAY.day, 10, 0), 13.0)
            ],
        )
    )


def test_route_serves_minute_quote(client: TestClient) -> None:
    body = client.get("/api/quote", params={"code": "600519"}).json()
    assert body["mode"] == "minute" and body["price"] == 13.0
    assert body["prev_close"] == 12.0 and body["name"] == "贵州茅台"


def test_route_calls_minute_source_with_minute_5_today_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = snapshot_root(tmp_path, monkeypatch)
    store_bars(
        [bar(DAY_BEFORE, 11.0), bar(YESTERDAY, 12.0)],
        dataset=layout.DAILY,
        root=base / "data",
    )
    calls: list[tuple[str, str, date, date]] = []

    def recording(code: str, dataset: str, start: date, end: date) -> Any:
        calls.append((code, dataset, start, end))
        return [minute_bar(datetime(TODAY.year, TODAY.month, TODAY.day, 10, 0), 13.0)]

    app = api.create_app(
        listings=BOOK,
        cfg=CFG,
        calendar=CALENDAR,
        recent=api.Recent(tmp_path / "site" / "recent.json"),
        minute_source=recording,
    )
    body = TestClient(app).get("/api/quote", params={"code": "600519"}).json()
    assert body["mode"] == "minute"
    assert calls == [("600519", "minute_5", TODAY, TODAY)]


def test_route_minute_failure_falls_back_to_daily_instead_of_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = snapshot_root(tmp_path, monkeypatch)
    store_bars(
        [bar(DAY_BEFORE, 11.0), bar(YESTERDAY, 12.0)],
        dataset=layout.DAILY,
        root=base / "data",
    )

    def boom(*_a: object) -> Any:
        raise ConnectionError("sina 不理人")

    app = api.create_app(
        listings=BOOK,
        cfg=CFG,
        calendar=CALENDAR,
        recent=api.Recent(tmp_path / "site" / "recent.json"),
        minute_source=boom,
    )
    response = TestClient(app).get("/api/quote", params={"code": "600519"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "daily" and body["price"] == 12.0
    assert "分钟活取失败" in str(body["note"])


def test_route_unknown_code_is_404_like_kline(client: TestClient) -> None:
    assert client.get("/api/quote", params={"code": "999999"}).status_code == 404
