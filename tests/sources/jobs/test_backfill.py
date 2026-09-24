"""`zx-relay backfill` 的对抗性测试（任务 #53）。

怀疑点清单（对齐 docs/03 的层级，全在 L1 + 接线对偶）：

1. **续跑判据只读盘**：已成的 (symbol, date) 要在盘上判出来——已齐整票零请求；
   差一天也要拉，且同键同值一行不动（mtime 都不许变）。
2. **中断续跑对偶**：跑一半被杀 → 重跑 → 与一次跑完**逐行相同**，且重跑不重拉已成的票
   （对偶判据不靠 /tmp 运行件，#53 的直接起因）。
3. **has_more 截断硬响**：rds 式"页满 + has_more + 下一页空"必须响，不许默默拿半份
   （ADR-0015），且响完不重试同一个确定性失败。
4. **钉源与熔断**：--relay 原样传进 client.fetch；连败到 breaker 收工（退出码 2）、
   中途成功清零连败；RelayUnavailable 可按 attempts 重试。
5. **fail-closed**：多余命令行参数/非法 --relay 取值 = 退出码 2 且一个请求都不发
   （独立审计 M1 / 任务 #30，与 zx-site 同款钉法）。

假转接源是本地按 ts_code 吐行的 Recorder——转接的响应形状（fields+items）与 akshare 的
帧形状不同，tests/fakes.py 的 FakeFrame 管不到这一族；日线侧的道具（bar/snapshot_root）
照旧用 fakes 的。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import bar, snapshot_root
from zhixing_quant.sources.jobs import backfill, relay_cli
from zhixing_quant.sources.relay.client import RelayUnavailable
from zhixing_quant.sources.relay.tables import parse_daily_basic
from zhixing_quant.storage import tables
from zhixing_quant.storage.write import store_bars

#: 与 sources/relay/tables.parse_daily_basic 对齐的裸响应列（顺序即 items 的下标）。
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
STK_FIELDS = ["ts_code", "trade_date", "up_limit", "down_limit"]
FORECAST_FIELDS = [
    "ts_code",
    "ann_date",
    "end_date",
    "type",
    "p_change_min",
    "p_change_max",
    "net_profit_min",
    "net_profit_max",
    "summary",
]

#: 窗口内固定的三个交易日（2024-01-02 起）与一个预载用的窗口前交易日。
DAYS = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4))
PAD_DAY = date(2023, 12, 29)
NOW = datetime(2024, 1, 10, 21, 0, 0)  # end=2024-01-10：窗口参数可断言、报告路径可预测


def _body(
    items: list[list[str]],
    fields: Sequence[str] = BASIC_FIELDS,
    *,
    has_more: bool | None = None,
    count: int | None = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"fields": list(fields), "items": items}
    if has_more is not None:
        data["has_more"] = has_more
    if count is not None:
        data["count"] = count
    return {"code": 0, "data": data}


def _basic_item(symbol: str, day: date, close: float) -> list[str]:
    return [backfill.ts_code_of(symbol), day.strftime("%Y%m%d"), str(close)] + ["None"] * 16


def _basic_rows(symbol: str, days: Sequence[date], close: float = 10.0) -> list[list[str]]:
    return [_basic_item(symbol, day, close) for day in days]


class FakeRelay:
    """按 ts_code 吐预设行、记下每次调用（params + kwargs）的假转接源。

    `script` 先于默认应答消费：故障注入（RelayUnavailable、截断页、KeyboardInterrupt）
    要能精确落在"第几次调用"上，而不是靠计数器与正常应答搅在一起。
    """

    def __init__(
        self,
        rows_by_symbol: dict[str, list[list[str]]] | None = None,
        *,
        source: str = "rds",
        fields: Sequence[str] = BASIC_FIELDS,
        script: list[Callable[..., tuple[str, dict[str, Any]]]] | None = None,
    ) -> None:
        self.rows_by_symbol = rows_by_symbol or {}
        self.source = source
        self.fields = list(fields)
        self.script = list(script or [])
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def __call__(
        self, api: str, params: dict[str, Any], **kwargs: Any
    ) -> tuple[str, dict[str, Any]]:
        self.calls.append((api, dict(params), dict(kwargs)))
        if self.script:
            step = self.script.pop(0)
            return step(api, params)
        symbol = str(params.get("ts_code", "")).split(".")[0]
        return self.source, _body(self.rows_by_symbol.get(symbol, []), self.fields)


def _plant_daily(root: Path, symbol: str, days: Sequence[date], close: float = 10.0) -> None:
    """把日线K线种进 tmp 干净区：锚点与续跑判据读的都是这本账。"""
    store_bars([bar(day, close, symbol=symbol) for day in days], dataset="daily", root=root)


def _run(
    table: str,
    symbols: Sequence[str],
    fetch: Any,
    *,
    root: Path,
    **kwargs: Any,
) -> backfill.BackfillResult:
    return backfill.run(
        table,
        symbols=list(symbols),
        fetch=fetch,
        anchor=relay_cli.make_anchor(table, limit_of=lambda _symbol: 10.0),
        root=root,
        directory=root / "reports" / "relay",
        **kwargs,
    )


def _partition(root: Path, table: str, symbol: str) -> Path:
    return root / table / "year=2024" / f"symbol={symbol}.parquet"


def _read(root: Path, symbol: str, *, table: str = "daily_basic") -> list[Any]:
    return tables.read_table(table, symbol, date(2024, 1, 1), date(2024, 1, 10), root=root)


# ── 续跑判据与幂等 ───────────────────────────────────────────────────────────


def test_a_symbol_already_complete_on_disk_never_touches_the_network(tmp_path: Path) -> None:
    """应有集 ⊆ 已有集 → 整票零请求。续跑判据读盘，不读 /tmp 运行件（#53 的直接起因）。"""
    _plant_daily(tmp_path, "600519", DAYS)
    tables.write_table(
        parse_daily_basic("rds", BASIC_FIELDS, _basic_rows("600519", DAYS)),
        table="daily_basic",
        root=tmp_path,
    )
    fetch = FakeRelay({"600519": _basic_rows("600519", DAYS)})

    result = _run("daily_basic", ["600519"], fetch, root=tmp_path, now=NOW)

    assert fetch.calls == [], "覆盖已齐还发请求 = 没有读盘判据"
    (outcome,) = result.outcomes
    assert outcome.status == "skipped" and outcome.reason == backfill.SKIP_COMPLETE
    assert result.added == 0 and result.exit_code() == 0


def test_missing_dates_are_fetched_then_a_rerun_changes_nothing(tmp_path: Path) -> None:
    """盘上只有第一天：补 2/3 日；重跑 = 读盘判齐 → 零请求、零改动（mtime 都不变）。"""
    _plant_daily(tmp_path, "600519", DAYS)
    tables.write_table(
        parse_daily_basic("rds", BASIC_FIELDS, _basic_rows("600519", DAYS[:1])),
        table="daily_basic",
        root=tmp_path,
    )
    fetch = FakeRelay({"600519": _basic_rows("600519", DAYS)})

    first = _run("daily_basic", ["600519"], fetch, root=tmp_path, now=NOW)

    assert first.added == 2 and first.repaired == 0
    assert first.existing_skipped == 1, "盘上已有的第一天要计入同键同值跳过"
    assert len(_read(tmp_path, "600519")) == 3
    api, params, _kwargs = fetch.calls[0]
    assert api == "daily_basic"
    assert params["ts_code"] == "600519.SH"
    assert params["start_date"] == "20240102" and params["end_date"] == "20240110"
    partition = _partition(tmp_path, "daily_basic", "600519")
    mtime = partition.stat().st_mtime_ns

    second = _run("daily_basic", ["600519"], fetch, root=tmp_path, now=NOW)

    assert len(fetch.calls) == 1, "重跑又拉了一次 = 判据没读盘"
    assert second.added == 0 and second.rewritten == 0
    (outcome,) = second.outcomes
    assert outcome.status == "skipped" and outcome.reason == backfill.SKIP_COMPLETE
    assert partition.stat().st_mtime_ns == mtime, "重跑动了盘 = 不幂等"


def test_identical_rows_never_touch_disk_even_while_the_symbol_stays_incomplete(
    tmp_path: Path,
) -> None:
    """源一直缺的那一天（日线有、源不给）让票永远"不齐"：每跑都拉，但同键同值零改写。"""
    _plant_daily(tmp_path, "600519", DAYS)  # 应有 3 天
    source_rows = _basic_rows("600519", DAYS[:2])  # 源只有 2 天
    fetch = FakeRelay({"600519": source_rows})

    first = _run("daily_basic", ["600519"], fetch, root=tmp_path, now=NOW)
    assert first.added == 2
    partition = _partition(tmp_path, "daily_basic", "600519")
    mtime = partition.stat().st_mtime_ns

    second = _run("daily_basic", ["600519"], fetch, root=tmp_path, now=NOW)

    assert len(fetch.calls) == 2, "缺口还在（源不给那天），该拉——跳过的粒度是行不是票"
    assert second.added == 0 and second.repaired == 0 and second.rewritten == 0
    assert second.existing_skipped == 2
    assert partition.stat().st_mtime_ns == mtime


# ── 中断续跑对偶（接线对偶，真落盘） ────────────────────────────────────────


def test_interrupted_run_then_resume_equals_one_shot_row_by_row(tmp_path: Path) -> None:
    """跑一半被杀（KeyboardInterrupt 穿透，和真杀进程同形）→ 重跑 → 与一次跑完逐行相同，
    且重跑只补没成的票：已成的票连请求都不发。"""
    symbols = ["600519", "000001", "300308"]
    rows = {code: _basic_rows(code, DAYS[:2]) for code in symbols}
    for code in symbols:
        _plant_daily(tmp_path / "oneshot", code, DAYS[:2])
        _plant_daily(tmp_path / "split", code, DAYS[:2])

    one_shot = _run(
        "daily_basic",
        symbols,
        FakeRelay(rows),
        root=tmp_path / "oneshot",
        now=NOW,
    )
    assert one_shot.added == 6 and not one_shot.failed

    def dying(_api: str, params: dict[str, Any], **_kwargs: Any) -> tuple[str, dict[str, Any]]:
        if str(params["ts_code"]).startswith("000001"):
            raise KeyboardInterrupt  # 第二只票处"被杀"
        symbol = str(params["ts_code"]).split(".")[0]
        return "rds", _body(rows[symbol])

    with pytest.raises(KeyboardInterrupt):
        _run("daily_basic", symbols, dying, root=tmp_path / "split", now=NOW)
    assert [row.trade_date for row in _read(tmp_path / "split", "600519")] == list(DAYS[:2]), (
        "被杀前落成的票要完整留在盘上"
    )
    assert _read(tmp_path / "split", "000001") == [], "被杀那票不许留半份"

    resume_fetch = FakeRelay(rows)
    resumed = _run("daily_basic", symbols, resume_fetch, root=tmp_path / "split", now=NOW)

    assert resumed.exit_code() == 0
    pulled = {str(params["ts_code"]).split(".")[0] for _api, params, _kw in resume_fetch.calls}
    assert pulled == {"000001", "300308"}, "600519 已成还被重拉 = 重复劳动"
    for code in symbols:
        assert _read(tmp_path / "split", code) == _read(tmp_path / "oneshot", code), (
            f"{code} 中断续跑的结果与一次跑完不一致"
        )


# ── 截断硬响（ADR-0015） ────────────────────────────────────────────────────


def test_a_silent_truncation_hard_fails_instead_of_half_payload(tmp_path: Path) -> None:
    """rds 式截断（页满 + has_more + 下一页空 + count 更大）：响、记败、一行不写、不重试。"""
    _plant_daily(tmp_path, "600519", DAYS)
    full_page = _basic_rows("600519", DAYS[:2])  # 撑满 page_size=2 的首页

    def page_one(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return "rds", _body(full_page, has_more=True, count=10)

    def page_two(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        return "rds", _body([], has_more=True, count=10)

    fetch = FakeRelay(script=[page_one, page_two])

    result = _run("daily_basic", ["600519"], fetch, root=tmp_path, now=NOW, page_size=2)

    (outcome,) = result.outcomes
    assert outcome.status == "failed" and "截断" in outcome.reason
    assert len(fetch.calls) == 2, "确定性失败（截断）不许烧 attempts 重发同一请求"
    assert _read(tmp_path, "600519") == [], "半份数据比没有数据更危险——一行都不许写"
    assert result.exit_code() == 1


# ── 钉源、重试、熔断 ─────────────────────────────────────────────────────────


def test_relay_unavailable_is_retried_with_backoff_then_lands(tmp_path: Path) -> None:
    """client 走完阶梯仍 RelayUnavailable → 任务层按 attempts 再问（指数小睡可注入）。"""
    _plant_daily(tmp_path, "600519", DAYS[:2])

    def dead(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        raise RelayUnavailable("rds、promax 上全部走完退避阶梯仍失败")

    fetch = FakeRelay({"600519": _basic_rows("600519", DAYS[:2])}, script=[dead])
    slept: list[float] = []

    result = _run(
        "daily_basic",
        ["600519"],
        fetch,
        root=tmp_path,
        now=NOW,
        attempts=2,
        sleep=slept.append,
    )

    assert slept == [5.0], "第一次失败后按 backoff×2⁰ 小睡，不与 client 阶梯叠成小时级"
    (outcome,) = result.outcomes
    assert outcome.status == "landed" and outcome.added == 2
    assert result.exit_code() == 0


def test_the_breaker_stops_after_consecutive_failed_symbols(tmp_path: Path) -> None:
    """连败到 breaker 就收工：剩下的票一个都不碰、退出码 2（没跑完，不是跑完了有败）。"""
    symbols = ["600519", "000001", "300308", "000002"]
    for code in symbols:
        _plant_daily(tmp_path, code, DAYS[:2])

    def dead(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        raise RelayUnavailable("源全灭")

    fetch = FakeRelay(script=[dead] * 4)
    slept: list[float] = []

    result = _run(
        "daily_basic",
        symbols,
        fetch,
        root=tmp_path,
        now=NOW,
        attempts=1,
        breaker=2,
        sleep=slept.append,
    )

    assert len(fetch.calls) == 2, "breaker=2：第二败就收工，后两票不许再发请求"
    assert slept == [], "attempts=1 时任务层不补睡——阶梯在 client 里已经走过了"
    statuses = [outcome.status for outcome in result.outcomes]
    assert statuses == ["failed", "failed", "breaker", "breaker"]
    assert result.breaker_tripped and result.exit_code() == 2
    assert "熔断收工" in result.markdown


def test_a_success_in_the_middle_resets_the_consecutive_counter(tmp_path: Path) -> None:
    """败-成-败 不许 tripan breaker：连败才是源死了，夹着成功说明源活着。"""
    symbols = ["600519", "000001", "300308"]
    for code in symbols:
        _plant_daily(tmp_path, code, DAYS[:2])

    def dead(_api: str, _params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        raise RelayUnavailable("抖了一下")

    ok = FakeRelay({"000001": _basic_rows("000001", DAYS[:2])})
    calls = {"n": 0}

    def flaky(api: str, params: dict[str, Any], **kwargs: Any) -> tuple[str, dict[str, Any]]:
        calls["n"] += 1
        if calls["n"] == 1 or calls["n"] == 3:  # 第 1、3 次失败，第 2 次成功
            return dead(api, params)
        return ok(api, params, **kwargs)

    result = _run(
        "daily_basic",
        symbols,
        flaky,
        root=tmp_path,
        now=NOW,
        attempts=1,
        breaker=2,
        sleep=lambda _seconds: None,
    )

    assert not result.breaker_tripped
    assert [outcome.status for outcome in result.outcomes] == ["failed", "landed", "failed"]
    assert result.exit_code() == 1, "跑完了但有败票 = 1；熔断才是 2"


# ── 锚点、窗口、异码混入 ─────────────────────────────────────────────────────


def test_close_mismatch_rejects_the_symbol_and_writes_nothing(tmp_path: Path) -> None:
    """daily_basic 的锚是同日收盘（一分钱容差）：对不上 = 整票拒，不是"少一行"。"""
    _plant_daily(tmp_path, "600519", DAYS[:2], close=10.0)
    fetch = FakeRelay({"600519": _basic_rows("600519", DAYS[:2], close=99.0)})

    result = _run("daily_basic", ["600519"], fetch, root=tmp_path, now=NOW)

    (outcome,) = result.outcomes
    assert outcome.status == "failed" and "锚点对账整批拒" in outcome.reason
    assert _read(tmp_path, "600519") == []
    assert result.exit_code() == 1


def test_rows_outside_the_window_are_dropped_even_if_the_source_ignores_the_params(
    tmp_path: Path,
) -> None:
    """promax 不认 start/end 的兜底：解析后仍按窗口裁——窗口外的行不进锚也不进盘。"""
    _plant_daily(tmp_path, "600519", [PAD_DAY, *DAYS])
    outside = _basic_item("600519", PAD_DAY, 10.0)  # 2023-12-29，在窗口之前
    fetch = FakeRelay({"600519": [outside, *_basic_rows("600519", DAYS[:2])]})

    result = _run("daily_basic", ["600519"], fetch, root=tmp_path, now=NOW)

    (outcome,) = result.outcomes
    assert outcome.status == "landed" and outcome.fetched == 2
    written = _read(tmp_path, "600519")
    assert [row.trade_date for row in written] == list(DAYS[:2])
    assert PAD_DAY not in {row.trade_date for row in written}


def test_a_response_filtered_to_the_wrong_symbol_is_refused(tmp_path: Path) -> None:
    """源没按 ts_code 过滤、混回别的代码：不可信，整票拒——不是"多出来的行当赠品收下"。"""
    _plant_daily(tmp_path, "600519", DAYS[:1])
    _plant_daily(tmp_path, "000001", DAYS[:1])
    fetch = FakeRelay({"600519": _basic_rows("000001", DAYS[:1])})  # 问 600519 回 000001

    result = _run("daily_basic", ["600519"], fetch, root=tmp_path, now=NOW)

    (outcome,) = result.outcomes
    assert outcome.status == "failed" and "混入其它代码" in outcome.reason
    assert result.exit_code() == 1


def test_stk_limit_anchor_uses_the_preloaded_prev_close_including_the_pad_day(
    tmp_path: Path,
) -> None:
    """stk_limit 锚要昨收：2024-01-02 的昨收是 2023-12-29——窗口前那天靠预载的回带读进来。
    没有回带 = 昨收 None = 整票锚不上被剔，所以"落得下"本身就是这条路径的证据。"""
    _plant_daily(tmp_path, "600519", [PAD_DAY, date(2024, 1, 2)], close=10.0)
    fetch = FakeRelay(
        {"600519": [["600519.SH", "20240102", "11.0", "9.0"]]},
        fields=STK_FIELDS,
    )

    result = _run("stk_limit", ["600519"], fetch, root=tmp_path, now=NOW)

    (outcome,) = result.outcomes
    assert outcome.status == "landed" and outcome.added == 1
    assert tables.read_table(
        "stk_limit", "600519", date(2024, 1, 1), date(2024, 1, 10), root=tmp_path
    )


# ── forecast（公告类：无窗口、主键幂等） ─────────────────────────────────────


FORECAST_ITEM = ["600519.SH", "20240430", "20240331", "预增", "50.0", "60.0", "", "", "预增公告"]


def test_forecast_pulls_full_history_and_the_rerun_only_skips_keys(tmp_path: Path) -> None:
    """公告类表没有"应有日全集"：每跑都拉全史，靠主键幂等——两次跑，盘上一行不多不少。"""
    fetch = FakeRelay({"600519": [FORECAST_ITEM]}, fields=FORECAST_FIELDS)

    first = _run("forecast", ["600519"], fetch, root=tmp_path)
    _api, params, _kwargs = fetch.calls[0]
    assert "start_date" not in params and "end_date" not in params, "全史拉取不带窗口"
    assert params["ts_code"] == "600519.SH"
    assert first.added == 1 and first.exit_code() == 0
    partition = tmp_path / "forecast" / "year=2024" / "symbol=600519.parquet"
    mtime = partition.stat().st_mtime_ns

    second = _run("forecast", ["600519"], fetch, root=tmp_path)

    assert len(fetch.calls) == 2, "公告类表判不出'齐'，整票跳过对它不成立"
    assert second.added == 0 and second.repaired == 0 and second.rewritten == 0
    assert second.existing_skipped == 1, "行级跳过要单独计数，不能与按日表的票级跳过混报"
    assert partition.stat().st_mtime_ns == mtime


# ── 报告与覆盖 ───────────────────────────────────────────────────────────────


def test_the_report_answers_the_four_questions_from_the_task(tmp_path: Path) -> None:
    """报告四件事：新增/改写、跳过、失败及原因、盘上最终覆盖（票 × 日）——都有数可对。"""
    done, missing, broken = "600519", "000001", "300308"
    _plant_daily(tmp_path, done, DAYS[:2])
    _plant_daily(tmp_path, missing, DAYS[:2])
    _plant_daily(tmp_path, broken, DAYS[:2])
    tables.write_table(
        parse_daily_basic("rds", BASIC_FIELDS, _basic_rows(done, DAYS[:2])),
        table="daily_basic",
        root=tmp_path,
    )
    fetch = FakeRelay(
        {
            missing: _basic_rows(missing, DAYS[:2]),
            broken: _basic_rows(broken, DAYS[:2], close=99.0),  # 锚拒
        }
    )

    result = _run("daily_basic", [done, missing, broken], fetch, root=tmp_path, now=NOW)

    text = result.report.read_text(encoding="utf-8")
    assert result.markdown in text
    assert f"新增 {result.added} 行" in text and result.added == 2
    assert f"同键同值跳过 {result.existing_skipped} 行" in text
    assert "覆盖已齐 1 / 日线无参考 0" in text
    assert f"失败 {len(result.failed)} 只" in text and broken in text
    assert "锚点对账整批拒" in text
    assert "盘上最终覆盖：daily_basic 共 2 只 × 2 个日期" in text
    assert result.report.parent == tmp_path / "reports" / "relay" / NOW.date().isoformat()
    assert result.report.name.startswith("backfill-daily_basic-")


def test_coverage_counts_symbols_and_distinct_days_from_disk(tmp_path: Path) -> None:
    assert backfill.coverage("daily_basic", root=tmp_path) == (0, 0, None, None)
    tables.write_table(
        parse_daily_basic("rds", BASIC_FIELDS, _basic_rows("600519", DAYS[:2])),
        table="daily_basic",
        root=tmp_path,
    )
    tables.write_table(
        parse_daily_basic("rds", BASIC_FIELDS, _basic_rows("300308", DAYS[1:], close=20.0)),
        table="daily_basic",
        root=tmp_path,
    )
    symbols, days, first, last = backfill.coverage("daily_basic", root=tmp_path)
    assert (symbols, days) == (2, 3)
    assert first == DAYS[0] and last == DAYS[2]


# ── CLI 接线与 fail-closed ───────────────────────────────────────────────────


def test_an_unknown_command_line_argument_stops_before_any_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """多余参数 = 退出码 2，且一步网络都不发（独立审计 M1 / 任务 #30，zx-site 同款钉法）。"""
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))
    sent: list[tuple[tuple[object, ...], dict[str, object]]] = []
    import zhixing_quant.sources.relay.client as client_mod

    def record(*args: object, **kwargs: object) -> tuple[str, dict[str, Any]]:
        sent.append((args, kwargs))
        return "rds", {}

    monkeypatch.setattr(client_mod, "fetch", record)

    with pytest.raises(SystemExit) as gone:
        relay_cli.main(["backfill", "--table", "daily_basic", "--nope"])

    assert gone.value.code == 2
    assert sent == [], "参数错了还发请求 = 先跑再判"


def test_an_unknown_relay_choice_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))
    with pytest.raises(SystemExit) as gone:
        relay_cli.main(["backfill", "--relay", "baidu"])
    assert gone.value.code == 2, "--relay 的取值域钉死 rds|promax：写错字不许当自动模式跑"


def _plant_cli_daily(data_root: Path, symbol: str, days: Sequence[date]) -> None:
    _plant_daily(data_root / "data", symbol, days)


def test_relay_flag_is_wired_into_client_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--relay rds → client 只拿 (rds,)；不给 → ADR-0014 顺序 (rds, promax)。中途换源由
    client 的阶梯-切源负责（tests/test_relay_tables.py 已钉），这里钉的是"钉没钉住"。"""
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))
    _plant_cli_daily(tmp_path, "600519", DAYS[:1])
    seen: list[tuple[str, ...]] = []

    def record(_api: str, _params: dict[str, Any], **kwargs: Any) -> tuple[str, dict[str, Any]]:
        seen.append(tuple(kwargs.get("relays", ())))
        return "rds", _body([])

    import zhixing_quant.sources.relay.client as client_mod

    monkeypatch.setattr(client_mod, "fetch", record)

    pinned = relay_cli.main(
        ["backfill", "--table", "daily_basic", "--symbols", "600519", "--relay", "rds"]
    )
    auto = relay_cli.main(["backfill", "--table", "daily_basic", "--symbols", "600519"])

    assert pinned == 0 and auto == 0
    assert seen == [("rds",), ("rds", "promax")]
    out = capsys.readouterr().out
    assert "转接源参考表回填 · daily_basic" in out and "报告落" in out


def test_the_full_market_pool_honours_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """不给 --symbols = 主数据全市场，--limit 截断：池子多大由这两个旋钮说，代码不猜。"""
    data_root = snapshot_root(tmp_path, monkeypatch)  # 真快照：600519/300750 在册
    seen: list[str] = []

    def record(_api: str, params: dict[str, Any], **_kwargs: Any) -> tuple[str, dict[str, Any]]:
        seen.append(str(params.get("ts_code", "")))
        return "rds", _body([], FORECAST_FIELDS)

    import zhixing_quant.sources.relay.client as client_mod

    monkeypatch.setattr(client_mod, "fetch", record)

    code = relay_cli.main(["backfill", "--table", "forecast", "--limit", "1"])

    assert code == 0
    assert len(seen) == 1 and seen[0] in {"600519.SH", "300750.SZ"}
    assert (data_root / "reports" / "relay").is_dir(), "报告要落数据根，不落仓库"


# ── 入参校验（fail-closed 的另一半） ─────────────────────────────────────────


def test_bad_parameters_raise_before_touching_anything(tmp_path: Path) -> None:
    dead_fetch = FakeRelay()

    def dead_anchor(
        _rows: list[Any], _refs: backfill.BarRefs
    ) -> tuple[list[str], int, list[Any], list[str]]:
        return [], 0, [], []

    with pytest.raises(ValueError, match="不在回填支持清单"):
        backfill.run(
            "fina_audit",
            symbols=["600519"],
            fetch=dead_fetch,
            anchor=dead_anchor,
            root=tmp_path,
            directory=tmp_path / "r",
        )
    with pytest.raises(ValueError, match="attempts/breaker"):
        backfill.run(
            "daily_basic",
            symbols=["600519"],
            fetch=dead_fetch,
            anchor=dead_anchor,
            root=tmp_path,
            directory=tmp_path / "r",
            attempts=0,
        )
    with pytest.raises(ValueError, match="票池是空"):
        backfill.run(
            "daily_basic",
            symbols=[],
            fetch=dead_fetch,
            anchor=dead_anchor,
            root=tmp_path,
            directory=tmp_path / "r",
        )
    with pytest.raises(ValueError, match="区间颠倒"):
        backfill.run(
            "daily_basic",
            symbols=["600519"],
            fetch=dead_fetch,
            anchor=dead_anchor,
            root=tmp_path,
            directory=tmp_path / "r",
            start=date(2024, 2, 1),
            now=NOW,
        )
    assert dead_fetch.calls == []


# ── report_rc（短页族）与 page_size 按表封顶（2026-09-25 实测 400 的回归） ──────

RC_FIELDS = ["ts_code", "report_date", "org_name", "quarter", "eps", "pe"]
RC_ITEM = ["600519.SH", "20240425", "高盛集团（GoldmanSachs）", "2024Q4", "67.07", "21.7"]


def test_forecast_default_page_size_is_the_interface_cap(tmp_path: Path) -> None:
    """page_size 缺省 = 按表上限：forecast 实测 max_limit=1000，全局 5000 会整池 400
    （2026-09-25 全市场回填 5568 连败的根因）。缺省路径必须落到 1000。"""
    fetch = FakeRelay({"600519": [FORECAST_ITEM]}, fields=FORECAST_FIELDS)

    result = _run("forecast", ["600519"], fetch, root=tmp_path)

    assert result.added == 1
    _api, params, _kwargs = fetch.calls[0]
    assert params["limit"] == 1000, (
        f"forecast 缺省 limit 必须是 max_limit=1000，实际 {params['limit']}"
    )
    assert result.page_size == 1000, "报告里的 page_size 要点名真实发出的那个尺寸"


def test_forecast_explicit_oversize_page_size_fails_before_any_request(tmp_path: Path) -> None:
    """显式超上限 = 发前拒（fail-closed，transport 零调用）：错参数要响，不许静默降级继续跑。"""
    fetch = FakeRelay({"600519": [FORECAST_ITEM]}, fields=FORECAST_FIELDS)

    result = _run("forecast", ["600519"], fetch, root=tmp_path, page_size=5000)

    (outcome,) = result.outcomes
    assert outcome.status == "failed" and "上限 1000" in outcome.reason
    assert fetch.calls == [], "超上限参数一个请求都不许发"
    assert result.exit_code() == 1


def test_report_rc_pulls_full_history_through_the_short_page_guard(tmp_path: Path) -> None:
    """report_rc 进回填清单：公告类全史（无窗口参数）、短页守卫（limit=5000、offset=0）、
    行级锚放行、主键幂等——重跑一行不多。"""
    fetch = FakeRelay({"600519": [RC_ITEM]}, fields=RC_FIELDS)

    first = _run("report_rc", ["600519"], fetch, root=tmp_path, now=NOW)

    assert first.added == 1 and first.exit_code() == 0
    _api, params, _kwargs = fetch.calls[0]
    assert params["ts_code"] == "600519.SH"
    assert "start_date" not in params and "end_date" not in params, "全史拉取不带窗口"
    assert params["limit"] == 5000 and params["offset"] == 0, "短页族固定 5000 对齐整页"
    written = tables.read_table(
        "report_rc", "600519", date(2024, 1, 1), date(2024, 12, 31), root=tmp_path
    )
    assert [row.quarter for row in written] == ["2024Q4"]
    partition = tmp_path / "report_rc" / "year=2024" / "symbol=600519.parquet"
    mtime = partition.stat().st_mtime_ns

    second = _run("report_rc", ["600519"], fetch, root=tmp_path, now=NOW)

    assert len(fetch.calls) == 2, "公告类表判不出'齐'（#53 口径）：每跑重拉全史"
    assert second.added == 0 and second.repaired == 0 and second.rewritten == 0
    assert second.existing_skipped == 1, "主键幂等：同键同值一行不动"
    assert partition.stat().st_mtime_ns == mtime, "重跑动了盘 = 不幂等"


def test_report_rc_future_publication_fails_the_symbol_without_writing(tmp_path: Path) -> None:
    """行级锚（发布日 ≤ 今天）拦在未来日期上：整票拒、一行不写。"""
    future = ["600519.SH", "20990101", "中泰证券", "2026Q4", "70.97", "18.11"]
    fetch = FakeRelay({"600519": [future]}, fields=RC_FIELDS)

    result = _run("report_rc", ["600519"], fetch, root=tmp_path, now=NOW)

    (outcome,) = result.outcomes
    assert outcome.status == "failed" and "锚点对账整批拒" in outcome.reason
    assert "report_date 在未来" in outcome.reason
    assert (
        tables.read_table("report_rc", "600519", date(2090, 1, 1), date(2100, 1, 1), root=tmp_path)
        == []
    )
