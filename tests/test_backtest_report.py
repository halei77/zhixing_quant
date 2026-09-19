"""报告这一页纸说的话算不算数（ADR-0010 决定 10；03-4.3、03-4.5）。

报告是用户唯一会逐字读的东西，所以这里判的不是"排版好不好看"，而是**它写下的每个断言能不能
被复核**：纸面上那三个钱数相加等不等于总数、六档拒因是不是全列了、算不出来的格子是不是 `—`
而不是 0、成本一上浮就翻期望的假设是不是被明说成"反转"。

一律拿真引擎跑出来的 `Result` 渲染，再对文本做判据：手搓一个 `Result` 只能测到"这段字符串
会印出来"，测不到"印出来的字符串对得上账"。第三节那三个数是从渲染结果里抠出来再加一遍的——
那才是"报告说的恒等式"这句话被机器判过的意思。
"""

import re
from datetime import date, datetime
from pathlib import Path

import pytest

from tests.fakes import Scripted, assumptions, flat_bands, minutes
from zhixing_quant.backtest.engine import EquityPoint, NeverSignal, Result, run
from zhixing_quant.backtest.fills import REASON_NO_BAR, REASONS, Order, Reject
from zhixing_quant.backtest.metrics import Metrics, measure
from zhixing_quant.backtest.position import REASON_T1, RoundTrip
from zhixing_quant.backtest.report import (
    NA,
    Identity,
    Probe,
    Report,
    Scope,
    equity_csv,
    render,
)
from zhixing_quant.backtest.spec import Side

DAY1 = date(2026, 9, 17)
DAY2 = date(2026, 9, 18)
STAMP = (DAY1, datetime(2026, 9, 17, 9, 35))
OUT = Path("data/backtests/20260919-080000")
IDENTITY = Identity(
    strategy="never",
    version="0.0.1",
    params_hash="0123456789ab",
    backtest_id=12,
    run_no=3,
)


def run_of(plan: dict[tuple[str, int], Side] | None = None) -> Result:
    """两天、每天三根、一手 100 股的一小段行情，按剧本下单。`None` 就是全程不动。"""
    bars = minutes(DAY1, [10.0, 10.2, 10.4]) + minutes(DAY2, [10.3, 10.5, 10.7])
    strategy: NeverSignal | Scripted = NeverSignal() if plan is None else Scripted(plan)
    return run(bars, strategy=strategy, assumptions=assumptions(), bands=flat_bands())


def _report(
    result: Result,
    *,
    sensitivity: tuple[Probe, ...] = (),
    disclosures: tuple[str, ...] = (),
) -> Report:
    return Report(
        identity=IDENTITY,
        scope=Scope(
            dataset="minute_5",
            start=DAY1,
            end=DAY2,
            codes=("600519",),
            bars=len(result.equity),
            depth="盘上 5 个月（2026-04-20 → 2026-09-18）",
        ),
        assumptions=result.assumptions,
        metrics=measure(result),
        result=result,
        output_dir=OUT,
        sensitivity=sensitivity,
        disclosures=disclosures,
    )


def _page(
    plan: dict[tuple[str, int], Side] | None = None,
    *,
    sensitivity: tuple[Probe, ...] = (),
    disclosures: tuple[str, ...] = (),
) -> str:
    return render(_report(run_of(plan), sensitivity=sensitivity, disclosures=disclosures))


def _paper_money(text: str, label: str) -> float:
    """从第三节把某个钱数抠回成 float——报告说"相加恒等于总数"，就用纸面上的数判一次。"""
    line = next(line for line in text.splitlines() if label in line)
    return float(line.split(label)[1].split(" 元")[0].replace(",", ""))


def _score_cell(text: str, name: str) -> str:
    line = next(line for line in text.splitlines() if line.startswith(f"| {name} |"))
    return line.split("|")[2].strip()


def _reject_rows(text: str) -> list[tuple[str, int]]:
    found = re.findall(r"^\| `(\w+)` \| (\d+) \|$", text, flags=re.MULTILINE)
    return [(reason, int(count)) for reason, count in found]


def _one_trip(pnl: float) -> Metrics:
    """一份只有一个回合、回合盈亏恰好是 `pnl` 元的成绩单。

    敏感性那一节只读期望的**符号**与回合数，所以这里不跑引擎：跑了反而把三层的可能错混成
    一个对不上的数，而指标算术本身在 `test_backtest_metrics.py` 已经逐笔核过。
    """
    return measure(
        Result(
            assumptions=assumptions(),
            fills=(),
            rejects=(),
            round_trips=(
                RoundTrip(
                    code="600519",
                    buy_at=STAMP,
                    sell_at=STAMP,
                    shares=100,
                    buy_price=10.0,
                    sell_price=10.0 + pnl / 100.0,
                    fee=0.0,
                ),
            ),
            day_events=(),
            equity=(),
            closings=(),
        )
    )


def test_the_header_says_which_run_this_is_not_just_which_strategy() -> None:
    """03-4.5：反复调参只报最好那次，靠的是"第 N 次"这个数被印在每一页上。"""
    text = _page()
    assert "第 3 次回测" in text
    assert "#12" in text
    assert "0123456789ab" in text
    assert "每跑一次计数加一" in text


def test_the_three_money_numbers_on_paper_add_up_to_the_total() -> None:
    """第三节那句"相加恒等于总数"不能只是文案：把纸上的三个数抠出来加一遍。

    容差不是零——每个数各自四舍五入到分，四项最多差二分。要判的是"差一个量级"那种错（漏加
    一项、底仓算重），而容差多留一分是为了不让自己卡在最后一位上。
    """
    text = _page({("600519", 0): "sell", ("600519", 4): "buy"})
    total = _paper_money(text, "账户总盈亏")
    parts = [
        _paper_money(text, "回合已实现"),
        _paper_money(text, "未还原腿浮盈亏"),
        _paper_money(text, "底仓 beta"),
    ]
    assert sum(parts) == pytest.approx(total, abs=0.03)
    assert parts[0] != 0.0 and total != 0.0  # 全是 0 的话上面那句是恒等式而不是判据


def test_a_run_with_no_trips_says_无回合_instead_of_printing_zero_percent() -> None:
    """空仓策略：胜率那一格必须是 `—`，还得有一句话说清"没做"不等于"做了且很差"。"""
    text = _page()
    assert _score_cell(text, "胜率") == NA
    assert _score_cell(text, "盈亏比") == NA
    assert _score_cell(text, "单笔期望（元）") == NA
    assert "无回合" in text
    assert "0.00%" not in text


def test_the_reject_table_lists_all_six_reasons_in_the_judging_order() -> None:
    """补零的活在这里：全零是一句有内容的话，缺档是"没查过"。顺序就是判序。"""
    result = run_of({("600519", 0): "sell", ("600519", 1): "sell"})
    text = render(_report(result))
    assert [reason for reason, _ in _reject_rows(text)] == [REASON_T1, *REASONS]
    counts = dict(_reject_rows(text))
    assert counts[REASON_T1] == 1  # 第二次卖是撞 T+1 那一笔
    assert counts[REASON_NO_BAR] == 0  # 每一笔都有结果，没有等不到下一根的
    assert sum(counts.values()) == len(result.rejects)
    assert f"| 合计 | {len(result.rejects)} |" in text


def test_the_base_position_cost_is_called_an_assumption_on_the_page() -> None:
    """底仓入账价是我们编的（ADR-0010 决定 5），这句话必须印在报告上而不是只写在 ADR 里。"""
    text = _page()
    assert "那是假设不是事实" in text
    assert "第一根K线的开盘价" in text


def test_the_board_prices_come_from_the_same_table_the_gate_uses() -> None:
    """档位与门禁同源（07 §5.2 的"同一张表"），并且明说不叠加 R004 的测量容差。"""
    text = _page()
    assert "config/gate.toml" in text
    assert "不叠加" in text


def test_cost_sensitivity_that_flips_the_sign_of_expectancy_raises_the_flag() -> None:
    """03-4.3：利润如果来自成本假设而不是信号，这一节就是它的红灯，措辞不许软化成"注意"。"""
    probes = (Probe(0.5, _one_trip(+1.0)), Probe(1.0, _one_trip(+1.0)), Probe(1.5, _one_trip(-1.0)))
    text = _page(sensitivity=probes)
    assert "反转" in text
    assert "亮红灯" in text
    assert "人工归因" in text


def test_cost_sensitivity_with_one_sign_reports_同号_rather_than_a_warning() -> None:
    """三档同号：即使期望是负的，"不来自成本假设"这句话照样成立，不该被报成安全。"""
    probes = tuple(Probe(f, _one_trip(-1.0)) for f in (0.5, 1.0, 1.5))
    text = _page(sensitivity=probes)
    assert "同号" in text
    assert "反转" not in text
    assert "×1.5" in text
    assert "老实的负" in text


def test_three_different_signs_are_named_in_the_order_of_the_multipliers() -> None:
    """三档三样：那句话要把每一档是正是负写出来，只说"有反转"等于让人回去重跑一遍。"""
    probes = (Probe(0.5, _one_trip(+1.0)), Probe(1.0, _one_trip(-1.0)), Probe(1.5, _one_trip(-1.0)))
    text = _page(sensitivity=probes)
    assert "×0.5→正、×1.0→负、×1.5→负" in text


def test_sensitivity_without_a_single_trip_is_not_read_as_a_pass() -> None:
    """三档全无回合：这是"没内容可判"，不是"通过"。把这两件事写反是这一页最贵的一句话。"""
    empty = measure(run_of())
    probes = tuple(Probe(f, empty) for f in (0.5, 1.0, 1.5))
    text = _page(sensitivity=probes)
    assert "无从判断" in text
    assert "这不是“通过”" in text


def test_the_optional_sections_stay_out_of_the_page_when_there_is_nothing_to_say() -> None:
    """没跑敏感性、没写披露，就不该留下两个空标题占位——空标题读起来像"查过，没有"。"""
    assert "## 六、" not in _page()
    assert "## 七、" not in _page()
    with_clues = _page(disclosures=("幸存者偏差：区间内退市的票不在票池里（03-4.6）",))
    assert "## 七、这一页不能保证的事" in with_clues
    assert "- 幸存者偏差" in with_clues


def test_open_positions_are_counted_in_hands_and_keep_their_floating_loss() -> None:
    """第五节：未还原的手数与它的浮盈亏。那笔钱**不在**胜率分母里，这句话要印在旁边。"""
    text = _page({("600519", 0): "sell"})  # 卖掉 3 手底仓、没接回 → 三手欠仓
    row = next(line for line in text.splitlines() if line.startswith("| 600519 |"))
    cells = [cell.strip() for cell in row.split("|")[1:-1]]
    assert cells[0] == "600519"
    assert cells[1] == "0"  # 期末持仓：底仓全卖光了
    assert cells[2] == "3 手"  # 欠着 300 股，等接回
    assert cells[3].startswith("-")  # 卖在 10.2、收在 10.7：浮亏如实挂着
    assert "不在胜率的分母里" in text


def test_a_run_that_never_touched_a_book_says_so_rather_than_printing_an_empty_table() -> None:
    """一根K线都没有：第五节不能印一张只有表头的空表，那读起来像"查过，都空着"。"""
    empty = run([], strategy=NeverSignal(), assumptions=assumptions(), bands=flat_bands())
    text = render(_report(empty))
    assert "这一跑没有一只票进过账" in text
    assert "| 票 | 期末持仓（股） |" not in text


def test_the_equity_curve_is_one_row_per_mark_with_the_fields_named() -> None:
    points = (
        EquityPoint(STAMP, "600519", -100.5, 300, 3060.0, 2959.5),
        EquityPoint((DAY2, datetime(2026, 9, 18, 9, 40)), "600519", 0.0, 300, 3150.0, 150.0),
    )
    lines = equity_csv(_with_equity(points)).rstrip("\n").split("\n")
    assert lines[0] == "day,time,code,cash,held,market_value,pnl"
    assert lines[1] == "2026-09-17,09:35,600519,-100.5,300,3060,2959.5"
    assert lines[2] == "2026-09-18,09:40,600519,0,300,3150,150"
    assert len(lines) == 1 + len(points)


def test_a_daily_mark_has_no_clock_to_print_and_does_not_invent_one() -> None:
    """日线的 `ts` 是那把退化的 `datetime.min`：时刻那一格留空，不许写 00:00 冒充有个分钟。"""
    daily = EquityPoint((DAY1, datetime.min), "600519", 0.0, 0, 0.0, 0.0)
    assert equity_csv(_with_equity((daily,))).rstrip("\n").endswith("2026-09-17,,600519,0,0,0,0")


def test_the_report_writes_no_new_numbers_so_two_renders_are_identical() -> None:
    """同一份 `Result` 渲染两遍逐字一样：报告不掺随机数、不掺"现在是几点"，所以它能被 diff。"""
    result = run_of({("600519", 0): "sell", ("600519", 3): "buy"})
    assert render(_report(result)) == render(_report(result))


def _with_equity(points: tuple[EquityPoint, ...]) -> Result:
    """一份只有净值曲线的 `Result`：判 CSV 的字段归属用不着真跑两天的行情。"""
    order = Order(code="600519", side="buy", shares=100, signal_at=STAMP)
    return Result(
        assumptions=assumptions(),
        fills=(),
        rejects=(Reject(order=order, reason=REASON_NO_BAR, detail="区间到头"),),
        round_trips=(),
        day_events=(),
        equity=points,
        closings=(),
    )
