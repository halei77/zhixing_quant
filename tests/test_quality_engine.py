"""门禁引擎（04 §一 架构；ADR-0002 方案 C）。

规则单条逻辑在 tests/test_quality_rules.py，这里测的是**装配**：事实怎么拼、
批级和逐条谁先跑、三种级别各落到哪个桶、以及两道"门禁自己也可能被骗"的自检。

用的都是 `config/gate.toml` 真表（除特别说明），所以这组测试同时也是"配置到引擎"
的接线检查：谓词改名、级别写错、参数漏给，都会在这里炸。
"""

from collections.abc import Callable
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from zhixing_quant import config as app_config
from zhixing_quant.domain.bar import Bar, BarDraft
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Interval, Listing, SecurityMaster
from zhixing_quant.quality import gate_config
from zhixing_quant.quality.engine import GateEngine, GateInconsistency, GateOutcome, QuarantinedRow
from zhixing_quant.quality.facts import BatchFacts, Violation
from zhixing_quant.quality.gate_config import GateConfig, RuleSpec

DAY1, DAY2, DAY3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
CALENDAR = TradingCalendar([DAY1, DAY2, DAY3, date(2024, 1, 5), date(2024, 1, 8)])
MASTER = SecurityMaster(
    [
        Listing("600519", "测试白酒", date(2000, 1, 4)),
        Listing("300750", "测试电池", date(2000, 1, 4)),
    ]
)

CLEAN: dict[str, Any] = {
    "source": "ak",
    "symbol": "600519",
    "trade_date": DAY1,
    "open": 10.0,
    "high": 11.0,
    "low": 9.5,
    "close": 10.5,
    "volume": 1000.0,
    "amount": 10500.0,
    "adj_factor": 1.0,
}


def _draft(**over: Any) -> BarDraft:
    return BarDraft.model_validate({**CLEAN, **over})


@pytest.fixture(scope="module")
def shipped() -> GateConfig:
    return gate_config.load(app_config.gate_config_file())


def _engine(config: GateConfig | None = None) -> GateEngine:
    cfg = config if config is not None else gate_config.load(app_config.gate_config_file())
    return GateEngine(cfg, master=MASTER, calendar=CALENDAR)


def _without(config: GateConfig, *rule_ids: str) -> GateConfig:
    """停用若干条规则，其余照旧：构造"门禁漏了"的形状用。"""
    return replace(
        config,
        rules=tuple(replace(r, enabled=False) if r.id in rule_ids else r for r in config.rules),
    )


# --- 分流 ------------------------------------------------------------------------


def test_clean_batch_reaches_the_clean_zone() -> None:
    outcome = _engine().run([_draft(), _draft(trade_date=DAY2, symbol="300750")])
    assert outcome.total == 2
    assert [b.symbol for b in outcome.clean_zone] == ["600519", "300750"]
    assert all(isinstance(b, Bar) for b in outcome.clean_zone)
    assert outcome.quarantined == ()
    assert outcome.warned == ()
    assert not outcome.has_fatal


def test_source_is_carried_through_for_scoring() -> None:
    """04 §三 是按源打分的：源名丢了，健康分就无处挂。"""
    outcome = _engine().run([_draft(source="xianyu"), _draft(source="xianyu")])
    assert outcome.source == "xianyu"


def test_mixed_source_batch_is_refused() -> None:
    """混批会把闲鱼源的脏算进 akshare 的分（04 §三），所以拒的是调用而不是数据。"""
    with pytest.raises(GateInconsistency, match="一个数据源"):
        _engine().run([_draft(source="ak"), _draft(source="xianyu")])


def test_rejected_row_lands_in_quarantine_with_its_rule_ids() -> None:
    outcome = _engine().run([_draft(high=8.0)])  # OHLC 逆序 → R001
    assert [q.rule_ids for q in outcome.quarantined] == [("R001",)]
    assert outcome.accepted == ()
    assert "high" in outcome.quarantined[0].violations[0].reason


def test_quarantine_keeps_the_original_row_untouched() -> None:
    """ADR-0002 第 2 条：隔离区存原始数据，不删不改不"顺手修一下"。"""
    dirty = _draft(high=8.0)
    outcome = _engine().run([dirty])
    assert outcome.quarantined[0].draft == dirty


def test_a_row_may_be_rejected_by_several_rules_at_once() -> None:
    """一条数据同时越界又逆序，隔离区要留全部理由：只留第一个就没法定位源头。"""
    outcome = _engine().run(
        [_draft(trade_date=DAY1), _draft(trade_date=DAY2, open=10.0, close=20.0, high=19.0)]
    )
    assert "R001" in outcome.quarantined[0].rule_ids
    assert "R004" in outcome.quarantined[0].rule_ids


def test_warned_row_is_both_accepted_and_counted() -> None:
    """04 §二 WARN 的原文是"放行但标记计数"：进了 accepted，也进 warned。"""
    outcome = _engine().run([_draft(volume=0.0, amount=0.0)])
    assert [b.symbol for b in outcome.accepted] == ["600519"]
    assert [q.rule_ids for q in outcome.warned] == [("R003",)]
    assert outcome.reject_rate == 0.0
    assert outcome.warn_rate == 1.0


# --- 批级 FATAL 短路 --------------------------------------------------------------


def test_fatal_batch_rule_rejects_the_whole_batch() -> None:
    """一行缺字段 → 整批不进干净区（04 §二 R010 FATAL"整批拒收"）。"""
    outcome = _engine().run([_draft(), _draft(trade_date=DAY2, close=None)])
    assert outcome.has_fatal
    assert [v.rule_id for v in outcome.fatal] == ["R010"]
    assert outcome.clean_zone == ()
    assert outcome.accepted == ()
    assert outcome.quarantined == ()
    assert outcome.total == 2  # 分母还在：日报要说"这批 2 行整批作废"


def test_row_rules_never_run_after_a_fatal() -> None:
    """短路不是优化是口径：一批"日期都不对"的数据逐条判出来的违规只会把日报刷满噪声。"""
    outcome = _engine().run([_draft(high=8.0), _draft(trade_date=DAY2, close=None)])
    assert [v.rule_id for v in outcome.fatal] == ["R010"]
    assert outcome.quarantined == ()


def test_non_trading_dates_are_fatal_for_the_batch() -> None:
    outcome = _engine().run([_draft(trade_date=date(2024, 1, 6))])  # 周六
    assert [v.rule_id for v in outcome.fatal] == ["R006"]


def test_missing_trading_day_inside_the_window_is_fatal() -> None:
    outcome = _engine().run([_draft(trade_date=DAY1), _draft(trade_date=DAY3)])
    assert "缺 1 个交易日" in outcome.fatal[0].reason


def test_empty_batch_is_fatal_rather_than_a_perfect_score() -> None:
    """一行都没有时不判：04 §三 会给出满分 A，"源一片空白"就成了"今天很干净"。"""
    outcome = _engine().run([])
    assert outcome.has_fatal
    assert outcome.clean_zone == ()
    assert outcome.total == 0


def test_a_batch_can_collect_two_reasons_from_one_rule() -> None:
    """R006 既报"日期不是交易日"又报"缺交易日"：一条规则可以有多个理由。"""
    outcome = _engine().run(
        [_draft(trade_date=DAY1), _draft(trade_date=DAY3), _draft(trade_date=date(2024, 1, 7))]
    )
    assert [v.rule_id for v in outcome.fatal] == ["R006", "R006"]


# --- 事实装配 --------------------------------------------------------------------


def test_duplicate_key_rejects_the_later_arrival() -> None:
    """04 §二 R008"后到的"：输入顺序里第二条才算重复。"""
    outcome = _engine().run([_draft(), _draft(close=10.6)])
    assert [q.rule_ids for q in outcome.quarantined] == [("R008",)]
    assert [b.close for b in outcome.accepted] == [10.5]


def test_previous_close_is_paired_by_date_not_by_position() -> None:
    """倒序喂进来的数据仍要按日期配对：否则"昨收"其实是明天的收盘，R007 判得毫无意义。"""
    early = _draft(trade_date=DAY1, open=10.0, high=10.5, low=9.9, close=10.0)
    late = _draft(trade_date=DAY2, open=12.0, high=12.1, low=11.9, close=12.0)
    for order in ([early, late], [late, early]):
        outcome = _engine().run(order)
        flagged = [q for q in outcome.quarantined if "R007" in q.rule_ids]
        assert [f.draft.trade_date for f in flagged] == [DAY2], order


def test_output_order_follows_input_order() -> None:
    """下游按输入顺序落库；回填昨收时排过序，出口必须还原回去。"""
    rows = [
        _draft(trade_date=DAY3, symbol="300750"),
        _draft(trade_date=DAY1),
        _draft(trade_date=DAY2, symbol="300750"),
    ]
    outcome = _engine().run(rows)
    assert [(b.symbol, b.trade_date) for b in outcome.accepted] == [
        ("300750", DAY3),
        ("600519", DAY1),
        ("300750", DAY2),
    ]


def test_rows_without_master_are_rejected_not_crashed() -> None:
    """主数据没这只票 → 一行都进不了干净区，而且引擎不能替它抛 KeyError。

    严格到"连首日也拒"是有意的：认不出板块就不知道该按哪一档限幅判，Bar 的 symbol
    校验又会把它拒在契约外——两条判据必须同进同退，否则门禁的"放行"没有意义。
    """
    engine = GateEngine(
        gate_config.load(app_config.gate_config_file()),
        master=SecurityMaster([Listing("600519", "测试", date(2000, 1, 4))]),
        calendar=CALENDAR,
    )
    outcome = engine.run(
        [
            _draft(symbol="601318", trade_date=DAY1),
            _draft(symbol="601318", trade_date=DAY2, open=10.0, close=10.1),
        ]
    )
    assert outcome.accepted == ()
    assert [q.rule_ids for q in outcome.quarantined] == [("R004",), ("R004", "R007")]
    assert "无法判定" in outcome.quarantined[0].violations[0].reason


def test_suspended_days_from_master_are_not_ghost_bars() -> None:
    """主数据说那天停牌 → R003 闭嘴。停牌标记来自数据装配，不是逐条 is_suspended 字段。"""
    master = SecurityMaster(
        [Listing("600519", "测试", date(2000, 1, 4))],
        suspensions=[Interval("600519", DAY1, DAY1)],
    )
    engine = GateEngine(gate_config.load(app_config.gate_config_file()), master, CALENDAR)
    outcome = engine.run([_draft(volume=0.0, amount=0.0)])
    assert outcome.warned == ()
    assert [b.volume for b in outcome.accepted] == [0.0]


def test_engine_without_a_master_refuses_instead_of_passing(shipped: GateConfig) -> None:
    """没装主数据时引擎照样能跑完，但每条都要留下"判不了"的把柄。

    这是 Step 2 采集早期的真实形状：日线先到、主数据后补。此时"今天零拒收"会被日报
    读成"数据很干净"，而其实什么都没判（04 引言 fail-closed）。
    """
    engine = GateEngine(shipped, master=None, calendar=CALENDAR)
    outcome = engine.run([_draft(), _draft(trade_date=DAY2, open=10.5, close=10.6)])
    assert outcome.accepted == ()
    assert [q.rule_ids for q in outcome.quarantined] == [("R004",), ("R004", "R007")]
    assert "无法判定" in outcome.quarantined[0].violations[0].reason


def test_row_without_a_date_skips_the_master_lookup(shipped: GateConfig) -> None:
    """没有交易日就没法查"那天它是什么状态"，跳过查询而不是抛 KeyError。

    R010 停用后才看得到这条路径（日线阶段它总是先整批拒收）：一行缺日期的数据炸掉整批，
    等于把它的成本转嫁给同批其它五千行。拒它的理由应该是"判不了涨跌幅"。
    """
    engine = GateEngine(_without(shipped, "R010"), MASTER, CALENDAR)
    outcome = engine.run([_draft(), _draft(trade_date=None)])
    assert [b.trade_date for b in outcome.accepted] == [DAY1]
    assert [q.rule_ids for q in outcome.quarantined] == [("R004",)]


# --- 门禁与干净区必须同口径 ---------------------------------------------------------


def test_gate_passing_but_bar_rejecting_is_an_inconsistency(shipped: GateConfig) -> None:
    """关掉 R001 后，逆序数据被门禁放行、被 Bar 拒收：这是门禁的 bug，必须响。"""
    engine = GateEngine(_without(shipped, "R001"), MASTER, CALENDAR)
    with pytest.raises(GateInconsistency, match="干净区拒收"):
        engine.run([_draft(high=8.0)])


def test_disabled_rule_is_not_evaluated(shipped: GateConfig) -> None:
    """R009 日线阶段停用（04 §二）：乱序数据不该因为它进隔离区。"""
    outcome = _engine().run(
        [
            _draft(trade_date=DAY3, open=10.0, high=10.5, low=9.9, close=10.0),
            _draft(trade_date=DAY1, open=10.0, high=10.5, low=9.9, close=10.0),
        ]
    )
    assert all("R009" not in q.rule_ids for q in outcome.quarantined)
    assert "R009" not in shipped.enabled_ids


def test_clean_zone_follows_the_fatal_flag() -> None:
    """04 §一 关键不变量的另一半：FATAL 那天策略层拿到的"干净区"必须是空集，
    哪怕引擎逐条判出来的 accepted 非空——整批拒收不能只拒一半。"""
    outcome = _engine().run([_draft(), _draft(trade_date=DAY2)])
    assert outcome.fatal == ()
    assert outcome.clean_zone == outcome.accepted != ()
    blocked = replace(outcome, fatal=(Violation("R006", "fatal", "整批日期不对"),))
    assert blocked.clean_zone == ()
    assert blocked.accepted != ()


# --- 批级谓词的返回形状 -------------------------------------------------------------


def _batch_only(predicate: Callable[[Any, Any], object]) -> GateConfig:
    """只装一条批级 FATAL 规则的临时配置：把 R006/R010 换成被测谓词，专看归一化。"""
    return GateConfig(
        defaults={"tolerance_pct": 0.5},
        rules=(
            RuleSpec(
                id="RX01",
                kind="batch",
                level="fatal",
                callable_path="tests.test_quality_engine:predicate",
                predicate=predicate,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("returned", "reasons"),
    [
        (None, []),
        (("整批日期不对",), ["整批日期不对"]),
        ("整批日期不对", ["整批日期不对"]),
        (("缺 1 个交易日", "", None), ["缺 1 个交易日"]),
        (True, ["True"]),
    ],
    ids=["none", "tuple", "bare-string", "blanks-dropped", "truthy-junk"],
)
def test_batch_predicate_return_shapes_become_violations(
    returned: object, reasons: list[str]
) -> None:
    """批级谓词是配置按名字装载的，返回什么形状引擎无法静态知道，只能归一。

    裸字符串那条是重点：`str` 同时也是 `Sequence[str]`，按序列展开会把一句
    "整批日期不对"拆成 6 条单字违规——日报读起来像"当天 6 个问题"，实际只有 1 个。
    写成 `return True` 这类真值垃圾必须留痕，当成"没问题"放行是最坏的失效。
    """

    def predicate(_facts: BatchFacts, _params: Any) -> object:
        return returned

    outcome = GateEngine(_batch_only(predicate), MASTER, CALENDAR).run([_draft()])
    assert [(v.rule_id, v.level, v.reason) for v in outcome.fatal] == [
        ("RX01", "fatal", r) for r in reasons
    ]
    assert outcome.has_fatal is bool(reasons)
    assert (outcome.clean_zone == ()) is bool(reasons)


# --- for_day：批次跨两日、报告只说一天 ------------------------------------------


def _bar(day: date) -> Bar:
    return Bar(
        source="ak",
        symbol="600519",
        trade_date=day,
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=100.0,
        amount=100.0,
    )


def _dirty(day: date) -> QuarantinedRow:
    draft = BarDraft(symbol="600519", trade_date=day, close=1.0)
    return QuarantinedRow(draft, (Violation("R001", "reject", "脏"),))


def test_for_day_recomputes_the_denominator_from_the_rows_it_keeps() -> None:
    """04 §四 的日报说的是"那天"：两日一批时 total 必须是当天的行数，否则分母白多一倍。"""
    batch = GateOutcome(
        source="ak",
        total=2,
        accepted=(_bar(DAY1),),
        quarantined=(_dirty(DAY2),),
        warned=(),
        fatal=(),
    )
    only_second = batch.for_day(DAY2)
    assert (only_second.total, only_second.rejected_count, len(only_second.accepted)) == (1, 1, 0)
    assert batch.for_day(DAY1).total == 1
    assert batch.for_day(DAY3).total == 0  # 那天没数据就报 0，不沿用批次规模
