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
from datetime import date, timedelta
from typing import Any

import pytest

from zhixing_quant.sources.jobs import relay_cli
from zhixing_quant.sources.relay import client as relay_client
from zhixing_quant.sources.relay.tables import parse_daily_basic, parse_forecast, parse_stk_limit
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
            _args(table="stk_holder"),
            fetch=lambda _a, _p: ("rds", {"code": 0, "data": {"fields": [], "items": []}}),
            prev_ref_of=lambda _s, _d: None,
            same_ref_of=lambda _s, _d: None,
            root=tmp_path,
        )


# ── forecast ─────────────────────────────────────────────────────────────────

FORECAST_FIELDS = [
    "ts_code",
    "ann_date",
    "end_date",
    "type",
    "p_change_min",
    "p_change_max",
    "net_profit_min",
    "net_profit_max",
    "last_parent_net",
    "summary",
    "update_flag",
]


def test_forecast_parses_and_rejects_bad_type_and_interval() -> None:
    raw = [
        "600519.SH",
        "20260113",
        "20251231",
        "预增",
        "14.67",
        "14.67",
        "8570000",
        "8570000",
        "None",
        "略增",
    ]
    rows = parse_forecast("rds", FORECAST_FIELDS, [raw])
    assert rows[0].type == "预增" and rows[0].ann_date == date(2026, 1, 13)
    with pytest.raises(ValueError, match="不在已知枚举"):
        parse_forecast(
            "rds", FORECAST_FIELDS, [["600519.SH", "20260113", "20251231", "暴涨", *raw[4:]]]
        )
    with pytest.raises(ValueError, match="预告区间颠倒"):
        parse_forecast(
            "rds",
            FORECAST_FIELDS,
            [["600519.SH", "20260113", "20251231", "预增", "20", "10", *raw[6:]]],
        )


def test_forecast_anchor_requires_quarter_end_and_past_ann_date() -> None:
    from zhixing_quant.sources.relay.tables import ForecastRow

    good = ForecastRow(
        "rds", "600519", date(2026, 1, 13), date(2025, 12, 31), "预增", 1.0, 2.0, 3.0, 4.0, "s"
    )
    bad_period = ForecastRow(
        "rds", "600519", date(2026, 1, 13), date(2025, 12, 30), "预增", 1.0, 2.0, 3.0, 4.0, "s"
    )
    future = ForecastRow(
        "rds", "600519", date(2099, 1, 13), date(2025, 12, 31), "预增", 1.0, 2.0, 3.0, 4.0, "s"
    )
    # 直接走锚函数（不经 _pull 的网络路径）
    anchor = relay_cli.ANCHORS["forecast"]
    problems, _unanchored, kept = anchor([good], None, None, None)
    assert not problems and len(kept) == 1
    problems, _, _ = anchor([bad_period, future], None, None, None)
    assert len(problems) == 2


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


# ── report_rc：第二种分页形状（页 < limit 即到底）与它的发前拒 ─────────────────

RC_FIELDS = ["ts_code", "report_date", "org_name", "quarter", "eps", "pe"]
#: 分页守卫的满页单元：这族的顶就是 5000（2026-09-25 实测 limit=5001 静默回 5000）。
RC_CAP = 5000


def _rc_item(
    day: str = "20260115",
    org: str = "中泰证券",
    quarter: str = "2026Q4",
    eps: str = "70.97",
    pe: str = "18.11",
) -> list[str]:
    return ["600519.SH", day, org, quarter, eps, pe]


def _rc_body(items: list[list[str]], fields: list[str] | None = None) -> dict[str, Any]:
    return {"code": 0, "data": {"fields": fields or RC_FIELDS, "items": items}}


def test_forecast_oversize_page_size_is_refused_before_any_request() -> None:
    """高基数接口 max_limit=1000（2026-09-25 实测 400）：超上限的 page_size 发前就拒。

    transport 零调用与 ngw 的 count≥1500 同款——超上限的请求必 400，重发一万次也是 400。
    """
    from zhixing_quant.sources.relay.pages import fetch_pages

    calls: list[object] = []

    def fetch(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        calls.append(_params)
        raise AssertionError("不该发请求")

    with pytest.raises(ValueError, match="max_limit=1000"):
        fetch_pages("forecast", {"ts_code": "600519.SH"}, fetch=fetch, page_size=5000)
    assert calls == [], "超上限参数必须在发请求前拒——fail-closed 不是 fail-slow"


def test_report_rc_page_above_the_silent_5000_cap_is_refused_before_any_request() -> None:
    """`limit=5001` 静默顶成 5000 行（2026-09-25 实测）：超顶参数发前拒，硬响。

    不拒的后果是"页 < limit"恒真——5000 < 5001，截断被读成拉完，丢的行不会出现在任何报告里。
    """
    from zhixing_quant.sources.relay.pages import fetch_pages_short

    calls: list[object] = []

    def fetch(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        calls.append(_params)
        raise AssertionError("不该发请求")

    with pytest.raises(ValueError, match="limit=5001 实测被静默顶成 5000"):
        fetch_pages_short("report_rc", {"ts_code": "600519.SH"}, fetch=fetch, page_size=RC_CAP + 1)
    assert calls == []


def test_report_rc_page_below_the_cap_is_refused_too() -> None:
    """比顶小的页同样拒：翻到第 5000 行会撞源顶（跨顶 offset 实测回空页），把截断读成拉完。"""
    from zhixing_quant.sources.relay.pages import fetch_pages_short

    with pytest.raises(ValueError, match="只认 page_size=5000"):
        fetch_pages_short("report_rc", {}, fetch=lambda _a, _p: ("rds", {}), page_size=1000)


def test_report_rc_short_page_means_bottom() -> None:
    """页 < limit 即到底：这一族 has_more 恒 False（count 只是 limit 回显），短页是唯一信号。"""
    from zhixing_quant.sources.relay.pages import fetch_pages_short

    calls: list[dict[str, Any]] = []

    def fetch(_api: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        calls.append(dict(params))
        return "rds", _rc_body([_rc_item(), _rc_item(day="20260116")])

    source, items, fields, total = fetch_pages_short(
        "report_rc", {"ts_code": "600519.SH"}, fetch=fetch, today=date(2026, 9, 25)
    )
    assert source == "rds" and len(items) == 2 and fields == RC_FIELDS
    assert total is None, "count 是 limit 回显不是总数，拿它当总数就是把回显读成拉完"
    assert len(calls) == 1 and calls[0]["offset"] == 0 and calls[0]["limit"] == RC_CAP


def test_report_rc_full_page_splits_the_date_window_until_each_half_fits() -> None:
    """满页 ≠ 到底：单查询静默只留最新 5000 行（实测全史首行 20210826、窗内却有 20190329）。
    撞顶按 start_date/end_date 对半切窗重查，两半单调不重叠，丢的更早行靠切窗捞回来。"""
    from zhixing_quant.sources.relay.pages import fetch_pages_short

    today = date(2026, 9, 25)
    calls: list[dict[str, Any]] = []

    def fetch(_api: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        calls.append(dict(params))
        if "start_date" not in params:
            return "rds", _rc_body([_rc_item()] * RC_CAP)  # 首查顶满
        start = date(int(str(params["start_date"])[:4]), 1, 1)
        if start.year <= 2000:
            return "rds", _rc_body([])  # 左半空窗（1990–2008 没有研报）
        return "rds", _rc_body([_rc_item(), _rc_item(day="20190401", org="国泰君安")])

    _source, items, _fields, total = fetch_pages_short(
        "report_rc", {"ts_code": "600519.SH"}, fetch=fetch, today=today
    )
    assert total is None
    assert len(calls) == 3, "首查满 → 左右两半各一次；左半空窗短页即底，不再下切"
    midpoint = date(1990, 1, 1) + (today - date(1990, 1, 1)) / 2
    assert calls[1]["start_date"] == "19900101"
    assert calls[1]["end_date"] == midpoint.strftime("%Y%m%d")
    assert calls[2]["start_date"] == (midpoint + timedelta(days=1)).strftime("%Y%m%d")
    assert calls[2]["end_date"] == "20260925"
    assert len(items) == 2, "满页那份 5000 行不入库本——重查的两半才是完整集，不许与它叠加出重复"


def test_report_rc_single_day_window_still_full_hard_fails() -> None:
    """切到单日仍满页：一天 5000 行研报不是数据是事故——硬响，不冒充拉完。"""
    from zhixing_quant.sources.relay.pages import fetch_pages_short

    def fetch(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return "rds", _rc_body([_rc_item()] * RC_CAP)

    with pytest.raises(ValueError, match="硬响"):
        fetch_pages_short(
            "report_rc",
            {"start_date": "20260115", "end_date": "20260115"},
            fetch=fetch,
            today=date(2026, 9, 25),
        )


def test_report_rc_fields_drift_across_split_windows_is_refused() -> None:
    """缩窗前后各页 fields 漂了 = 源改了列序/列集，半份 A 半份 B 的拼接不可信。"""
    from zhixing_quant.sources.relay.pages import fetch_pages_short

    seen = {"n": 0}

    def fetch(_api: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if "start_date" not in params:
            return "rds", _rc_body([_rc_item()] * RC_CAP)
        seen["n"] += 1
        if seen["n"] == 1:
            return "rds", _rc_body([_rc_item()])
        return "rds", _rc_body([_rc_item()], fields=["ts_code", "report_date"])

    with pytest.raises(ValueError, match="fields 变了"):
        fetch_pages_short("report_rc", {}, fetch=fetch, today=date(2026, 9, 25))


def test_report_rc_over_returned_page_is_taken_as_the_whole_harvest() -> None:
    """源忽略 limit 一次全吐（promax 形）：行比问的还多就是它能给的全部，就地返回。"""
    from zhixing_quant.sources.relay.pages import fetch_pages_short

    def fetch(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return "rds", _rc_body([_rc_item()] * (RC_CAP + 7))

    _source, items, _fields, _total = fetch_pages_short(
        "report_rc", {}, fetch=fetch, today=date(2026, 9, 25)
    )
    assert len(items) == RC_CAP + 7


# ── report_rc 解析与值域（ADR-0015 决定 3） ────────────────────────────────────


def test_report_rc_parse_keeps_valid_rows_and_rejects_dirty_values_by_row() -> None:
    """脏值拒收（任务点名）：quarter 的 null/'Q'、空 eps、空券商——**拒行不拒批**。

    整批拒会让这张表永远进不了库：真实 5000 行里就带 7 个脏 quarter、23 个空 eps
    （2026-09-25 实测 600519）。跳行既拒了脏值又收得下真数据（daily_basic close-null 同款）。
    """
    from zhixing_quant.sources.relay.tables import parse_report_rc

    items = [
        _rc_item(),
        _rc_item(quarter="None"),  # JSON null 被 str() 成 "None"
        _rc_item(quarter="Q"),  # 字面脏值 'Q'
        _rc_item(eps="None"),  # 没有分母的行
        _rc_item(org="None"),  # 没有行身份的行
    ]
    rows = parse_report_rc("rds", RC_FIELDS, items)
    assert len(rows) == 1, f"四行脏值必须全部拒行，留下的是 {rows}"
    row = rows[0]
    assert row.quarter == "2026Q4" and row.eps == 70.97 and row.pe == 18.11
    assert row.symbol == "600519" and row.report_date == date(2026, 1, 15)


def test_report_rc_parse_survives_field_order_drift_and_missing_column() -> None:
    from zhixing_quant.sources.relay.tables import parse_report_rc

    shuffled = ["pe", "quarter", "ts_code", "eps", "org_name", "report_date"]
    raw = ["18.11", "2026Q4", "600519.SH", "70.97", "中泰证券", "20260115"]
    rows = parse_report_rc("rds", shuffled, [raw])
    assert rows[0].org_name == "中泰证券" and rows[0].eps == 70.97
    # 缺列（fields 里没有 eps）：整批拒——字段完备是表级校验的第一条，缺哪列都少一维
    with pytest.raises(ValueError, match="缺字段"):
        parse_report_rc(
            "rds",
            ["ts_code", "report_date", "org_name", "quarter", "pe"],
            [["600519.SH", "20260115", "中泰证券", "2026Q4", "18.11"]],
        )


def test_report_rc_parse_rejects_duplicate_keys_and_garbage_numbers() -> None:
    from zhixing_quant.sources.relay.tables import parse_report_rc

    with pytest.raises(ValueError, match="两次"):
        parse_report_rc("rds", RC_FIELDS, [_rc_item(), _rc_item()])  # 同键同值也不许叠页
    with pytest.raises(ValueError, match="YYYYMMDD"):
        parse_report_rc("rds", RC_FIELDS, [_rc_item(day="2026-01-15")])
    with pytest.raises(ValueError, match="暴涨"):  # 不可解析的 eps 是形状级错误，整批拒
        parse_report_rc("rds", RC_FIELDS, [_rc_item(eps="暴涨")])


def test_report_rc_parse_keeps_negative_eps_for_the_record() -> None:
    """eps≤0 的亏损预测如实入库（源的真话），作不作分母由现算层判——存储不改写源的数。"""
    from zhixing_quant.sources.relay.tables import parse_report_rc

    rows = parse_report_rc("rds", RC_FIELDS, [_rc_item(eps="-1.5", pe="None")])
    assert rows[0].eps == -1.5 and rows[0].pe is None


# ── report_rc 表级与锚点 ────────────────────────────────────────────────────────


def test_report_rc_storage_roundtrip_idempotent_and_conflict_throws(tmp_path: Any) -> None:
    from zhixing_quant.sources.relay.tables import ReportRcRow
    from zhixing_quant.storage.write import BarConflict

    row = ReportRcRow("rds", "600519", date(2026, 1, 15), "中泰证券", "2026Q4", 70.97, 18.11)
    first = tables.write_table([row], table="report_rc", root=tmp_path)
    assert (first.added, first.rewritten) == (1, 1)
    again = tables.write_table([row], table="report_rc", root=tmp_path)
    assert again.rewritten == 0, "同键同值重跑不重写"
    twin = ReportRcRow("rds", "600519", date(2026, 1, 15), "中泰证券", "2026Q4", 71.0, 18.11)
    with pytest.raises(BarConflict, match="两条不同的行"):
        tables.write_table([row, twin], table="report_rc", root=tmp_path)
    back = tables.read_table(
        "report_rc", "600519", date(2026, 1, 1), date(2026, 2, 1), root=tmp_path
    )
    assert [r.quarter for r in back] == ["2026Q4"] and back[0].eps == 70.97


def test_report_rc_anchor_refuses_future_publications_and_bad_quarters() -> None:
    """行级可验（与 forecast 同族）：发布日在未来、quarter 不成形 → 整批拒，一行不写。"""
    from zhixing_quant.sources.relay.tables import ReportRcRow

    good = ReportRcRow("rds", "600519", date(2026, 1, 15), "中泰证券", "2026Q4", 70.97, None)
    future = ReportRcRow("rds", "600519", date(2099, 1, 1), "中泰证券", "2026Q4", 1.0, None)
    bad_quarter = ReportRcRow("rds", "600519", date(2026, 1, 15), "中泰证券", "Q", 70.97, None)
    anchor = relay_cli.ANCHORS["report_rc"]
    problems, unanchored, kept = anchor([good, future, bad_quarter], None, None, None)
    assert len(problems) == 2 and unanchored == 0 and kept == [good]


def test_pull_report_rc_is_a_short_page_table_end_to_end(tmp_path: Any) -> None:
    """pull 逐票路径整条接通：date_param 不带 trade_date（研报表没有按日窗口）、
    走短页守卫、过行级锚、落盘。"""

    def fetch(_api: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        assert "trade_date" not in params, "report_rc 按 ts_code 拉全史，没有按日窗口"
        assert params["limit"] == RC_CAP
        return "rds", _rc_body([_rc_item()])

    report, code, ledger = relay_cli._pull(
        _args(table="report_rc", symbols="600519"),
        fetch=fetch,
        prev_ref_of=lambda _symbol, _day: 10.0,
        same_ref_of=lambda _symbol, _day: 10.0,
        limit_of=lambda _symbol: 10.0,
        root=tmp_path,
    )
    assert code == 0 and ledger is not None and ledger.added == 1
    back = tables.read_table(
        "report_rc", "600519", date(2026, 1, 1), date(2026, 2, 1), root=tmp_path
    )
    assert back and back[0].org_name == "中泰证券"
    assert "report_rc" in report


def test_pull_page_size_defaults_to_the_table_cap(tmp_path: Any) -> None:
    """--page-size 缺省 = 按表上限：forecast 显式 5000 会被发前拒，缺省路径给 1000。"""
    from zhixing_quant.sources.relay.pages import fetch_pages

    args = _args(table="forecast", symbols="600519")
    args.page_size = None  # CLI 缺省

    seen: list[dict[str, Any]] = []

    def fetch(_api: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        seen.append(dict(params))
        return "rds", {"code": 0, "data": {"fields": [], "items": []}}

    report, code, _ledger = relay_cli._pull(
        args,
        fetch=fetch,
        prev_ref_of=lambda _s, _d: None,
        same_ref_of=lambda _s, _d: None,
        root=tmp_path,
    )
    assert code == 0 and seen and seen[0]["limit"] == 1000, "forecast 缺省必须落到 max_limit=1000"
    assert "没有行" in report
    with pytest.raises(ValueError, match="上限 1000"):
        fetch_pages("forecast", {}, fetch=fetch, page_size=5000)  # 显式超上限：发前拒
