"""状态机纯逻辑（09 §三、§四）。阶段空间只有 5×5，参数化穷举比属性测试更彻底。"""

from collections.abc import Callable

import pytest

from zhixing_quant.tasks import models


@pytest.mark.parametrize(("code", "label"), models.STAGE_LABELS.items())
def test_stage_accepts_both_code_and_label(code: str, label: str) -> None:
    assert models.parse_stage(code) == code
    assert models.parse_stage(label) == code


@pytest.mark.parametrize("text", ["", "开发阶段", "DEV"])
def test_unknown_stage_rejected(text: str) -> None:
    with pytest.raises(models.StageError, match="未知阶段"):
        models.parse_stage(text)


@pytest.mark.parametrize(
    ("table", "parse"),
    [
        (models.REJECT_REASONS, models.parse_reason),
        (models.VERDICTS, models.parse_verdict),
        (models.ACTOR_LABELS, models.parse_actor),
        (models.TASK_TYPE_LABELS, models.parse_task_type),
        (models.BACKTEST_STATUS_LABELS, models.parse_backtest_status),
    ],
)
def test_every_enum_round_trips_on_code_and_label(
    table: dict[str, str], parse: Callable[[str], str]
) -> None:
    for code, label in table.items():
        assert parse(code) == code
        assert parse(label) == code
    with pytest.raises(models.StageError):
        parse("不存在的取值")


def test_stage_order_is_the_pipeline_in_09() -> None:
    """09 §三 的五个阶段，顺序不许动：动了等于改了生命周期却没改契约。"""
    assert models.STAGES == ("dev", "test", "data_verify", "accept", "conclude")
    assert [models.stage_label(s) for s in models.STAGES] == [
        "开发",
        "测试",
        "数据验证",
        "功能验收",
        "结论",
    ]


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("dev", "test"),
        ("test", "data_verify"),
        ("data_verify", "accept"),
        ("accept", "conclude"),
    ],
)
def test_advance_goes_to_immediate_successor(current: str, expected: str) -> None:
    assert models.next_stage(current) == expected


def test_advance_from_conclude_is_rejected() -> None:
    with pytest.raises(models.StageError, match="最后阶段"):
        models.next_stage("conclude")


def test_next_stage_on_unknown_stage() -> None:
    with pytest.raises(models.StageError, match="未知阶段"):
        models.next_stage("nope")


@pytest.mark.parametrize("current", models.STAGES)
@pytest.mark.parametrize("target", models.STAGES)
def test_reject_only_goes_backwards(current: str, target: str) -> None:
    earlier = models.STAGES.index(target) < models.STAGES.index(current)
    if earlier:
        models.check_reject_target(current, target)
    else:
        with pytest.raises(models.StageError, match="更早的阶段"):
            models.check_reject_target(current, target)


def test_reject_target_outside_enum_rejected() -> None:
    with pytest.raises(models.StageError, match="非法"):
        models.check_reject_target("test", "suspended")


@pytest.mark.parametrize(("count", "expected"), [(0, False), (2, False), (3, True), (7, True)])
def test_suspend_threshold(count: int, expected: bool) -> None:
    assert models.should_suspend(count) is expected
