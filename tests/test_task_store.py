"""任务流水线读写层（09 §三~§六）：Step 0d 验收标准 1~4 的主体。

库文件落在 tmp_path 下，顺带验证 db.connect 会自建 taskdb 父目录——数据根不要求
人手准备（ADR-0007）。
"""

import itertools
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from zhixing_quant.tasks import db, models
from zhixing_quant.tasks.store import Backtest, Store, Task, TaskNotFound

BASE = datetime(2026, 9, 18, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    con = db.connect(tmp_path / "taskdb" / "tasks.duckdb")
    assert (tmp_path / "taskdb").is_dir(), "taskdb 目录应由 connect 自建"
    ticks = itertools.count()
    yield Store(con, clock=lambda: BASE + timedelta(minutes=next(ticks)))
    con.close()


def _task(store: Store) -> Task:
    return store.create_task("示例任务", "feat", "0d", "09")


def _to_accept(store: Store, task_id: int) -> None:
    """开发→测试→数据验证→功能验收，走满三段通过。"""
    for i in range(3):
        store.advance(task_id, evidence=f"ci-run-{i}")


def _backtest(store: Store, params_hash: str, **over: Any) -> Backtest:
    args: dict[str, Any] = {
        "strategy": "ma_cross",
        "version": "v1",
        "params_hash": params_hash,
        "data_start": "2020-01-02",
        "data_end": "2024-12-31",
        "cost_assumption": "双边 0.13%",
        "report_path": "backtests/ma_cross.md",
        "status": "失败",
    }
    args.update(over)
    return store.add_backtest(**args)


# ---- 全流程（09 §九-1） --------------------------------------------------


def test_full_lifecycle_records_every_stage(store: Store) -> None:
    task = _task(store)
    assert (task.stage, task.status) == ("dev", "open")
    for expected in ("test", "data_verify", "accept"):
        task = store.advance(task.id, evidence="pytest 全绿")
        assert task.stage == expected
    task = store.conclude(task.id, verdict="通过", evidence="验收对照表见 docs/01")
    assert (task.stage, task.status, task.summary) == ("conclude", "closed", "通过")
    actions = [e.action for e in store.timeline(task.id)]
    assert actions == ["登记", "阶段通过", "阶段通过", "阶段通过", "结论"]
    times = [e.at for e in store.timeline(task.id)]
    assert times == sorted(times), "时间线必须按注入的 clock 单调排列"


def test_stage_can_be_marked_not_applicable_with_reason(store: Store) -> None:
    task = _task(store)
    store.advance(task.id, evidence="单测报告路径")
    store.advance(task.id, evidence="黄金样本通过")
    task = store.advance(task.id, na_reason="纯文档任务，无数据可验证")
    assert task.stage == "accept"
    last = store.timeline(task.id)[-1]
    assert (last.action, last.reason, last.evidence) == (
        "阶段不适用",
        "纯文档任务，无数据可验证",
        "",
    )


def test_board_lists_and_filters_by_status(store: Store) -> None:
    a = _task(store)
    _task(store)
    _to_accept(store, a.id)
    store.conclude(a.id, verdict="通过", evidence="对照表")
    assert len(store.board()) == 2
    assert [t.id for t in store.board("closed")] == [a.id]
    assert len(store.board("open")) == 1


def test_unknown_task_raises(store: Store) -> None:
    with pytest.raises(TaskNotFound):
        store.get_task(999)
    with pytest.raises(TaskNotFound):
        store.timeline(999)


def test_user_decision_requires_a_note(store: Store) -> None:
    task = _task(store)
    for _ in range(3):
        store.advance(task.id, evidence="自称测好了")
        task = store.reject(task.id, "dev", "other", "还是不行")
    assert task.status == "suspended"
    with pytest.raises(models.StageError, match="裁决理由"):
        store.resume(task.id, "  ")


class _NoIdCursor:
    """RETURNING 拿不到 id 的极端情况：宁可抛错，也不许静默写下无主的事件。"""

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []


class _NoIdCon:
    def execute(self, *_args: Any, **_kw: Any) -> _NoIdCursor:
        return _NoIdCursor()


def test_insert_without_returned_id_fails_loudly() -> None:
    with pytest.raises(RuntimeError, match="未返回 id"):
        Store(_NoIdCon()).create_task("x", "feat", "0d")


# ---- 非法迁移（09 §九-2） ------------------------------------------------


def test_create_task_input_validation(store: Store) -> None:
    with pytest.raises(models.StageError, match="标题"):
        store.create_task("   ", "feat", "0d")
    with pytest.raises(models.StageError, match="任务类型"):
        store.create_task("x", "epic", "0d")


@pytest.mark.parametrize(
    ("evidence", "na_reason"),
    [("", ""), ("证据", "理由")],
    ids=["无证据就通过", "证据与理由同时给"],
)
def test_advance_requires_exactly_one_of_evidence_or_na_reason(
    store: Store, evidence: str, na_reason: str
) -> None:
    task = _task(store)
    with pytest.raises(models.StageError, match="二选一"):
        store.advance(task.id, evidence=evidence, na_reason=na_reason)
    assert store.get_task(task.id).stage == "dev", "被拒的调用不许留下半个状态"


def test_cannot_conclude_before_acceptance_stage(store: Store) -> None:
    task = _task(store)
    with pytest.raises(models.StageError, match="功能验收阶段"):
        store.conclude(task.id, verdict="通过", evidence="过早下结论")


def test_conclude_requires_evidence(store: Store) -> None:
    task = _task(store)
    _to_accept(store, task.id)
    with pytest.raises(models.StageError, match="验收对照"):
        store.conclude(task.id, verdict="通过", evidence="  ")


def test_cannot_advance_past_conclude(store: Store) -> None:
    task = _task(store)
    _to_accept(store, task.id)
    with pytest.raises(models.StageError, match="conclude 下结论"):
        store.advance(task.id, evidence="想直接前进到结论")
    done = store.conclude(task.id, verdict="不通过", evidence="对照表")
    with pytest.raises(models.StageError, match="已结"):
        store.advance(done.id, evidence="还想动它")


def test_reject_only_backwards_and_needs_note(store: Store) -> None:
    task = _task(store)
    store.advance(task.id, evidence="单测")
    with pytest.raises(models.StageError, match="更早"):
        store.reject(task.id, "conclude", "other", "想往前打回")
    with pytest.raises(models.StageError, match="更早"):
        store.reject(task.id, "test", "other", "打回当前阶段本身")
    with pytest.raises(models.StageError, match="说明"):
        store.reject(task.id, "dev", "other", "  ")
    with pytest.raises(models.StageError, match="打回原因"):
        store.reject(task.id, "dev", "随便编一个", "理由")
    back = store.reject(task.id, "dev", "测试不充分", "断言太弱")
    assert (back.stage, back.reject_count) == ("dev", 1)
    event = store.timeline(back.id)[-1]
    assert event.reason == "测试不充分：断言太弱"


# ---- 打回与挂起（09 §九-3） ----------------------------------------------


def test_three_rejects_suspend_and_user_decides(store: Store) -> None:
    task = _task(store)
    current = task
    for _ in range(2):
        store.advance(current.id, evidence="自称测好了")
        current = store.reject(current.id, "dev", "impl_defect", "还是不行")
    assert (current.reject_count, current.status) == (2, "open")

    store.advance(current.id, evidence="再来一次")
    suspended = store.reject(current.id, "dev", "数据异常", "第三次")
    assert (suspended.reject_count, suspended.status) == (3, "suspended")
    assert suspended.status_label == "挂起"
    assert [e.action for e in store.timeline(suspended.id)].count("自动挂起") == 1

    with pytest.raises(models.StageError, match="已挂起"):
        store.advance(suspended.id, evidence="绕过挂起继续跑")
    with pytest.raises(models.StageError, match="挂起中的任务"):
        store.resume(_task(store).id, note="对非挂起任务使用")

    resumed = store.resume(suspended.id, note="根因已定位，继续")
    assert (resumed.status, resumed.reject_count) == ("open", 3), "计数不清零"
    store.advance(resumed.id, evidence="补了断言")
    assert store.get_task(resumed.id).stage == "test"

    again = store.reject(store.advance(resumed.id, na_reason="无数据").id, "dev", "other", "又坏")
    assert again.status == "suspended"
    assert store.abandon(again.id, note="拆分重做").status == "closed"


def test_reject_on_closed_task_rejected(store: Store) -> None:
    task = _task(store)
    _to_accept(store, task.id)
    done = store.conclude(task.id, verdict="通过", evidence="对照表")
    with pytest.raises(models.StageError, match="进行中"):
        store.reject(done.id, "dev", "other", "事后打回")


# ---- 坑表（09 §九-3） ----------------------------------------------------


def test_pitfall_registration_and_search(store: Store) -> None:
    pid = store.add_pitfall("CI 绿但本机红", "工具版本两处钉", "钩子走 uv run --frozen", "0d")
    store.add_pitfall("盘符路径散落", "数据根没配置项", "ZX_DATA_ROOT + 扫描测试")
    assert [p.id for p in store.search_pitfalls("版本")] == [pid]
    assert len(store.search_pitfalls()) == 2
    assert store.search_pitfalls("不存在的关键字") == []
    assert store.search_pitfalls("盘符")[0].related_tasks == ""
    with pytest.raises(models.StageError, match="三要素"):
        store.add_pitfall("只有现象", "", "方法")


# ---- 回测台账（09 §九-4） -------------------------------------------------


def test_backtest_run_number_is_per_strategy_and_params(store: Store) -> None:
    task = _task(store)
    first = _backtest(store, "a1", task_id=task.id)
    assert (first.run_no, first.status, first.id) == (1, "fail", 1)
    assert _backtest(store, "a1", task_id=task.id).run_no == 2
    assert _backtest(store, "b2", status="通过").run_no == 1
    assert len(store.list_backtests("ma_cross")) == 3
    assert len(store.list_backtests()) == 3
    assert store.get_backtest(first.id).report_path == "backtests/ma_cross.md"


def test_backtest_input_validation(store: Store) -> None:
    with pytest.raises(models.StageError, match="报告路径"):
        _backtest(store, "h", report_path=" ")
    with pytest.raises(models.StageError, match="回测状态"):
        _backtest(store, "h", status="极好")
    with pytest.raises(TaskNotFound):
        _backtest(store, "h", task_id=4242)
    with pytest.raises(TaskNotFound):
        store.get_backtest(4242)


def test_schema_constraints_hold_the_enums(store: Store) -> None:
    """CHECK 是状态机之外的第二道：绕过 Store 手写 SQL 也插不进未知阶段。"""
    _task(store)
    with pytest.raises(Exception, match=r"Constraint|CHECK"):
        store._con.execute("UPDATE tasks SET stage = 'vibes' WHERE id = 1")
