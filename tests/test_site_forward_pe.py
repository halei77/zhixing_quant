"""`forward_pe` 组件（ADR-0022 决定 2 + 03-L4 未来函数禁令）的对抗性测试。

怀疑点清单（这一族错了照样能生成、生成出来却在骗模型）：

1. **as_of 回填**（03-L4 的头号错法）：用 as_of 那天的预测钉给窗口每一天 = 把今天才知道的
   盈利预测递给 120 天前的判断。测试**植入这个错误实现**，证明本 fixture 能把它测红
   （正确序列 ≠ 回填序列，且回填版的 est_asof 会晚于行日期）。
2. **点时可见性**：发布日晚于 t 的修正，t 那天一个字都不许看到；发布日 ≤ t 的当时最新才作数。
3. **换挡**：跨年后 FY1 自动从 2026 滚到 2027（无需新研报）；t 之后的修正不在 t 的行里。
4. **缺测的三种写法**：无预测日是「—」——不是 0、更不是 pe_ttm；全窗无预测出 `NO_DATA_NOTE`
   （ADR-0020），不给只有表头的空表。
5. **分母口径**：年内取 Q4（全年），不拿 Q1–Q3 的年内累计 EPS 当分母（实测累计会把 PE 放大
   数倍）；同日多券商取字典序首个（发布日只有日期粒度，确定性即可）。

组合根的接线（读哪份盘、不复权、全史窗口）由 build 级测试判；纯对齐逻辑不碰盘。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tests.fakes import bar, snapshot_root
from zhixing_quant.domain.bar import Bar
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.site import table, tokens
from zhixing_quant.site.prompt import NO_DATA_NOTE, build, title_of
from zhixing_quant.site.table import (
    ForwardRow,
    IncompleteComponent,
    align_forward_pe,
    render_forward_pe,
)
from zhixing_quant.site.templates import Selection, Template
from zhixing_quant.sources.relay.tables import ReportRcRow
from zhixing_quant.storage import layout
from zhixing_quant.storage.tables import write_table
from zhixing_quant.storage.write import store_bars

#: 2026-09-14（周一）.. 2026-09-18（周五）：一个完整交易周，够摆"窗口中段发布修正"。
D1, D2, D3, D4, D5 = (date(2026, 9, day) for day in range(14, 19))
WEEK = (D1, D2, D3, D4, D5)
#: 窗口收盘一律 100.0：期望值就是 100 ÷ eps，一眼对得出谁除错了。
CLOSE = 100.0


def bars(closes: dict[date, float] | None = None) -> list[Bar]:
    table_close = closes or dict.fromkeys(WEEK, CLOSE)
    return [bar(day, price) for day, price in table_close.items()]


def forecast(
    report_date: date,
    *,
    eps: float = 50.0,
    quarter: str = "2026Q4",
    org: str = "中泰证券",
    pe: float | None = None,
) -> ReportRcRow:
    """一支研报行。eps 默认 50：100 ÷ 50 = 2.00，期望值不用计算器。"""
    return ReportRcRow("rds", "600519", report_date, org, quarter, eps, pe)


def _wrong_as_of_backfill(
    forecasts: list[ReportRcRow], as_of: date, window: tuple[date, ...]
) -> list[ForwardRow]:
    """**错误实现（对抗靶）**：取 as_of 当天可见的"当时最新"预测，回填给窗口每一天。

    这是 03-L4 点名的未来函数错法——把今天才知道的盈利预测递给窗口最旧那天。它与正确
    实现共用 FY1 挑选（`table._fy1_of`），错的只在"钉给哪几天"：正实现逐日点时，它一把
    钉全窗。任何一条对值/不变量断言都会把它测红——这正是本 fixture 存在的理由。
    """
    visible = [row for row in forecasts if row.report_date <= as_of]
    latest = max(row.report_date for row in visible)
    picked = table._fy1_of([row for row in visible if row.report_date == latest], as_of)
    assert picked is not None
    return [
        ForwardRow(day, CLOSE, CLOSE / picked.eps, picked.quarter, picked.report_date)
        for day in window
    ]


# ── 逐日点时：核心不变量与对值 ────────────────────────────────────────────────


def test_each_day_uses_the_forecast_visible_on_that_day_not_the_windows_latest() -> None:
    """窗口中段有一次预测修正（D3 发布，eps 50→100）：D1/D2 用旧预测，D3 起用新预测。

    est_asof 逐行跟真：前两天印的是旧发布日——把点时可见性印在表里（ADR-0022 决定 2）。
    """
    forecasts = [forecast(D1, eps=50.0), forecast(D3, eps=100.0)]
    rows = align_forward_pe(bars(), forecasts)

    assert [(row.fwd_pe, row.est_asof) for row in rows] == [
        (2.0, D1),  # 100 ÷ 50，as_of=D1 的当时最新是 D1 那份
        (2.0, D1),
        (1.0, D3),  # 修正发布当天就切换——发布日 ≤ t 是包含关系（ADR 原文）
        (1.0, D3),
        (1.0, D3),
    ]


def test_the_as_of_backfill_wrong_implementation_is_catched_by_this_fixture() -> None:
    """对抗测试（03-L4）：植入"用 as_of 那天的预测回填全窗"，本 fixture 必须把它测红。"""
    forecasts = [forecast(D1, eps=50.0), forecast(D3, eps=100.0)]
    correct = align_forward_pe(bars(), forecasts)
    wrong = _wrong_as_of_backfill(forecasts, as_of=D5, window=WEEK)

    # 红路一（对值）：正确序列的 D1 是 2.00（当时只有旧预测），回填版是 1.00（今天的预测泄漏）。
    assert [row.fwd_pe for row in correct] == [2.0, 2.0, 1.0, 1.0, 1.0]
    assert [row.fwd_pe for row in wrong] == [1.0] * 5
    assert correct != wrong, "fixture 辨别力：两种实现必须给出不同序列，否则测了等于没测"
    # 红路二（不变量）：回填版把 est_asof=D5 钉在 D1 的行上——est_asof ≤ 行日期当场破。
    assert wrong[0].est_asof == D3 > D1, "回填版把 D1 的行钉上 D3 的发布日——不变量当场破"
    for row in correct:
        assert row.est_asof is None or row.est_asof <= row.trade_date, (
            f"逐日点时不变量破了：{row.trade_date} 用了 {row.est_asof} 发布的预测"
        )


# ── 点时可见性：早/晚两份预测 ─────────────────────────────────────────────────


def test_a_forecast_published_after_t_never_reaches_ts_row() -> None:
    """同一天打开数据，两份预测一早一晚：早的（发布日 ≤ t）t 看得到，晚的（发布日 > t）
    在 t 的行里一个字都不许出现——晚的那份要到它自己的发布日才上岗。"""
    early = forecast(D2, eps=50.0)  # 早：D2 发布
    late = forecast(D4, eps=5.0, org="华泰证券")  # 晚：D4 才发布（对 D1–D3 是未来）
    rows = align_forward_pe(bars(), [early, late])

    assert [(row.fwd_pe, row.est_asof) for row in rows] == [
        (None, None),  # D1：两份都还没发布
        (2.0, D2),  # D2–D3：只看得到早的（100÷50），晚的 eps=5 不许提前泄漏
        (2.0, D2),
        (20.0, D4),  # D4 起晚的成为"当时最新"（100÷5）
        (20.0, D4),
    ]
    assert all(row.est_asof is None or row.est_asof <= row.trade_date for row in rows)


def test_same_day_multi_broker_forecasts_pick_deterministically() -> None:
    """同一发布日多份研报（实测 41% 的 (date, quarter) 同日多券商）：发布日只有日期粒度，
    日内先后无从分辨——取字典序首个 org，输入顺序怎么翻结果都一样（不发明一致预期口径）。"""
    a = forecast(D2, eps=40.0, org="高盛集团（GoldmanSachs）")
    b = forecast(D2, eps=60.0, org="中泰证券")

    for order in ([a, b], [b, a]):
        rows = align_forward_pe(bars(), order)
        at_d2 = rows[WEEK.index(D2)]
        assert at_d2.est_asof == D2
        # 字典序：'中' (U+4E2D) < '高' (U+9AD8) → 中泰证券的 60 先
        assert at_d2.est_period == "2026Q4" and at_d2.fwd_pe == pytest.approx(100.0 / 60.0)
        pick = table._fy1_of([row for row in order if row.report_date == D2], D2)
        assert pick is not None and pick.org_name == "中泰证券"


# ── 换挡与修正可见性 ──────────────────────────────────────────────────────────


def test_fy1_rolls_over_the_year_boundary_without_a_new_report() -> None:
    """FY1 = 财年 ≥ t.year 的最前一个：跨年后自动换挡到下一年——同一批研报即可，
    不需要新发布。2026-12-31 收盘用 2026Q4 的 50，2027-01-04 收盘用 2027Q4 的 60。"""
    edges = [date(2026, 12, 30), date(2026, 12, 31), date(2027, 1, 4)]
    edge_bars = [bar(day, CLOSE) for day in edges]
    forecasts = [
        forecast(date(2026, 9, 1), eps=50.0, quarter="2026Q4"),
        forecast(date(2026, 9, 1), eps=60.0, quarter="2027Q4"),
    ]
    rows = align_forward_pe(edge_bars, forecasts)
    assert [(row.fwd_pe, row.est_period) for row in rows] == [
        (2.0, "2026Q4"),
        (2.0, "2026Q4"),  # 当年最后一天，FY2026 还没披露完——仍是当年
        (100.0 / 60.0, "2027Q4"),  # 新年第一个交易日换挡
    ]


def test_a_revision_published_after_t_does_not_rewrite_earlier_rows() -> None:
    """t 看不到 t 之后的修正：D5 发布的 70 只改 D5 自己那天，D4 及以前纹丝不动。"""
    forecasts = [
        forecast(date(2026, 8, 1), eps=50.0, quarter="2026Q4"),
        forecast(D5, eps=70.0, quarter="2026Q4"),
    ]
    rows = align_forward_pe(bars(), forecasts)
    assert [row.fwd_pe for row in rows] == [2.0, 2.0, 2.0, 2.0, 100.0 / 70.0]
    assert [row.est_asof for row in rows] == [
        date(2026, 8, 1),
        date(2026, 8, 1),
        date(2026, 8, 1),
        date(2026, 8, 1),
        D5,
    ]


# ── 分母口径：年内最全报告期、eps≤0、缺测 ─────────────────────────────────────


def test_the_annual_q4_eps_wins_over_same_year_cumulative_quarters() -> None:
    """同一报告四行累计 EPS（实测形状：国泰君安 18.9/33.4/49.2/69.58 逐级累计）：
    分母取 Q4 全年。拿 Q2 的年内累计当分母会把 26 倍的 PE 读成 51 倍。"""
    day = D3
    forecasts = [
        forecast(day, eps=18.9, quarter="2026Q1"),
        forecast(day, eps=33.4, quarter="2026Q2"),
        forecast(day, eps=49.2, quarter="2026Q3"),
        forecast(day, eps=69.58, quarter="2026Q4"),
    ]
    rows = align_forward_pe(bars(), forecasts)
    at = rows[WEEK.index(day)]
    assert at.est_period == "2026Q4"
    assert at.fwd_pe == pytest.approx(CLOSE / 69.58)


def test_days_without_a_forecast_render_dash_neither_zero_nor_pe_ttm() -> None:
    """无预测日：「—」。不是 0（0 填充会把"没这数"洗成"这数为 0"），更不回落 pe_ttm
    （两条不同定义的线接缝会被读成估值跳变——ADR-0022 决定 2 明禁）。"""
    rows = align_forward_pe(bars(), [forecast(D3, eps=50.0)])
    text = render_forward_pe(rows, fields=("close", "fwd_pe", "est_period", "est_asof"))

    first_line = next(line for line in text.splitlines() if line.startswith("| 2026-09-14"))
    # 格子逐个看：close、fwd_pe、est_period、est_asof 全是「—」族里的缺测格——不是 0
    cells = [cell.strip() for cell in first_line.strip("|").split("|")]
    assert cells[0] == "2026-09-14" and cells[1] == "100.00"
    assert cells[2:] == ["—", "—", "—"]
    # 列头里不许有 pe_ttm/PE(TTM)：口径行里那句"不回落PE(TTM)"是声明，不是列
    header = next(line for line in text.splitlines() if line.startswith("| 时间 |"))
    assert header == "| 时间 | 收盘 | 远期PE | 预测报告期 | 预测发布日 |"
    third_line = next(line for line in text.splitlines() if line.startswith("| 2026-09-16"))
    assert "2.00" in third_line and "2026-09-16" in third_line


def test_the_latest_forecast_with_nonpositive_eps_yields_dash_for_that_day() -> None:
    """最新一份预测是亏损（eps≤0）→ 当日「—」：负盈利的 PE 无意义，也不回落到更早的
    正 EPS 旧报——那会把旧预期冒充成当前最新。"""
    forecasts = [
        forecast(date(2026, 8, 1), eps=50.0),
        forecast(D3, eps=-2.0),
    ]
    rows = align_forward_pe(bars(), forecasts)
    # D1/D2 的当时最新还是旧的正 EPS 报（点时）；D3 起亏损报成为最新 → 「—」不回落
    assert [row.fwd_pe for row in rows] == [2.0, 2.0, None, None, None]
    text = render_forward_pe(rows, fields=("close", "fwd_pe", "est_period", "est_asof"))
    for line in text.splitlines():
        if line.startswith("| 2026-09-1") and line.split("|")[1].strip() >= "2026-09-16":
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            assert cells[2:] == ["—", "—", "—"], f"亏损预测日不许印出任何 PE：{cells}"


# ── 渲染与口径行 ──────────────────────────────────────────────────────────────


def test_render_carries_the_caliber_line_and_the_four_fields() -> None:
    rows = align_forward_pe(bars(), [forecast(D1, eps=50.0)])
    fields = ("close", "fwd_pe", "est_period", "est_asof")
    text = render_forward_pe(rows, fields=fields)
    assert "远期PE口径" in text and "不复权收盘" in text and "不回落PE(TTM)" in text
    assert "| 时间 | 收盘 | 远期PE | 预测报告期 | 预测发布日 |" in text
    assert "| 2026-09-14 | 100.00 | 2.00 | 2026Q4 | 2026-09-14 |" in text
    csv_text = render_forward_pe(rows, fields=fields, format="csv")
    assert csv_text.startswith("# 远期PE口径")
    assert "2026-09-14,100.00,2.00,2026Q4,2026-09-14" in csv_text
    assert table.reference_label("forward_pe") in text


def test_render_refuses_to_hand_out_an_empty_skeleton() -> None:
    with pytest.raises(IncompleteComponent, match="一张都没有"):
        render_forward_pe([], fields=("close", "fwd_pe", "est_period", "est_asof"))


def test_title_of_forward_pe_is_registered_not_invented() -> None:
    assert title_of("forward_pe") == "远期PE（逐日点时）"


# ── 组合根接线（读盘现算，ADR-0012 + ADR-0022） ───────────────────────────────


def _tmpl(days: int) -> Template:
    return Template(
        name="短期投资",
        role="你是资深 A 股分析师",
        task="判断机会与风险",
        data=(
            Selection(dataset=layout.DAILY, days=days),
            Selection(
                dataset="forward_pe",
                days=days,
                fields=("close", "fwd_pe", "est_period", "est_asof"),
            ),
        ),
        output="先给结论，再给依据。",
        format="markdown",
        fields=("open", "close"),
        adjust="raw",
        status="ready",
        waiting_on="",
    )


@pytest.fixture
def disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """三天不复权日线（100/100/100）+ 可选 report_rc 行的干净区，落在 tmp 数据根里。"""
    base = snapshot_root(tmp_path, monkeypatch)
    store_bars([bar(day, CLOSE) for day in WEEK[:3]], dataset=layout.DAILY, root=base / "data")
    return base / "data"


def _write_forecasts(root: Path, rows: list[ReportRcRow]) -> None:
    write_table(rows, table="report_rc", root=root)


def test_build_computes_the_series_server_side_and_shares_the_daily_window(
    disk: Path,
) -> None:
    """服务端现算：提示词里的远期 PE 与日K同窗同 days，逐格是收盘÷当时可见预测。"""
    _write_forecasts(disk, [forecast(D1, eps=50.0)])
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    text = build(
        _tmpl(3),
        "600519",
        D3,
        calendar=calendar,
        token_warn_above=100_000,
    ).text

    assert "### 日K（3 个交易日）" in text
    assert "### 远期PE（逐日点时）（3 个交易日）" in text, "与日K同 days：盘上几天两边都几天"
    assert "| 2026-09-14 | 100.00 | 2.00 | 2026Q4 | 2026-09-14 |" in text
    assert "远期PE口径" in text
    # 不复权口径：bar 默认因子 8，后复权会把 close 印成 800——这里必须是 100.00
    assert "| 800.00 |" not in text


def test_build_a_window_without_any_forecast_gives_the_no_data_note(
    disk: Path,
) -> None:
    """窗口里一天预测都没有 → `NO_DATA_NOTE` 整节交代（ADR-0020），不给只有表头的空表。"""
    assert disk.is_dir()  # 夹具只负责把数据根钉进 env；本例断言"没有 report_rc 行"的那一侧
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    text = build(
        _tmpl(3),
        "600519",
        D3,
        calendar=calendar,
        token_warn_above=100_000,
    ).text

    assert "### 远期PE（逐日点时）" in text
    assert NO_DATA_NOTE.splitlines()[0] in text
    section = text.split("### 远期PE（逐日点时）")[1]
    assert "预测报告期" not in section, "空段不许给表骨架——空表会被读成那段时间没有波动"


def test_build_treats_forecasts_published_after_the_window_as_no_data(
    disk: Path,
) -> None:
    """研报全在窗口之后才发布：窗口内每一天都取不到 → 同样整节交代，不是一张全「—」表。"""
    _write_forecasts(disk, [forecast(date(2026, 12, 1), eps=50.0)])
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    text = build(
        _tmpl(3),
        "600519",
        D3,
        calendar=calendar,
        token_warn_above=100_000,
    ).text

    section = text.split("### 远期PE（逐日点时）")[1]
    assert NO_DATA_NOTE.splitlines()[0] in section
    assert "2026Q4" not in section


def test_build_never_leaks_tomorrows_forecast_into_yesterday(
    disk: Path,
) -> None:
    """组合根整条链的点时断言：D2 发布的修正只从 D2 那行起出现，D1 行用不上它。"""
    _write_forecasts(
        disk,
        [forecast(D1, eps=50.0), forecast(D2, eps=100.0)],
    )
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    text = build(
        _tmpl(3),
        "600519",
        D3,
        calendar=calendar,
        token_warn_above=100_000,
    ).text

    # 远期 PE 表是 5 列 6 根竖线；日K 表（open+close）只有 3 列——按格数认表，不认日期撞车
    forward_rows = [
        line for line in text.splitlines() if line.startswith("| 2026-") and line.count("|") == 6
    ]
    day1_line = next(line for line in forward_rows if line.startswith("| 2026-09-14 |"))
    day2_line = next(line for line in forward_rows if line.startswith("| 2026-09-15 |"))
    assert "| 2.00 |" in day1_line and "2026-09-14" in day1_line, "D1 只认 D1 的预测"
    assert "| 1.00 |" in day2_line, "修正从发布日当天上岗"
    day1_cells = [cell.strip() for cell in day1_line.strip("|").split("|")]
    assert day1_cells[4] == "2026-09-14", "D1 行印的 est_asof 是它自己那份的发布日"
    day2_cells = [cell.strip() for cell in day2_line.strip("|").split("|")]
    assert day2_cells[4] == "2026-09-15", "修正后的行 est_asof 跟着切到发布日"
    assert tokens.estimate(text) > 0


def test_token_estimate_still_covers_the_prompt(disk: Path) -> None:
    """加一节 3 行表不顶破阈值口径——tokens.estimate 对成型提示词出正数（06 §八-5 的底座）。"""
    _write_forecasts(disk, [forecast(D1, eps=50.0)])
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    prompt = build(_tmpl(3), "600519", D3, calendar=calendar, token_warn_above=100_000)
    assert prompt.tokens > 0 and prompt.warn is None
