"""任务流水线的读写层（09 §三~§六）：把状态机校验落到库上。

每个写操作同一顺序：读当前态 → 校验 → 写新态 + 追加事件。事件只增不改
（09 §二），所以非法调用必须"什么都没写"就返回——先校验后动手，不指望事务
回滚兜住状态机漏判。

时间戳由注入的 clock 提供而不是直接 now()：L4 的确定性要求（03）从这一层起就
成立，测试里的事件序才是可断言的。
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from zhixing_quant.tasks import models


class TaskNotFound(KeyError):
    """任务 id 不存在。CLI 转成退出码 3。"""


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    type: str
    step: str
    refs: str
    stage: str
    status: str
    reject_count: int
    summary: str

    @property
    def stage_label(self) -> str:
        return models.stage_label(self.stage)

    @property
    def status_label(self) -> str:
        return models.status_label(self.status)


@dataclass(frozen=True)
class Event:
    id: int
    task_id: int
    at: datetime
    actor: str
    action: str
    from_stage: str
    to_stage: str
    reason: str
    evidence: str


@dataclass(frozen=True)
class Pitfall:
    id: int
    symptom: str
    root_cause: str
    workaround: str
    related_tasks: str
    created_at: datetime


@dataclass(frozen=True)
class Backtest:
    id: int
    task_id: int | None
    strategy: str
    version: str
    params_hash: str
    data_start: str
    data_end: str
    cost_assumption: str
    metrics_in: str
    metrics_out: str
    run_no: int
    report_path: str
    status: str

    @property
    def status_label(self) -> str:
        return models.backtest_status_label(self.status)


_TASK_COLS = "id, title, type, step, refs, stage, status, reject_count, summary"
_BACKTEST_SELECT = (
    "SELECT id, task_id, strategy, version, params_hash, data_start, data_end,"
    " cost_assumption, metrics_in, metrics_out, run_no, report_path, status FROM backtests"
)


def _as_task(row: Sequence[Any]) -> Task:
    return Task(
        id=int(row[0]),
        title=str(row[1]),
        type=str(row[2]),
        step=str(row[3]),
        refs=str(row[4]),
        stage=str(row[5]),
        status=str(row[6]),
        reject_count=int(row[7]),
        summary=str(row[8]),
    )


class Store:
    def __init__(self, con: Any, clock: Callable[[], datetime] = utcnow) -> None:
        self._con = con
        self._clock = clock

    # ---- 基础设施 -------------------------------------------------------

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[Any]:
        return list(self._con.execute(sql, list(params)).fetchall())

    def _insert_returning_id(self, sql: str, params: Sequence[Any]) -> int:
        row: Any = self._con.execute(sql, list(params)).fetchone()
        if row is None:
            raise RuntimeError("INSERT 未返回 id")
        return int(row[0])

    def _event(
        self,
        task_id: int,
        actor: str,
        action: str,
        *,
        at: datetime,
        from_stage: str = "",
        to_stage: str = "",
        reason: str = "",
        evidence: str = "",
    ) -> None:
        self._con.execute(
            "INSERT INTO task_events (task_id, occurred_at, actor, action, from_stage,"
            " to_stage, reason, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [task_id, at, actor, action, from_stage, to_stage, reason, evidence],
        )

    # ---- 任务生命周期（09 §三） -----------------------------------------

    def create_task(
        self,
        title: str,
        type_: str,
        step: str,
        refs: str = "",
        actor: str = "agent",
    ) -> Task:
        if not title.strip():
            raise models.StageError("任务标题不能为空")
        kind = models.parse_task_type(type_)
        who = models.parse_actor(actor)
        at = self._clock()
        task_id = self._insert_returning_id(
            "INSERT INTO tasks (title, type, step, refs, stage, status, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, 'dev', 'open', ?, ?) RETURNING id",
            [title.strip(), kind, step, refs, at, at],
        )
        self._event(task_id, who, "登记", at=at, to_stage="dev", evidence=f"step={step}")
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> Task:
        rows = self._query(f"SELECT {_TASK_COLS} FROM tasks WHERE id = ?", [task_id])
        if not rows:
            raise TaskNotFound(task_id)
        return _as_task(rows[0])

    def board(self, status: str | None = None) -> list[Task]:
        sql = f"SELECT {_TASK_COLS} FROM tasks"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id"
        return [_as_task(r) for r in self._query(sql, params)]

    def advance(
        self,
        task_id: int,
        evidence: str = "",
        na_reason: str = "",
        actor: str = "agent",
    ) -> Task:
        """当前阶段通过 → 进入下一阶段。通过必附证据，不适用必附理由（09 §三）。"""
        task = self.get_task(task_id)
        if task.status == "suspended":
            raise models.StageError("任务已挂起，需用户裁决后 resume 才能继续")
        if task.status == "closed":
            raise models.StageError("任务已结，不能再迁移")
        if bool(evidence.strip()) == bool(na_reason.strip()):
            raise models.StageError("advance 需要且只需要二选一：--evidence 或 --na-reason")
        target = models.next_stage(task.stage)
        if target == "conclude":
            raise models.StageError("功能验收通过请用 conclude 下结论，advance 不产生结论")
        who = models.parse_actor(actor)
        at = self._clock()
        skipped = na_reason.strip() != ""
        action = "阶段不适用" if skipped else "阶段通过"
        self._con.execute(
            "UPDATE tasks SET stage = ?, updated_at = ? WHERE id = ?", [target, at, task_id]
        )
        self._event(
            task_id,
            who,
            action,
            at=at,
            from_stage=task.stage,
            to_stage=target,
            reason=na_reason,
            evidence=evidence,
        )
        return self.get_task(task_id)

    def reject(
        self,
        task_id: int,
        to_stage: str,
        reason: str,
        note: str,
        actor: str = "agent",
    ) -> Task:
        """打回：结构化原因必填，计数 +1，触达阈值自动挂起（09 §四）。"""
        task = self.get_task(task_id)
        if task.status != "open":
            raise models.StageError(f"只有进行中的任务可被打回，当前 {task.status_label}")
        target = models.parse_stage(to_stage)
        models.check_reject_target(task.stage, target)
        code = models.parse_reason(reason)
        if not note.strip():
            raise models.StageError("打回必须写说明（--note）")
        who = models.parse_actor(actor)
        at = self._clock()
        count = task.reject_count + 1
        status = "suspended" if models.should_suspend(count) else task.status
        self._con.execute(
            "UPDATE tasks SET stage = ?, status = ?, reject_count = ?, updated_at = ? WHERE id = ?",
            [target, status, count, at, task_id],
        )
        self._event(
            task_id,
            who,
            "打回",
            at=at,
            from_stage=task.stage,
            to_stage=target,
            reason=f"{models.REJECT_REASONS[code]}：{note.strip()}",
        )
        if status == "suspended":
            self._event(
                task_id,
                who,
                "自动挂起",
                at=at,
                from_stage=target,
                to_stage=target,
                reason=f"打回累计 {count} 次，达阈值 {models.SUSPEND_THRESHOLD}，升级用户裁决",
            )
        return self.get_task(task_id)

    def conclude(self, task_id: int, verdict: str, evidence: str, actor: str = "agent") -> Task:
        """结论：功能验收通过后由 agent 给出并入库，最终确认权在用户（09 §八-4）。"""
        task = self.get_task(task_id)
        if task.stage != "accept" or task.status != "open":
            raise models.StageError(
                f"只有功能验收阶段的进行中任务可下结论，当前 {task.stage_label}"
            )
        if not evidence.strip():
            raise models.StageError("结论必须附验收对照证据（--evidence）")
        code = models.parse_verdict(verdict)
        who = models.parse_actor(actor)
        at = self._clock()
        self._con.execute(
            "UPDATE tasks SET stage = 'conclude', status = 'closed', summary = ?, updated_at = ?"
            " WHERE id = ?",
            [models.VERDICTS[code], at, task_id],
        )
        self._event(
            task_id,
            who,
            "结论",
            at=at,
            from_stage=task.stage,
            to_stage="conclude",
            reason=models.VERDICTS[code],
            evidence=evidence,
        )
        return self.get_task(task_id)

    def resume(self, task_id: int, note: str, actor: str = "user") -> Task:
        """用户裁决继续：解除挂起。打回计数不清零，历史留在事件流里。"""
        return self._decide_suspended(task_id, "继续", note, actor, status="open")

    def abandon(self, task_id: int, note: str, actor: str = "user") -> Task:
        """用户裁决放弃：任务作废关闭。"""
        return self._decide_suspended(task_id, "放弃", note, actor, status="closed")

    def _decide_suspended(
        self, task_id: int, action: str, note: str, actor: str, status: str
    ) -> Task:
        task = self.get_task(task_id)
        if task.status != "suspended":
            raise models.StageError(f"{action} 只适用于挂起中的任务，当前 {task.status_label}")
        if not note.strip():
            raise models.StageError(f"{action} 必须写明裁决理由（--note）")
        who = models.parse_actor(actor)
        at = self._clock()
        self._con.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?", [status, at, task_id]
        )
        self._event(task_id, who, f"用户裁决：{action}", at=at, reason=note.strip())
        return self.get_task(task_id)

    def timeline(self, task_id: int) -> list[Event]:
        self.get_task(task_id)  # 任务不存在时统一报 TaskNotFound
        rows = self._query(
            "SELECT id, task_id, occurred_at, actor, action, from_stage, to_stage, reason,"
            " evidence FROM task_events WHERE task_id = ? ORDER BY occurred_at, id",
            [task_id],
        )
        return [
            Event(
                id=int(r[0]),
                task_id=int(r[1]),
                at=_as_dt(r[2]),
                actor=str(r[3]),
                action=str(r[4]),
                from_stage=str(r[5]),
                to_stage=str(r[6]),
                reason=str(r[7]),
                evidence=str(r[8]),
            )
            for r in rows
        ]

    # ---- 坑表（09 §四） --------------------------------------------------

    def add_pitfall(
        self, symptom: str, root_cause: str, workaround: str, related_tasks: str = ""
    ) -> int:
        if not all(t.strip() for t in (symptom, root_cause, workaround)):
            raise models.StageError("坑表三要素（现象/根因/规避方法）都不能为空")
        return self._insert_returning_id(
            "INSERT INTO pitfalls (symptom, root_cause, workaround, related_tasks, created_at)"
            " VALUES (?, ?, ?, ?, ?) RETURNING id",
            [symptom.strip(), root_cause.strip(), workaround.strip(), related_tasks, self._clock()],
        )

    def search_pitfalls(self, keyword: str = "") -> list[Pitfall]:
        sql = "SELECT id, symptom, root_cause, workaround, related_tasks, created_at FROM pitfalls"
        params: list[Any] = []
        if keyword.strip():
            sql += " WHERE lower(symptom || ' ' || root_cause || ' ' || workaround) LIKE ?"
            params.append(f"%{keyword.strip().lower()}%")
        sql += " ORDER BY id"
        return [
            Pitfall(
                id=int(r[0]),
                symptom=str(r[1]),
                root_cause=str(r[2]),
                workaround=str(r[3]),
                related_tasks=str(r[4]),
                created_at=_as_dt(r[5]),
            )
            for r in self._query(sql, params)
        ]

    # ---- 回测台账（09 §五） ---------------------------------------------

    def add_backtest(
        self,
        strategy: str,
        version: str,
        params_hash: str,
        data_start: str,
        data_end: str,
        cost_assumption: str,
        report_path: str,
        status: str,
        task_id: int | None = None,
        metrics_in: str = "",
        metrics_out: str = "",
    ) -> Backtest:
        if task_id is not None:
            self.get_task(task_id)
        state = models.parse_backtest_status(status)
        if not report_path.strip():
            raise models.StageError("回测必须登记报告路径（09 §五）")
        run_no = 1 + int(
            self._query(
                "SELECT count(*) FROM backtests WHERE strategy = ? AND params_hash = ?",
                [strategy, params_hash],
            )[0][0]
        )
        at = self._clock()
        bid = self._insert_returning_id(
            "INSERT INTO backtests (task_id, strategy, version, params_hash, data_start,"
            " data_end, cost_assumption, metrics_in, metrics_out, run_no, report_path, status,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
            [
                task_id,
                strategy,
                version,
                params_hash,
                data_start,
                data_end,
                cost_assumption,
                metrics_in,
                metrics_out,
                run_no,
                report_path.strip(),
                state,
                at,
            ],
        )
        return self.get_backtest(bid)

    def get_backtest(self, backtest_id: int) -> Backtest:
        rows = self._query(_BACKTEST_SELECT + " WHERE id = ?", [backtest_id])
        if not rows:
            raise TaskNotFound(backtest_id)
        return _as_backtest(rows[0])

    def list_backtests(self, strategy: str | None = None) -> list[Backtest]:
        sql = _BACKTEST_SELECT
        params: list[Any] = []
        if strategy is not None:
            sql += " WHERE strategy = ?"
            params.append(strategy)
        sql += " ORDER BY id"
        return [_as_backtest(r) for r in self._query(sql, params)]


def _as_backtest(row: Sequence[Any]) -> Backtest:
    task_id = None if row[1] is None else int(row[1])
    return Backtest(
        id=int(row[0]),
        task_id=task_id,
        strategy=str(row[2]),
        version=str(row[3]),
        params_hash=str(row[4]),
        data_start=str(row[5]),
        data_end=str(row[6]),
        cost_assumption=str(row[7]),
        metrics_in=str(row[8]),
        metrics_out=str(row[9]),
        run_no=int(row[10]),
        report_path=str(row[11]),
        status=str(row[12]),
    )


def _as_dt(value: Any) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
