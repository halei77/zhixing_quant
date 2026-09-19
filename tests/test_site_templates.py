"""模板表装载：形状不对就在装载时响（06 §五、ADR-0011 决定 2）。

这里判的不是"YAML 能不能解析"，而是**一份可疑的配置会不会一路走到生成提示词**。
少一个 `adjust` 就会默认成某个口径，重名会让站点的一次点击对应两条模板，`days: 0` 会渲染出
一张只有表头的表——每一条都能在下游被读成"数据没问题"。所以键少一个、词不在清单里、
名字重复，全部在这里红。

真实那张表（`config/prompt_templates.yaml`）也在场：它是 06 §八-2 那条验收的对象，
"改配置即生效"的前提是它此刻确实装得起来。
"""

from pathlib import Path
from typing import Any

import pytest

from zhixing_quant import config
from zhixing_quant.site import templates
from zhixing_quant.site.templates import TemplateConfigError, parse

#: 一条合法模板的全部必填项。改动它要连带看下面那些"缺一个键"的参数化——它们靠删这里的键工作。
ROW: dict[str, Any] = {
    "name": "短期投资",
    "role": "你是资深 A 股分析师",
    "task": "给结论",
    "data": [{"dataset": "daily", "days": 120}],
    "format": "markdown",
    "fields": ["open", "close"],
    "adjust": "backward",
    "status": "ready",
    "output": "先结论，再依据",
}


def row(**over: Any) -> dict[str, Any]:
    return {**ROW, **over}


def root(*rows: object) -> dict[str, Any]:
    """一条合法模板 + 想改的键。收 `object`：有一条测试专门喂字符串进去，看它报不报"第几条"。"""
    return {"token_warn_above": 100000, "templates": [*(rows or (row(),))]}


def bad(data: dict[str, Any]) -> str:
    """装载这份配置，把抛出的那句话拿回来。不抛就是测试该红。"""
    with pytest.raises(TemplateConfigError) as caught:
        parse(data)
    return str(caught.value)


def test_the_shipped_table_loads() -> None:
    """真实那张表装得起来，且一期两条是 ready（06 §五 的默认组合照抄在配置里）。"""
    cfg = templates.load(config.prompt_templates_file())
    assert cfg.token_warn_above == 100000
    ready = [t.name for t in cfg.templates if t.status == "ready"]
    assert ready == ["短期投资", "波段"]
    assert [(s.dataset, s.days) for s in cfg.by_name("短期投资").data] == [
        ("daily", 120),
        ("minute_60", 60),
        ("minute_30", 30),
        ("minute_5", 10),
    ]


def test_a_missing_file_is_not_an_empty_table(tmp_path: Path) -> None:
    """缺文件要说得出缺的是哪个文件："没有模板"与"没找到模板表"是两句话。"""
    missing = tmp_path / "prompt_templates.yaml"
    with pytest.raises(TemplateConfigError) as caught:
        templates.load(missing)
    message = str(caught.value)
    assert "没有模板" not in message
    assert str(missing) in message


@pytest.mark.parametrize("key", ["token_warn_above", "templates"])
def test_the_root_must_have_both_keys(key: str) -> None:
    assert key in bad({k: v for k, v in root().items() if k != key})


@pytest.mark.parametrize(
    "key",
    ["name", "role", "task", "data", "format", "fields", "adjust", "status", "output"],
)
def test_a_template_missing_any_required_key_names_that_key(key: str) -> None:
    stripped = {k: v for k, v in row().items() if k != key}
    assert key in bad(root(stripped))


@pytest.mark.parametrize("key", ["name", "role", "task", "output"])
@pytest.mark.parametrize("blank", ["", "   ", "\n"], ids=["空串", "一串空格", "只有换行"])
def test_an_empty_field_is_refused_where_a_default_would_have_been_quieter(
    key: str, blank: str
) -> None:
    """空与"只有空白"同级：`role: >` 后面留个空格照样能装载，生成的提示词只是少了一句"你是谁"。"""
    assert key in bad(root(row(**{key: blank})))


@pytest.mark.parametrize(
    "limit",
    [0, -1, "100000", 1.5, True],
    ids=["零", "负", "文字", "小数", "布尔"],
)
def test_the_token_threshold_has_to_be_a_positive_int(limit: Any) -> None:
    """`True` 单列一条：它是 int 的子类，不挡就等于阈值变成 1，每一条提示词都会警告。"""
    assert "token_warn_above" in bad({**root(), "token_warn_above": limit})


@pytest.mark.parametrize("templates_key", ["", "abc", 5, []], ids=["空串", "文字", "整数", "空表"])
def test_templates_has_to_be_a_nonempty_list(templates_key: Any) -> None:
    assert "templates" in bad({**root(), "templates": templates_key})


def test_a_row_that_is_not_a_mapping_is_named_by_index() -> None:
    """报错要指出是第几条：一张 20 条的表里只说"读不出键值对"，人得一条条翻。"""
    assert "第 1 条模板" in bad(root(row(), "不是字典"))


def test_duplicate_names_are_refused() -> None:
    assert "名字重复" in bad(root(row(), row(task="换个任务")))


@pytest.mark.parametrize("format", ["html", "MARKDOWN", ""], ids=["没听过", "大写", "空"])
def test_format_only_accepts_the_two_shapes(format: str) -> None:
    assert "format" in bad(root(row(format=format)))


@pytest.mark.parametrize("adjust", ["noweek", "RAW"], ids=["没听过", "大写"])
def test_adjust_only_accepts_the_three_calibers(adjust: str) -> None:
    assert "adjust" in bad(root(row(adjust=adjust)))


def test_status_only_accepts_ready_and_pending() -> None:
    assert "status" in bad(root(row(status="todo")))


def test_a_pending_template_has_to_say_what_it_waits_for() -> None:
    """pending 而不写 waiting_on，半年后没人知道这条到底在等什么——那就等于永久失踪。"""
    assert "waiting_on" in bad(root(row(status="pending")))


def test_a_ready_template_needs_no_waiting_on() -> None:
    assert templates.parse(root(row())).templates[0].waiting_on == ""


@pytest.mark.parametrize("days", [0, -5], ids=["零天", "负天"])
def test_days_has_to_be_positive(days: int) -> None:
    assert "天数" in bad(root(row(data=[{"dataset": "daily", "days": days}])))


def test_days_is_an_integer_not_a_string() -> None:
    assert "days" in bad(root(row(data=[{"dataset": "daily", "days": "120"}])))


def test_a_selection_needs_its_dataset() -> None:
    assert "dataset" in bad(root(row(data=[{"days": 5}])))


def test_the_same_dataset_twice_is_a_typo_not_a_combination() -> None:
    """两条同周期不是"组合"：渲染时会出两张一样的表头，而没人看得出哪条是笔误。"""
    assert "出现了两次" in bad(
        root(row(data=[{"dataset": "daily", "days": 5}, {"dataset": "daily", "days": 10}]))
    )


def test_an_empty_data_list_is_refused() -> None:
    assert "数据选择" in bad(root(row(data=[])))


def test_data_has_to_be_a_list() -> None:
    assert "data" in bad(root(row(data="daily")))


def test_an_unknown_field_is_listed_with_the_options() -> None:
    text = bad(root(row(fields=["close", "eps"])))
    assert "eps" in text and "turnover" in text


def test_a_duplicated_field_is_refused() -> None:
    assert "重复" in bad(root(row(fields=["close", "close"])))


def test_an_empty_field_list_is_refused() -> None:
    assert "fields" in bad(root(row(fields=[])))


def test_fields_has_to_be_a_list() -> None:
    assert "fields" in bad(root(row(fields="close")))


def test_a_non_text_field_name_is_named() -> None:
    assert "字段名" in bad(root(row(fields=[3])))


def test_a_huge_day_count_is_legal_here_and_is_the_callers_problem() -> None:
    """纯层不知道盘上有几天：`days: 9999` 在 5a 是合法的（ADR-0011 代价三）。

    把深度判断放进来会让模板层依赖 `storage`，那张表就不再能脱离磁盘单测了——而 06 §八-3
    要的对照测试恰恰要求它能。缺口由 5b 用 `query.depth()` 报出来。
    """
    assert (
        templates.parse(root(row(data=[{"dataset": "daily", "days": 9999}])))
        .templates[0]
        .data[0]
        .days
        == 9999
    )


def test_an_unknown_dataset_is_legal_here_because_the_word_list_lives_on_disk() -> None:
    """`min5` 在这一层装得起来（ADR-0011 代价三），而且**应当**装得起来。

    周期清单的真源是 `storage/layout.py` 那四个目录名：纯层去 import 它，等于把 ADR-0011 决定 1
    那道墙拆了；自己再写一份，就是两份迟早漂。所以拼错的周期由 5b 在查盘上覆盖时报告——与
    "盘上有几天"走的是同一条路。这一条钉住的是取舍本身，不是"我们忘了判"。
    """
    cfg = templates.parse(root(row(data=[{"dataset": "min5", "days": 3}])))
    assert cfg.templates[0].data[0].dataset == "min5"


def test_by_name_reports_the_names_it_does_have() -> None:
    cfg = templates.parse(root())
    with pytest.raises(TemplateConfigError) as caught:
        cfg.by_name("波段")
    assert "现有的：短期投资" in str(caught.value)


def test_selections_keep_the_declared_order() -> None:
    """顺序有意义：短期投资那条是"日K 在前、5 分在后"，提示词里的表就按这个次序出现。"""
    cfg = templates.parse(
        root(row(data=[{"dataset": "daily", "days": 5}, {"dataset": "minute_5", "days": 2}]))
    )
    assert [s.dataset for s in cfg.templates[0].data] == ["daily", "minute_5"]
