"""R011 交叉对账：分钟合成日K vs 日线（01 Step 4 验收 2；ADR-0009 决定 6 的实测判据）。

五种坏法各一条，加上"对得上时什么都不报"和几种"不该报"的边界。最要紧的是后两类：一条天天报警
的规则会在两周内被人忽略，而它淹掉的恰恰是那种滑出源窗口、再也补不回来的坏法。

容差是百分数（0.5 = 0.5%），与 `gate.toml` 的 `tolerance_pct` 同源——专门有一条测试钉住单位，
因为把比例当百分数传进去的规则会永远不报警，而"永远全绿"正是这套门禁最不能有的失效方式。
"""

from collections.abc import Sequence
from datetime import date, datetime

from tests.fakes import daily_span, minute_span
from zhixing_quant.domain.bar import Bar
from zhixing_quant.quality.reconcile import KINDS, Finding, counts, reconcile_day

DAY = date(2024, 1, 3)
MORNING = datetime(2024, 1, 3, 9, 35)
TAIL = datetime(2024, 1, 3, 15, 0)
SYMBOL = "600519"
DATASET = "minute_5"
#: 与 `config/gate.toml` 的 `[defaults] tolerance_pct` 同一个数（R004/R007 用的也是它）。
TOL = 0.5


def matching() -> tuple[list[Bar], Bar]:
    """一对对得上的账：价逐分相等，量额差 0.2%（容差之内；全量实测的偏差恒为分钟侧偏少，
    区间 -0.50%~-8.64%，见 04 §二 那张表末尾那句）。

    合成侧：开盘 10.00、收盘 10.10、最高 10.20、最低 9.90、量 500、额 5035。
    日线侧刻意给更宽的高低区间——"分钟极值落在日线区间内"是常态，不是偏差。
    """
    bars = [
        minute_span(MORNING, open_=10.00, high=10.10, low=9.90, close=10.05, volume=300.0),
        minute_span(TAIL, open_=10.05, high=10.20, low=10.00, close=10.10, volume=200.0),
    ]
    day = daily_span(
        DAY, open_=10.00, high=10.30, low=9.85, close=10.10, volume=501.0, amount=5045.0
    )
    return bars, day


def find(
    bars: Sequence[Bar], day: Bar | None, *, tolerance_pct: float = TOL
) -> tuple[Finding, ...]:
    return reconcile_day(SYMBOL, DAY, DATASET, bars, day, tolerance_pct=tolerance_pct)


def test_a_day_that_agrees_reports_nothing() -> None:
    """什么都不报：一条逢报必响的规则，先从这条测试开始红。"""
    bars, day = matching()
    assert find(bars, day) == ()


def test_a_one_cent_price_difference_is_a_finding() -> None:
    """价写死等号，不留容差：两边都是不复权价，同一天的开盘价不可能两个都对（全量实测里不等过
    5 条、最大 0.51 元，那条不等正是这条判据要报出来的东西）。

    给价留容差等于把"有一边的开盘价根本不是那天的开盘价"降级成噪声——那正是 R011 要抓的东西。
    两种坏法分开报：开盘与收盘来自不同字段，一起坏是两处错而不是一处。
    """
    bars, day = matching()
    off_close = day.model_copy(update={"close": 10.09})
    assert [f.kind for f in find(bars, off_close)] == ["price"]
    off_open = day.model_copy(update={"open": 10.01})
    assert [f.kind for f in find(bars, off_open)] == ["price"]
    both = [f.detail for f in find(bars, off_open.model_copy(update={"close": 10.09}))]
    assert len(both) == 2 and "开盘" in both[0] and "收盘" in both[1]


def test_extremes_fire_only_when_minutes_grey_past_the_daily() -> None:
    """只有"分钟越过日线"这个方向算坏。反方向是常态：日内瞬时最高价日线看得见、分钟看不见。

    两个方向各报一条而不是合并：最高价越界与最低价越界的成因不同（前者多半是错抓了别的日子的高价，
    后者是源给的最低价字段缺了），合并成一条"极值不符"等于让人回去自己比那四个数。
    """
    bars, day = matching()  # 合成 high=10.20 low=9.90
    above = day.model_copy(update={"high": 10.15})
    below = day.model_copy(update={"low": 9.95})
    assert [f.kind for f in find(bars, above)] == ["extremes"]
    assert [f.kind for f in find(bars, below)] == ["extremes"]
    both = find(bars, above.model_copy(update={"low": 9.95}))
    assert len(both) == 2 and "最高价" in both[0].detail and "最低价" in both[1].detail


def test_minutes_without_a_daily_line_is_the_other_books_error() -> None:
    """反方向的不对：分钟线抓到一堆、日线那天没有，多半是日线任务那天漏了这只票。"""
    bars, _day = matching()
    found = find(bars, None)
    assert [f.kind for f in found] == ["no_daily"]
    assert "2 根" in found[0].detail


def test_daily_volume_without_minutes_is_the_gap_worth_waking_for() -> None:
    """这一条是整个 R011 存在的首要理由：漏一天还能补，漏到滑出窗口就补不回来了。

    文案里两件事都说：明天重跑救得回来（尾巴整段重落盘），以及救不回来的那个条件。只写后者会把
    一条"今天跑晚了"的常规提醒说成事故。
    """
    _bars, day = matching()
    found = find([], day)
    assert [f.kind for f in found] == ["no_minutes"]
    assert "滑出" in found[0].detail


def test_a_suspended_day_is_not_reported_as_a_gap() -> None:
    """全天停牌的两种写法都不能报：那种日子真的没有分钟行。

    `is_suspended` 是日线源给的标记，实测它并不可靠（有整日零成交却不带标记的），所以"量=0"这条
    也要认。代价是"零成交但源在漏票"会被放过——两害相权，每天几十条假警报会把真断档淹掉。
    """
    _bars, day = matching()
    marked = day.model_copy(update={"is_suspended": True})
    silent = day.model_copy(update={"volume": 0.0, "amount": 0.0})
    assert find([], marked) == ()
    assert find([], silent) == ()


def test_nothing_on_either_side_says_nothing() -> None:
    """两边都空不报断档：那天可能根本没上市、可能池子里就没有它。

    "该不该有数据"是股票池与交易日历的问题（R003 那一层），这一层只回答"两本账对得上吗"。
    """
    assert find([], None) == ()


def test_the_volume_tolerance_is_a_percent() -> None:
    """0.4% 放过、0.6% 报警，且 `tolerance_pct=1.0` 时 0.6% 也放过。

    第三条是单位探针：把比例（0.006）当百分数比的话，这条规则永远不会响，而看起来只是"今天很干净"。
    """
    bars, day = matching()  # 合成 volume=500
    near = day.model_copy(update={"volume": 502.0})
    far = day.model_copy(update={"volume": 497.0})
    assert find(bars, near) == ()
    assert [f.kind for f in find(bars, far)] == ["volume"]
    assert find(bars, far, tolerance_pct=1.0) == ()


def test_volume_and_amount_are_two_separate_judgements() -> None:
    """量与额是两条独立判定，不合并成一条：合了就从"两本账里哪一本对不上"退化成"总之对不上"。"""
    bars, day = matching()
    amount_off = day.model_copy(update={"amount": 5100.0})
    found = find(bars, amount_off)
    assert [f.kind for f in found] == ["volume"]
    assert "成交额" in found[0].detail


def test_a_zero_daily_volume_with_minutes_on_disk_is_an_infinite_gap() -> None:
    """日线说全天零成交、分钟线有 500 股：两本账不可能同时成立，报出来而不是除出 0。

    `_relative` 在 `want=0` 时给 inf 而不是 0——给 0 等于宣称"两边都是零所以一致"，而这里
    分钟侧不是零。
    """
    bars, _day = matching()
    zero = daily_span(DAY, open_=10.00, high=10.30, low=9.85, close=10.10, volume=0.0, amount=0.0)
    found = find(bars, zero)
    assert [f.kind for f in found] == ["volume", "volume"]
    assert "inf" in found[0].detail


def test_counts_keeps_the_kinds_that_are_clean() -> None:
    """五种都列出来，包括 0 条的那几种：只报非零的会让"今天没查"看起来像"全部对上了"。"""
    bars, day = matching()
    found = find([], day) + find(bars, None) + find(bars, day.model_copy(update={"volume": 497.0}))
    assert counts(found) == {
        "no_minutes": 1,
        "no_daily": 1,
        "price": 0,
        "extremes": 0,
        "volume": 1,
    }
    assert counts([]) == dict.fromkeys(KINDS, 0)
