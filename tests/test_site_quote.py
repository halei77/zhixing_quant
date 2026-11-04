"""`site/api.py` 的 `/api/quote`：Muse 用行情快照（查看用，不进干净区）。

2026-11-04 第二版口径（用户拍板：实时 + 缓存，不依赖家里日线）：
一次分钟活取（sina 主、ngw 兜底，`live.py` 自带盘中 90 秒/盘外 3600 秒
缓存）算到底——今日价、当日聚合、昨收全部取自同一批分钟根，干净区
日线不再参与。家里日线不一定常发布，2026-11-04 冒烟时它停在
10 月 21 日，拿它当昨收会把 -18% 误读成当日涨跌幅；没实时数就说没
实时数，不回落日线。

判的全是口径，不是价格本身：分钟价是哪一根、昨收取哪一根、当日
开高低量额怎么聚合、今天没根时是不是上一收盘、两边都没数时是不是
空着不编——这几件事错了，Muse 拿到的就是一个看起来很真、其实口径
错了的价。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes import minute_bar, snapshot_root
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Listing
from zhixing_quant.site import api, prompt
from zhixing_quant.site.templates import Config, Selection, Template
from zhixing_quant.storage import layout

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


def _minute(
    close: float,
    when: datetime,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 100.0,
) -> dict[str, object]:
    return {
        "date": when.date().isoformat(),
        "ts": when.isoformat(),
        "open": close if open_ is None else open_,
        "high": close if high is None else high,
        "low": close if low is None else low,
        "close": close,
        "volume": volume,
        "amount": close * volume,
    }


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)


def _build(
    minute: list[dict[str, object]] | None = None,
    *,
    minute_failed: bool = False,
) -> dict[str, object]:
    return prompt.build_quote(
        code="600519",
        name="贵州茅台",
        minute_bars=[] if minute is None else minute,
        as_of=TODAY,
        fetched_at=datetime.now(UTC),
        minute_failed=minute_failed,
    )


def test_minute_price_uses_prev_trading_day_minute_close_and_aggregates_day() -> None:
    body = _build(
        [
            _minute(480.20, _at(YESTERDAY, 15)),
            _minute(490.0, _at(TODAY, 10), open_=489.89, high=490.0, low=489.0),
            _minute(493.0, _at(TODAY, 10, 5), open_=490.0, high=493.0, low=490.0, volume=200.0),
        ]
    )
    assert body["mode"] == "minute"
    assert body["source"] == "minute_5_live"
    assert body["price"] == 493.0
    assert body["prev_close"] == 480.20
    assert body["prev_close_date"] == YESTERDAY.isoformat()
    assert body["change_pct"] == pytest.approx(2.6656, abs=1e-4)
    # 当日聚合是今日根的，不是末根自己的：开是首根开，高/低是今日极值，量额是求和。
    assert body["open"] == 489.89
    assert body["high"] == 493.0
    assert body["low"] == 489.0
    assert body["volume"] == 300.0
    assert body["amount"] == pytest.approx(490.0 * 100.0 + 493.0 * 200.0)
    assert body["bar_date"] == TODAY.isoformat()
    assert "10:05" in str(body["bar_time"])


def test_today_only_gives_null_change_not_zero() -> None:
    body = _build([_minute(13.0, _at(TODAY, 10))])
    assert body["mode"] == "minute"
    assert body["price"] == 13.0
    assert body["prev_close"] is None
    assert body["change"] is None
    assert body["change_pct"] is None


def test_no_today_bars_returns_last_close_not_unavailable() -> None:
    body = _build(
        [
            _minute(11.0, _at(DAY_BEFORE, 15)),
            _minute(12.0, _at(YESTERDAY, 15)),
        ]
    )
    assert body["mode"] == "last_close"
    assert body["price"] == 12.0
    assert body["prev_close"] == 11.0
    assert body["prev_close_date"] == DAY_BEFORE.isoformat()
    assert body["bar_date"] == YESTERDAY.isoformat()
    assert "上一交易日收盘" in str(body["note"])
    assert "非今日实时价" in str(body["note"])


def test_nothing_anywhere_is_unavailable_and_does_not_fall_back_to_daily() -> None:
    body = _build([])
    assert body["mode"] == "unavailable"
    assert body["price"] is None
    assert body["change_pct"] is None
    assert body["source"] is None
    assert "未回落干净区日线" in str(body["note"])


def test_minute_failure_is_unavailable_with_failure_note_not_500() -> None:
    body = _build([], minute_failed=True)
    assert body["mode"] == "unavailable"
    assert "分钟活取失败" in str(body["note"])


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    snapshot_root(tmp_path, monkeypatch)
    return TestClient(
        api.create_app(
            listings=BOOK,
            cfg=CFG,
            calendar=CALENDAR,
            recent=api.Recent(tmp_path / "site" / "recent.json"),
            minute_source=lambda *_a: [
                minute_bar(_at(YESTERDAY, 15), 12.0),
                minute_bar(_at(TODAY, 10), 13.0),
            ],
        )
    )


def test_route_serves_minute_quote(client: TestClient) -> None:
    body = client.get("/api/quote", params={"code": "600519"}).json()
    assert body["mode"] == "minute" and body["price"] == 13.0
    assert body["prev_close"] == 12.0 and body["name"] == "贵州茅台"


def test_route_calls_minute_source_with_minute_5_and_30_day_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_root(tmp_path, monkeypatch)
    calls: list[tuple[str, str, date, date]] = []

    def recording(code: str, dataset: str, start: date, end: date) -> Any:
        calls.append((code, dataset, start, end))
        return [
            minute_bar(_at(YESTERDAY, 15), 12.0),
            minute_bar(_at(TODAY, 10), 13.0),
        ]

    app = api.create_app(
        listings=BOOK,
        cfg=CFG,
        calendar=CALENDAR,
        recent=api.Recent(tmp_path / "site" / "recent.json"),
        minute_source=recording,
    )
    body = TestClient(app).get("/api/quote", params={"code": "600519"}).json()
    assert body["mode"] == "minute"
    assert calls == [("600519", "minute_5", TODAY - timedelta(days=30), TODAY)]


def test_route_minute_failure_is_unavailable_not_daily_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_root(tmp_path, monkeypatch)

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
    assert body["mode"] == "unavailable" and body["price"] is None
    assert "分钟活取失败" in str(body["note"])


def test_route_unknown_code_is_404_like_kline(client: TestClient) -> None:
    assert client.get("/api/quote", params={"code": "999999"}).status_code == 404
