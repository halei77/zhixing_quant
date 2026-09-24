"""`fina_trend` 组件（ROE/营收与净利增速序列，06 §十-5 + 03-L4 未来函数禁令）的对抗性测试。

怀疑点清单（这一族错了照样能生成、生成出来却在骗模型）：

1. **as_of 回填**（03-L4 的头号错法）：用 as_of 那天可见的最新一期回填给窗口每一天 =
   把今天才披露的 ROE/增速递给窗口最旧那天的判断。测试**植入这个错误实现**，证明本
   fixture 能把它测红（正确序列 ≠ 回填序列，且回填版的 fina_asof 会晚于行日期）。
2. **点时可见性**：首披日晚于 t 的一期，t 那天一个字都不许看到；ann_date ≤ t 的当时最新
   报告期才作数——`end_date` 是报告期不是发布日（拿它当点时就是未来函数）。
3. **换期与重述**：窗口中段披露新报告期 → 从披露日当行切换；同报告期的重述（晚 ann_date）
   从它的首披日起上岗、不改写更早的行；**更晚披露的旧期间压不过更新的报告期**。
4. **缺测的两种写法**：首份财报披露前的日子是「—」——不是 0；全窗没有已披露财报出
   `NO_DATA_NOTE`（ADR-0020），不给只有表头的空表。
5. **全史读**：窗口起点之前披露的老财报要算进"当时最新"——只读窗口内会把那段读成"无数据"。

组合根的接线（读哪份盘、首披日全史、与日K同窗）由 build 级测试判；纯对齐逻辑不碰盘。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from tests.fakes import bar, snapshot_root
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.site import table, tokens
from zhixing_quant.site.prompt import NO_DATA_NOTE, build, title_of
from zhixing_quant.site.table import (
    FinaTrendRow,
    IncompleteComponent,
    align_fina_trend,
    render_fina_trend,
)
from zhixing_quant.site.templates import Selection, Template
from zhixing_quant.sources.relay.tables import FinaIndicatorRow
from zhixing_quant.storage import layout
from zhixing_quant.storage.tables import write_table
from zhixing_quant.storage.write import store_bars

#: 2026-09-14（周一）.. 2026-09-18（周五）：一个完整交易周，够摆"窗口中段披露新一期"。
D1, D2, D3, D4, D5 = (date(2026, 9, day) for day in range(14, 19))
WEEK = (D1, D2, D3, D4, D5)
#: 窗口收盘一律 100.0：日K那节的格子好认（这张表没有 close 列）。
CLOSE = 100.0

FIELDS = ("roe_waa", "tr_yoy", "netprofit_yoy", "fina_period", "fina_asof")


def bars() -> list[object]:
    return [bar(day, CLOSE) for day in WEEK]


def report(
    ann_date: date,
    *,
    end_date: date = date(2026, 3, 31),
    roe: float = 10.57,
    tr_yoy: float = 6.34,
    netprofit_yoy: float = 1.47,
) -> FinaIndicatorRow:
    """一行财报指标。默认值是 600519 2026Q1 的实测量纲——期望值不用换算。"""
    return FinaIndicatorRow("rds", "600519", ann_date, end_date, roe, tr_yoy, netprofit_yoy)


def _wrong_as_of_backfill(
    reports: list[FinaIndicatorRow], as_of: date, window: tuple[date, ...]
) -> list[FinaTrendRow]:
    """**错误实现（对抗靶）**：取 as_of 当天可见的"最新一期"，回填给窗口每一天。

    这是 03-L4 点名的未来函数错法——把今天才披露的 ROE/增速递给窗口最旧那天。它与正确
    实现共用"挑哪一期"（(end_date, ann_date) 最大者），错的只在"钉给哪几天"：正实现逐日
    点时，它一把钉全窗。任何一条对值/不变量断言都会把它测红——这正是本 fixture 的理由。
    """
    visible = [row for row in reports if row.ann_date <= as_of]
    picked = max(visible, key=lambda row: (row.end_date, row.ann_date))
    return [
        FinaTrendRow(
            day,
            picked.roe_waa,
            picked.tr_yoy,
            picked.netprofit_yoy,
            picked.end_date,
            picked.ann_date,
        )
        for day in window
    ]


# ── 逐日点时：核心不变量与对值 ────────────────────────────────────────────────


def test_each_day_uses_the_report_period_visible_on_that_day_not_the_windows_latest() -> None:
    """窗口中段披露新一期（D3 首披 2026Q2）：D1/D2 用 Q1，D3 起用 Q2。

    fina_period/fina_asof 逐行跟真：前两天印的是旧报告期与它的首披日——把点时可见性印在
    表里（ADR-0022 同手法）。
    """
    reports = [
        report(D1, end_date=date(2026, 3, 31), roe=10.57),
        report(D3, end_date=date(2026, 6, 30), roe=19.20, tr_yoy=15.0, netprofit_yoy=8.0),
    ]
    rows = align_fina_trend(bars(), reports)

    assert [(row.roe_waa, row.fina_period, row.fina_asof) for row in rows] == [
        (10.57, date(2026, 3, 31), D1),
        (10.57, date(2026, 3, 31), D1),
        (19.20, date(2026, 6, 30), D3),  # 披露当天就切换——ann_date ≤ t 是包含关系
        (19.20, date(2026, 6, 30), D3),
        (19.20, date(2026, 6, 30), D3),
    ]


def test_the_as_of_backfill_wrong_implementation_is_catched_by_this_fixture() -> None:
    """对抗测试（03-L4）：植入"用 as_of 那天的最新一期回填全窗"，本 fixture 必须把它测红。"""
    reports = [
        report(D1, end_date=date(2026, 3, 31), roe=10.57),
        report(D3, end_date=date(2026, 6, 30), roe=19.20, tr_yoy=15.0, netprofit_yoy=8.0),
    ]
    correct = align_fina_trend(bars(), reports)
    wrong = _wrong_as_of_backfill(reports, as_of=D5, window=WEEK)

    # 红路一（对值）：正确序列的 D1 是 10.57（当时只有 Q1），回填版是 19.20（今天的泄漏）。
    assert [row.roe_waa for row in correct] == [10.57, 10.57, 19.20, 19.20, 19.20]
    assert [row.roe_waa for row in wrong] == [19.20] * 5
    assert correct != wrong, "fixture 辨别力：两种实现必须给出不同序列，否则测了等于没测"
    # 红路二（不变量）：回填版把 fina_asof=D3 钉在 D1 的行上——fina_asof ≤ 行日期当场破。
    assert wrong[0].fina_asof == D3 > D1, "回填版把 D1 的行钉上 D3 的首披日——不变量当场破"
    for row in correct:
        assert row.fina_asof is None or row.fina_asof <= row.trade_date, (
            f"逐日点时不变量破了：{row.trade_date} 用了 {row.fina_asof} 披露的一期"
        )
        assert row.fina_asof is not None and (
            row.fina_period is None or row.fina_period <= row.fina_asof
        ), "报告期晚于它的首披日——时序不成立（行级锚在入盘时就该拦下）"


# ── 点时可见性：早/晚披露与重述 ──────────────────────────────────────────────


def test_a_report_published_after_t_never_reaches_ts_row() -> None:
    """同一天打开数据，两期一早一晚：早的（ann ≤ t）t 看得到，晚的（ann > t）在 t 的行里
    一个字都不许出现——晚的那期要到它自己的首披日才上岗。"""
    early = report(D2, end_date=date(2026, 3, 31), roe=10.57)
    late = report(D4, end_date=date(2026, 6, 30), roe=19.20, tr_yoy=15.0, netprofit_yoy=8.0)
    rows = align_fina_trend(bars(), [early, late])

    assert [row.roe_waa for row in rows] == [None, 10.57, 10.57, 19.20, 19.20]
    assert rows[0].fina_asof is None and rows[2].fina_asof == D2
    assert rows[3].fina_period == date(2026, 6, 30) and rows[3].fina_asof == D4


def test_a_revision_published_after_t_does_not_rewrite_earlier_rows() -> None:
    """重述（同报告期、晚首披日）：修正从它的首披日当行上岗，更早的行原样不动——
    修正史本身就是点时可见性的证据，两行在盘上并存（主键含 ann_date）。"""
    original = report(D1, end_date=date(2026, 3, 31), roe=10.57)
    revised = report(D3, end_date=date(2026, 3, 31), roe=11.11, tr_yoy=6.50, netprofit_yoy=1.50)
    rows = align_fina_trend(bars(), [original, revised])

    assert [(row.roe_waa, row.fina_asof) for row in rows] == [
        (10.57, D1),
        (10.57, D1),
        (11.11, D3),
        (11.11, D3),
        (11.11, D3),
    ]


def test_an_older_period_disclosed_later_never_displaces_the_newer_one() -> None:
    """更晚披露的**旧期间**（Q1 的补披露）压不过更新的报告期：挑期按 (end_date, ann_date)
    字典序，end 在前——否则一次迟到的旧披露会把表拨回上个季度。"""
    current = report(D3, end_date=date(2026, 6, 30), roe=19.20, tr_yoy=15.0, netprofit_yoy=8.0)
    stale = report(D4, end_date=date(2026, 3, 31), roe=10.57)
    rows = align_fina_trend(bars(), [current, stale])

    # D1/D2 还没有任何披露 → 「—」；D3 起 Q2 上岗；D4 虽有 ann=D4 的 Q1 补披露可见，
    # end 更小 → 不换期（(end, ann) 字典序 end 在前）。
    assert [row.fina_period for row in rows] == [
        None,
        None,
        date(2026, 6, 30),
        date(2026, 6, 30),
        date(2026, 6, 30),
    ]
    assert [row.roe_waa for row in rows[3:]] == [19.20, 19.20], "迟到的旧披露不许拨回上季"


def test_days_before_the_first_disclosure_render_dash_neither_zero_nor_blank() -> None:
    """首份财报披露前的日子：三列全 None 渲染「—」——源没披露 ≠ ROE 为零。"""
    rows = align_fina_trend(bars(), [report(D4, end_date=date(2026, 6, 30), roe=19.20)])
    assert [row.roe_waa for row in rows] == [None, None, None, 19.20, 19.20]
    text = render_fina_trend(rows, fields=FIELDS)
    assert "| — |" in text.splitlines()[4], "披露前的日子是「—」，不是 0"


def test_a_report_disclosed_before_the_window_still_counts_as_visible() -> None:
    """窗口起点前披露的老财报要算进"当时最新"——只读窗口内会把窗口开头读成"无数据"。
    取数侧的对应保证是读首披日全史（prompt._FINA_HISTORY_START），这里判对齐层的语义。"""
    old = report(
        date(2026, 8, 1), end_date=date(2026, 6, 30), roe=19.20, tr_yoy=15.0, netprofit_yoy=8.0
    )
    rows = align_fina_trend(bars(), [old])

    assert all(row.roe_waa == 19.20 for row in rows)
    assert all(row.fina_asof == date(2026, 8, 1) for row in rows)
    assert all(row.fina_asof is not None and row.fina_asof <= row.trade_date for row in rows)


# ── 渲染与标题 ───────────────────────────────────────────────────────────────


def test_render_carries_the_caliber_line_and_the_five_fields() -> None:
    reports = [report(D1, end_date=date(2026, 3, 31), roe=10.57)]
    rows = align_fina_trend(bars()[:2], reports)
    text = render_fina_trend(rows, fields=FIELDS)

    assert "ROE/增速口径" in text and "逐日点时" in text and "首披日" in text
    assert "累计" in text and "未年化" in text, "年内累计口径必须写在表里——Q1 的 ROE 不是全年"
    assert "rds fina_indicator" in text
    assert "| 时间 | ROE(加权,%) | 营业总收入同比(%) | 净利润同比(%) | 报告期 | 发布日 |" in text
    assert "| 2026-09-14 | 10.57 | 6.34 | 1.47 | 2026-03-31 | 2026-09-14 |" in text
    csv_text = render_fina_trend(rows, fields=FIELDS, format="csv")
    assert csv_text.startswith("# ROE/增速口径")
    assert "2026-09-14,10.57,6.34,1.47,2026-03-31,2026-09-14" in csv_text
    assert table.reference_label("fina_trend") in text


def test_render_refuses_to_hand_out_an_empty_skeleton() -> None:
    with pytest.raises(IncompleteComponent, match="一张都没有"):
        render_fina_trend([], fields=FIELDS)


def test_title_of_fina_trend_is_registered_not_invented() -> None:
    assert title_of("fina_trend") == "ROE与增速（逐日点时）"


# ── 组合根接线（读盘现算，ADR-0012 + ADR-0022 同手法） ────────────────────────


def _tmpl(days: int) -> Template:
    return Template(
        name="建仓价分析",
        role="你是资深 A 股基本面分析师",
        task="测算合理建仓区间",
        data=(
            Selection(dataset=layout.DAILY, days=days),
            Selection(dataset="fina_trend", days=days, fields=FIELDS),
        ),
        output="先列假设，再给区间。",
        format="markdown",
        fields=("open", "close"),
        adjust="raw",
        status="ready",
        waiting_on="",
    )


@pytest.fixture
def disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """三天不复权日线（100/100/100）+ 可选 fina_indicator 行的干净区，落在 tmp 数据根里。"""
    base = snapshot_root(tmp_path, monkeypatch)
    store_bars([bar(day, CLOSE) for day in WEEK[:3]], dataset=layout.DAILY, root=base / "data")
    return base / "data"


def _write_reports(root: Path, rows: list[FinaIndicatorRow]) -> None:
    write_table(rows, table="fina_indicator", root=root)


def test_build_computes_the_series_server_side_and_shares_the_daily_window(disk: Path) -> None:
    """服务端现算：提示词里的 ROE/增速与日K同窗同 days，逐格是当时可见报告期的源指标。"""
    _write_reports(
        disk,
        [
            report(D1, end_date=date(2026, 3, 31), roe=10.57),
            report(D3, end_date=date(2026, 6, 30), roe=19.20, tr_yoy=15.0, netprofit_yoy=8.0),
        ],
    )
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    text = build(_tmpl(3), "600519", D3, calendar=calendar, token_warn_above=100_000).text

    assert "### 日K（3 个交易日）" in text
    assert "### ROE与增速（逐日点时）（3 个交易日）" in text, "与日K同 days：盘上几天两边都几天"
    assert "| 2026-09-14 | 10.57 | 6.34 | 1.47 | 2026-03-31 | 2026-09-14 |" in text
    assert "| 2026-09-16 | 19.20 | 15.00 | 8.00 | 2026-06-30 | 2026-09-16 |" in text
    assert "ROE/增速口径" in text


def test_build_a_window_without_any_disclosed_report_gives_the_no_data_note(disk: Path) -> None:
    """窗口里一期都没披露（没回填）→ `NO_DATA_NOTE` 整节交代（ADR-0020），
    不给只有表头的空表。"""
    assert disk.is_dir()  # 夹具只负责把数据根钉进 env；本例断言"没有 fina_indicator 行"那一侧
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    text = build(_tmpl(3), "600519", D3, calendar=calendar, token_warn_above=100_000).text

    assert "### ROE与增速（逐日点时）" in text
    assert NO_DATA_NOTE.splitlines()[0] in text
    section = text.split("### ROE与增速（逐日点时）")[1]
    assert "报告期" not in section, "空段不许给表骨架——空表会被读成那段时间 ROE 一直是零"


def test_build_treats_reports_published_after_the_window_as_no_data(disk: Path) -> None:
    """财报全在窗口之后才首披：窗口内每一天都取不到 → 同样整节交代，不是一张全「—」表。"""
    _write_reports(disk, [report(date(2026, 12, 1), end_date=date(2026, 9, 30), roe=19.20)])
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    text = build(_tmpl(3), "600519", D3, calendar=calendar, token_warn_above=100_000).text

    section = text.split("### ROE与增速（逐日点时）")[1]
    assert NO_DATA_NOTE.splitlines()[0] in section
    assert "19.20" not in section


def test_build_never_leaks_tomorrows_report_into_yesterday(disk: Path) -> None:
    """组合根整条链的点时断言：D3 首披的一期只从 D3 那行起出现，D1 行用不上它。"""
    _write_reports(
        disk,
        [
            report(D1, end_date=date(2026, 3, 31), roe=10.57),
            report(D3, end_date=date(2026, 6, 30), roe=19.20, tr_yoy=15.0, netprofit_yoy=8.0),
        ],
    )
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    text = build(_tmpl(3), "600519", D3, calendar=calendar, token_warn_above=100_000).text

    # ROE/增速表 6 列 7 根竖线；日K表（open+close）只有 3 列——按格数认表，不认日期撞车
    trend_rows = [
        line for line in text.splitlines() if line.startswith("| 2026-") and line.count("|") == 7
    ]
    day1_line = next(line for line in trend_rows if line.startswith("| 2026-09-14 |"))
    day3_line = next(line for line in trend_rows if line.startswith("| 2026-09-16 |"))
    assert "| 10.57 |" in day1_line and "2026-03-31" in day1_line, "D1 只认 D1 那时可见的一期"
    assert "| 19.20 |" in day3_line, "新一期从首披日当天上岗"
    day1_cells = [cell.strip() for cell in day1_line.strip("|").split("|")]
    assert day1_cells[5] == "2026-09-14", "D1 行印的 fina_asof 是它自己那期的首披日"
    assert tokens.estimate(text) > 0


def test_build_reads_reports_disclosed_before_the_window(disk: Path) -> None:
    """取数侧读首披日**全史**（_FINA_HISTORY_START 起）：窗口前披露的一期在窗口开头作数。"""
    _write_reports(
        disk,
        [
            report(
                date(2026, 8, 1),
                end_date=date(2026, 6, 30),
                roe=19.20,
                tr_yoy=15.0,
                netprofit_yoy=8.0,
            )
        ],
    )
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    text = build(_tmpl(3), "600519", D3, calendar=calendar, token_warn_above=100_000).text

    assert "| 2026-09-14 | 19.20 | 15.00 | 8.00 | 2026-06-30 | 2026-08-01 |" in text


def test_token_estimate_still_covers_the_prompt(disk: Path) -> None:
    """加一节 3 行表不顶破阈值口径——tokens.estimate 对成型提示词出正数（06 §八-5 的底座）。"""
    _write_reports(disk, [report(D1, end_date=date(2026, 3, 31), roe=10.57)])
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    prompt = build(_tmpl(3), "600519", D3, calendar=calendar, token_warn_above=100_000)
    assert prompt.tokens > 0 and prompt.warn is None
