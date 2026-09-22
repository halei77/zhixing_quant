"""转接源参考表栈的对抗性测试（client / tables / storage.tables / relay_cli）。

怀疑点清单：
1. 退避阶梯与切源的顺序：首选源走完阶梯才换备选；Retry-After 比阶梯大时取大者
2. 解析器对"字段顺序漂移"与"缺字段"的反应（zip(fields,row) 是唯一正确姿势）
3. 参考表存储的幂等语义与 store_bars 逐条相同（重跑 rewritten=0、异值修复、批内冲突抛）
4. 锚点对账：超阈整批拒一行不写；锚不上剔除计数，两者不许混成一个数
5. fields 各页漂移要响（分页中途源改列序 = 数据不可信）
6. 值域校验：up ≤ down 的行不产出
"""

from __future__ import annotations

import argparse
from datetime import date
from typing import Any

import pytest

from zhixing_quant.sources.jobs import relay_cli
from zhixing_quant.sources.relay import client as relay_client
from zhixing_quant.sources.relay.tables import parse_daily_basic, parse_stk_limit
from zhixing_quant.storage import tables
from zhixing_quant.storage.query import read_bars

DAY = date(2026, 9, 18)
FIELDS = ["ts_code", "trade_date", "up_limit", "down_limit"]


def _row(symbol: str, up: str, down: str, day: str = "20260918") -> list[str]:
    return [symbol, day, up, down]


def _body(items: list[list[str]]) -> dict[str, Any]:
    return {"code": 0, "data": {"fields": FIELDS, "items": items}}


# ── client ───────────────────────────────────────────────────────────────────


def test_fetch_takes_first_relay_and_records_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relay_client, "_get", lambda *_a: {"code": 0})
    source, body = relay_client.fetch("stk_limit", {}, sleep=lambda _s: None, backoff=(60,))
    assert source == "rds" and body["code"] == 0


def test_fetch_backs_off_then_succeeds_without_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """429 带 Retry-After 时取其与阶梯值的较大者，且 sleep 是注入的——测试不等真时间。"""
    calls: list[float] = []
    attempts: list[int] = []

    def fake_get(relay: str, _api: str, _params: Any, _timeout: float) -> dict[str, Any]:
        attempts.append(attempt_counter[0])
        attempt_counter[0] += 1
        if len(attempts) < 3:
            raise relay_client.RelayHttpError(relay, 429, "120", b"slow down")
        return {"code": 0}

    attempt_counter = [0]
    monkeypatch.setattr(relay_client, "_get", fake_get)
    source, _ = relay_client.fetch("stk_limit", {}, sleep=calls.append, backoff=(60, 300, 1800))
    assert source == "rds"
    assert calls == [120.0, 300.0], "第一次 429 按 Retry-After 120s，第二次按阶梯 300s"


def test_fetch_fails_over_to_second_relay_after_exhausting_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首选源走完整个阶梯才换备选——半途就切等于把限流放大成事故。"""
    seen_relays: list[str] = []

    def fake_get(relay: str, _api: str, _params: Any, _timeout: float) -> dict[str, Any]:
        seen_relays.append(relay)
        if relay == "rds":
            raise relay_client.RelayHttpError(relay, 503, None, b"down")
        return {"code": 0}

    monkeypatch.setattr(relay_client, "_get", fake_get)
    source, _ = relay_client.fetch("stk_limit", {}, sleep=lambda _s: None, backoff=(1, 2))
    assert source == "promax"
    assert set(seen_relays) == {"rds", "promax"}
    assert seen_relays.count("rds") == 3, "阶梯 1→2 共 3 次尝试都在首选源上"


def test_fetch_reports_unavailable_when_every_relay_dies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        relay_client,
        "_get",
        lambda relay, _api, _params, _timeout: (_ for _ in ()).throw(
            relay_client.RelayHttpError(relay, 503, None, b"down")
        ),
    )
    with pytest.raises(relay_client.RelayUnavailable, match="全部走完"):
        relay_client.fetch("stk_limit", {}, sleep=lambda _s: None, backoff=(1,))


# ── 解析与值域 ───────────────────────────────────────────────────────────────


def test_parse_normalizes_code_suffixes_and_dates() -> None:
    rows = parse_stk_limit("rds", FIELDS, [_row("600519.SH", "11.03", "9.03")])
    assert rows[0].symbol == "600519"
    assert rows[0].trade_date == DAY
    assert rows[0].up_limit == 11.03


def test_parse_survives_field_order_drift() -> None:
    """字段顺序不固定是这族源的已知陷阱：乱序响应必须解析出同样的行。"""
    shuffled = ["down_limit", "trade_date", "ts_code", "up_limit"]
    rows = parse_stk_limit("rds", shuffled, [["9.03", "20260918", "600519.SH", "11.03"]])
    assert rows[0].up_limit == 11.03 and rows[0].down_limit == 9.03


def test_parse_rejects_missing_column_and_bad_values() -> None:
    with pytest.raises(ValueError, match="缺字段"):
        parse_stk_limit("rds", ["ts_code", "trade_date"], [_row("600519.SH", "11", "9")])
    with pytest.raises(ValueError, match="涨跌停价不成立"):
        parse_stk_limit("rds", FIELDS, [_row("600519.SH", "9.03", "9.03")])
    with pytest.raises(ValueError, match="YYYYMMDD"):
        parse_stk_limit("rds", FIELDS, [_row("600519.SH", "11.03", "9.03", "2026-09-18")])
    with pytest.raises(ValueError, match="两次"):
        parse_stk_limit("rds", FIELDS, [_row("600519.SH", "11", "9"), _row("600519.SH", "11", "9")])


# ── storage.tables ───────────────────────────────────────────────────────────


def _limit_row(symbol: str, up: float, day: date, source: str = "rds") -> Any:
    from zhixing_quant.sources.relay.tables import StkLimitRow

    return StkLimitRow(source, symbol, day, up, up - 2.0)


def test_write_is_idempotent_and_repairs_changed_values(tmp_path: Any) -> None:
    rows = [_limit_row("600519", 11.03, DAY)]
    first = tables.write_table(rows, table="stk_limit", root=tmp_path)
    assert (first.added, first.rewritten) == (1, 1)
    again = tables.write_table(rows, table="stk_limit", root=tmp_path)
    assert again.rewritten == 0, "同键同值重跑不重写——幂等的直接证据"
    changed = tables.write_table(
        [_limit_row("600519", 11.05, DAY)], table="stk_limit", root=tmp_path
    )
    assert (changed.added, changed.repaired) == (0, 1)
    back = tables.read_table("stk_limit", "600519", DAY, DAY, root=tmp_path)
    assert back[0].up_limit == 11.05 and back[0].source == "rds"


def test_write_refuses_conflicting_rows_and_wrong_type(tmp_path: Any) -> None:
    from zhixing_quant.storage.write import BarConflict

    with pytest.raises(BarConflict, match="两条不同的行"):
        tables.write_table(
            [_limit_row("600519", 11.03, DAY), _limit_row("600519", 11.05, DAY, source="promax")],
            table="stk_limit",
            root=tmp_path,
        )
    with pytest.raises(ValueError, match="行形状与列定义不符"):
        tables.write_table([object()], table="stk_limit", root=tmp_path)


def test_unknown_table_and_bar_reader_are_both_refused(tmp_path: Any) -> None:
    with pytest.raises(ValueError, match="未知参考表"):
        tables.table_spec("nope")
    tables.write_table([], table="stk_limit", root=tmp_path)  # 空批不抛：合法表的正常形态
    with pytest.raises(ValueError, match="未知 dataset"):
        # read_bars 对参考表：形状表里没有这名字要炸在入口，不是返回空（ADR-0015 后果二）
        read_bars("600519", DAY, DAY, dataset="stk_limit", root=tmp_path)


# ── daily_basic ──────────────────────────────────────────────────────────────

BASIC_FIELDS = [
    "ts_code",
    "trade_date",
    "close",
    "turnover_rate",
    "turnover_rate_f",
    "volume_ratio",
    "pe",
    "pe_ttm",
    "pb",
    "ps",
    "ps_ttm",
    "dv_ratio",
    "dv_ttm",
    "total_share",
    "float_share",
    "free_share",
    "total_mv",
    "circ_mv",
]


def test_daily_basic_null_stays_null_never_zero() -> None:
    """陷阱清单的 null 纪律：源给 'None' 的字段保持 None——0 填充会把"没这数"洗成"这数为 0"。"""
    raw = [
        "600519.SH",
        "20260918",
        "1257.12",
        "0.2",
        "None",
        "",
        "19.0",
        "None",
        "6.2",
        "9.2",
        "9.2",
        "4.1",
        "4.1",
        "125008.16",
        "None",
        "56879.87",
        "156735231.0",
        "156735231.0",
    ]
    rows = parse_daily_basic("rds", BASIC_FIELDS, [raw])
    row = rows[0]
    assert row.turnover_rate == 0.2
    assert row.turnover_rate_f is None and row.volume_ratio is None and row.pe_ttm is None
    assert row.float_share is None
    assert row.close == 1257.12


def test_daily_basic_close_anchor_rejects_mismatch(tmp_path: Any) -> None:
    """表内 close 与干净区收盘差 5 分钱：超过一分钱容差 → 整批拒。两边都到分，差一分就是不同真。"""
    raw = ["600519.SH", "20260918", "1257.17"] + ["None"] * 15

    def fetch(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return "rds", {"code": 0, "data": {"fields": BASIC_FIELDS, "items": [raw]}}

    report, code, ledger = relay_cli._pull(
        _args(table="daily_basic"),
        fetch=fetch,
        prev_ref_of=lambda _symbol, _day: None,
        same_ref_of=lambda _symbol, _day: 1257.12,
        root=tmp_path,
    )
    assert code == 1 and ledger is None
    assert "整批拒" in report and "1257.17" in report


def test_daily_basic_close_anchor_passes_and_writes(tmp_path: Any) -> None:
    raw = ["600519.SH", "20260918", "1257.12", "0.2"] + ["None"] * 14

    def fetch(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return "rds", {"code": 0, "data": {"fields": BASIC_FIELDS, "items": [raw]}}

    _report, code, ledger = relay_cli._pull(
        _args(table="daily_basic"),
        fetch=fetch,
        prev_ref_of=lambda _symbol, _day: None,
        same_ref_of=lambda _symbol, _day: 1257.12,
        root=tmp_path,
    )
    assert code == 0 and ledger is not None and ledger.added == 1
    back = tables.read_table("daily_basic", "600519", DAY, DAY, root=tmp_path)
    assert back[0].close == 1257.12 and back[0].pe is None


def test_a_table_without_a_registered_anchor_is_refused(tmp_path: Any) -> None:
    """ADR-0015 决定 3：没配锚的表不许拉——fail-closed 不给"先拉了再补锚"留门。"""
    with pytest.raises(ValueError, match="没有登记解析器或锚点"):
        relay_cli._pull(
            _args(table="forecast"),
            fetch=lambda _a, _p: ("rds", {"code": 0, "data": {"fields": [], "items": []}}),
            prev_ref_of=lambda _s, _d: None,
            same_ref_of=lambda _s, _d: None,
            root=tmp_path,
        )


# ── relay_cli ────────────────────────────────────────────────────────────────


def _args(
    day: str = "2026-09-18", symbols: str | None = None, table: str = "stk_limit"
) -> argparse.Namespace:
    return argparse.Namespace(
        command="pull",
        table=table,
        day=day,
        day_parsed=date.fromisoformat(day),
        symbols=symbols,
        page_size=5000,
        out=None,
    )


def test_pull_writes_anchored_rows_and_counts_unanchored(tmp_path: Any) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fetch(api: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        calls.append((api, params))
        return "rds", _body([_row("600519.SH", "11.03", "9.03"), _row("880001.BJ", "5.0", "4.5")])

    def prev(symbol: str, _day: date) -> float | None:
        return 10.03 if symbol == "600519" else None

    def limit(_symbol: str) -> float:
        return 10.0

    report, code, ledger = relay_cli._pull(
        _args(), fetch=fetch, prev_ref_of=prev, same_ref_of=prev, limit_of=limit, root=tmp_path
    )
    assert code == 0 and ledger is not None and ledger.added == 1, "锚不上的剔除，只落有锚的"
    assert "1 行锚不上被剔除" in report
    assert calls[0][1]["trade_date"] == "20260918" and calls[0][1]["offset"] == 0


def test_pull_rejects_the_whole_page_when_anchor_breaks(tmp_path: Any) -> None:
    """涨停价对昨收 12%：主板档位 10%+1pp 装不下 → 整批拒，一行不写。"""

    def fetch(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return "rds", _body([_row("600519.SH", "11.24", "9.03")])

    report, code, ledger = relay_cli._pull(
        _args(),
        fetch=fetch,
        prev_ref_of=lambda _symbol, _day: 10.03,
        same_ref_of=lambda _symbol, _day: 10.03,
        limit_of=lambda _symbol: 10.0,
        root=tmp_path,
    )
    assert code == 1 and ledger is None
    assert "整批拒" in report and "11.24" in report


def test_pagination_hard_fails_on_silent_truncation() -> None:
    """rds 实测（2026-09-22）：offset≥5000 一律空页但 has_more=True、count=5644 照给。
    "下一页空 + has_more" 必须响，不许当拉完——那是无声丢 11% 的行。"""
    pages: list[Any] = [
        "rds",
        {
            "code": 0,
            "data": {
                "fields": FIELDS,
                "items": [_row("600519.SH", "11.03", "9.03")] * 5000,
                "has_more": True,
                "count": 5644,
            },
        },
        "rds",
        {"code": 0, "data": {"fields": FIELDS, "items": [], "has_more": True, "count": 5644}},
    ]

    def fetch(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if not pages:
            raise AssertionError("不应再翻页")
        src = pages.pop(0)
        body = pages.pop(0)
        return str(src), body

    with pytest.raises(ValueError, match="截断"):
        relay_cli.fetch_all_pages("stk_limit", DAY, fetch=fetch, page_size=5000)


def test_pull_filters_by_symbols_and_handles_empty_day(tmp_path: Any) -> None:
    def fetch(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return "rds", _body([])

    report, code, ledger = relay_cli._pull(
        _args(symbols="600519"),
        fetch=fetch,
        prev_ref_of=lambda _symbol, _day: 10.0,
        same_ref_of=lambda _symbol, _day: 10.0,
        limit_of=lambda _symbol: 10.0,
        root=tmp_path,
    )
    assert code == 0 and ledger is None and "没有行" in report


def test_main_maps_network_failure_to_exit_two(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_kw: Any) -> tuple[str, dict[str, Any]]:
        raise relay_client.RelayUnavailable("全灭")

    import zhixing_quant.sources.relay.client as client_mod

    monkeypatch.setattr(client_mod, "fetch", boom)
    code = relay_cli.main(["pull", "--table", "stk_limit", "--day", "2026-09-18"])
    assert code == 2, "双源全灭是没开始，不是数据坏"
