"""门禁规则表的装载与自检（04 §二；ADR-0002 第 1 条）。

这里测的两类东西分得很清：
- **表与契约一致**：装载 `config/gate.toml` 后，编号/级别/阈值必须与 04 §二 逐字对上。
  配置漂移是这个项目最容易死的方式——引擎全绿、规则其实被改松了。
- **装载器会咬人**（02 §二 DoD-5）：编号重复、level 拼错、callable 指向不存在的东西、
  把 FATAL 规则全关掉，都必须当场抛错，而不是带着错的表跑到第 3000 行。
"""

from pathlib import Path
from typing import Any

import pytest

from zhixing_quant import config as app_config
from zhixing_quant.quality import gate_config
from zhixing_quant.quality.gate_config import GateConfig, GateConfigError

RULE_MODULE = "zhixing_quant.quality.rules:"

#: 04 §二 表格的原文转录。改了这张表 = 改 04，两处必须一起改。
SPEC_04: tuple[tuple[str, str, str], ...] = (
    ("R001", "row", "reject"),
    ("R002", "row", "reject"),
    ("R003", "row", "warn"),
    ("R004", "row", "reject"),
    ("R005", "row", "warn"),
    ("R006", "batch", "fatal"),
    ("R007", "row", "reject"),
    ("R008", "row", "reject"),
    ("R009", "row", "reject"),
    ("R010", "batch", "fatal"),
)


def _rule(
    rid: str = "R001", kind: str = "row", level: str = "reject", **raw: Any
) -> dict[str, Any]:
    return {
        "id": rid,
        "kind": kind,
        "level": level,
        "callable": f"{RULE_MODULE}ohlc_legal",
        **raw,
    }


def _fatal(rid: str = "R010") -> dict[str, Any]:
    """parse 要求表里至少有一条**启用的** FATAL 规则（04 §二 R006/R010 是整批级），
    所以"合法表"的样本都得带一条垫底的，否则测到的是"缺 FATAL"而不是本意。
    """
    return {
        "id": rid,
        "kind": "batch",
        "level": "fatal",
        "callable": f"{RULE_MODULE}field_completeness",
    }


def _data(*rules: dict[str, Any], bare: bool = False, **defaults: Any) -> dict[str, Any]:
    return {
        "defaults": {"tolerance_pct": 0.5, **defaults},
        "rule": [dict(r) for r in rules] if bare else [_fatal(), *(dict(r) for r in rules)],
    }


@pytest.fixture(scope="module")
def shipped() -> GateConfig:
    """真实发布的规则表：所有"表对不对"的断言都打在它身上。"""
    return gate_config.load(app_config.gate_config_file())


# --- 装载器 ----------------------------------------------------------------------


def test_loads_shipped_table_without_error(shipped: GateConfig) -> None:
    assert [r.id for r in shipped.rules] == [rid for rid, _, _ in SPEC_04]


def test_every_rule_is_wired_to_a_real_predicate(shipped: GateConfig) -> None:
    for spec in shipped.rules:
        assert callable(spec.predicate), spec
        assert spec.callable_path.startswith(RULE_MODULE), spec


def test_rule_ids_kinds_and_levels_match_doc_04(shipped: GateConfig) -> None:
    assert [(r.id, r.kind, r.level) for r in shipped.rules] == list(SPEC_04)


def test_r009_is_installed_but_disabled_for_daily_stage(shipped: GateConfig) -> None:
    """04 §二 R009 括号里写着"日线阶段暂不启用"。停用而不是删除：删了就没人知道它存在过。"""
    r009 = shipped.rule("R009")
    assert not r009.enabled
    assert "R009" not in shipped.enabled_ids
    assert callable(r009.predicate)


def test_defaults_are_merged_into_every_rule(shipped: GateConfig) -> None:
    """板块阈值放 defaults 是为了 R004/R007 联动（04 §二）；不合并就是两处各写一份。"""
    for spec in shipped.rules:
        assert spec.params["tolerance_pct"] == shipped.defaults["tolerance_pct"]
        assert "limits_pct" in spec.params, spec.id


def test_rule_section_can_override_a_default() -> None:
    """源级特例的口子（04 §五"阈值是否需源级覆盖"）：覆盖发生在规则段，不是改全局。"""
    cfg = gate_config.parse(
        _data(_rule(params={"tolerance_pct": 1.5, "limits_pct": {"main": 9.0}}))
    )
    assert cfg.rule("R001").params == {"tolerance_pct": 1.5, "limits_pct": {"main": 9.0}}


def test_enabled_rules_filters_by_kind(shipped: GateConfig) -> None:
    assert [r.id for r in shipped.enabled_rules("batch")] == ["R006", "R010"]
    assert [r.id for r in shipped.enabled_rules("row")] == [
        "R001",
        "R002",
        "R003",
        "R004",
        "R005",
        "R007",
        "R008",
    ]


def test_unknown_rule_id_raises_keyerror_not_stopiteration(shipped: GateConfig) -> None:
    """StopIteration 冒到调用方会变成"查不到值"，看不出是编号写错。"""
    with pytest.raises(KeyError, match="R999"):
        shipped.rule("R999")


def _toml_rule(raw: dict[str, Any]) -> str:
    """把测试里用的规则 dict 落成 TOML 段。只处理字符串值——本文件只需要这一种。"""
    body = "".join(f'{key} = "{value}"\n' for key, value in raw.items())
    return f"[[rule]]\n{body}"


def test_load_reads_toml_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "gate.toml"
    text = "[defaults]\ntolerance_pct = 0.5\n\n" + _toml_rule(_fatal()) + _toml_rule(_rule())
    path.write_text(text, encoding="utf-8")
    cfg = gate_config.load(path)
    assert cfg.enabled_ids == ("R010", "R001")
    assert cfg.rule("R001").predicate.__name__ == "ohlc_legal"


def test_missing_file_is_an_error_not_an_empty_gate(tmp_path: Path) -> None:
    """文件不在就直接抛。返回空表等于"门禁存在但一条不判"，是最坏的兜底。"""
    with pytest.raises(GateConfigError, match="不存在"):
        gate_config.load(tmp_path / "nope.toml")


# --- 阈值：04 §二 的原文数字 -------------------------------------------------------


def test_board_limits_match_doc_04(shipped: GateConfig) -> None:
    """非ST主板 10、ST 5、创业板/科创板 20、北交所 30（04 §二 R004 列）。"""
    assert shipped.defaults["limits_pct"] == {
        "main": 10.0,
        "st": 5.0,
        "gem": 20.0,
        "star": 20.0,
        "bse": 30.0,
    }
    assert shipped.defaults["tolerance_pct"] == 0.5


def test_r007_threshold_is_linked_to_board_limits_not_a_flat_number(shipped: GateConfig) -> None:
    """04 §二：R007 阈值与 R004 板块阈值联动。flat 11% 会把创业板 20% 的合法跳空整批拒收。"""
    params = shipped.rule("R007").params
    assert "limits_pct" in params, "没联动就是各写一份，迟早漂"
    main = params["limits_pct"]["main"] + params["tolerance_pct"] + params["gap_extra_pct"]
    gem = params["limits_pct"]["gem"] + params["tolerance_pct"] + params["gap_extra_pct"]
    assert main == pytest.approx(11.0)  # 04 §二 写的 "偏差 > 11%"
    assert gem > 20.0


def test_new_listing_exemption_days_match_doc_04(shipped: GateConfig) -> None:
    """注册制下主板/创业板/科创板新股前 5 个交易日不设涨跌幅（04 §二 豁免段）。"""
    days = shipped.rule("R004").params["new_listing_no_limit_days"]
    assert {k: days[k] for k in ("main", "gem", "star")} == {"main": 5, "gem": 5, "star": 5}


# --- 装载器会咬人 -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        ({"defaults": {"tolerance_pct": 0.5}, "rule": []}, "一张"),
        ({"rule": [_rule()]}, "defaults"),
        ({"defaults": {}, "rule": [_rule()]}, "defaults"),
        (
            _data(_rule(), _rule()),
            "重复",
        ),
        (_data(_rule(level="REJECT")), "level"),
        (_data(_rule(kind="rowwise")), "kind"),
        (_data({"id": "R001", "kind": "row", "level": "reject"}), "callable"),
        (_data(_rule(callable="no-colon-here")), "模块:函数"),
        (_data(_rule(callable="zhixing_quant.quality.rules:")), "模块:函数"),
        (_data(_rule(callable=":ohlc_legal")), "模块:函数"),
        (_data(_rule(callable="zhixing_quant.nope:x")), "解析失败"),
        (_data(_rule(callable="zhixing_quant.quality.rules:missing_fn")), "解析失败"),
        (_data(_rule(callable="zhixing_quant.config:ENV_VAR")), "不是函数"),
        (
            _data(
                _rule(rid="R006", kind="batch", level="fatal", enabled=False),
                bare=True,
            ),
            "启用的",
        ),
    ],
)
def test_parse_refuses_broken_tables(data: dict[str, Any], needle: str) -> None:
    with pytest.raises(GateConfigError, match=needle):
        gate_config.parse(data)


def test_parse_accepts_the_minimal_legal_table() -> None:
    """一张 FATAL 就够跑：门禁不要求规则数，只要求"整批级拒收"这条底线还在。"""
    assert gate_config.parse(_data()).enabled_ids == ("R010",)


def test_resolve_callable_returns_the_same_function_object() -> None:
    from zhixing_quant.quality import rules

    assert gate_config.resolve_callable(f"{RULE_MODULE}ohlc_legal") is rules.ohlc_legal


@pytest.mark.parametrize("dotted", ["", "a.b", ":x", "mod:", "  :x"])
def test_resolve_callable_refuses_malformed_paths(dotted: str) -> None:
    with pytest.raises(GateConfigError, match="模块:函数"):
        gate_config.resolve_callable(dotted)
