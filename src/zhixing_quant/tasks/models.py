"""任务状态机（09 §三、§四）——纯逻辑，不碰数据库。

阶段只能线性前进、打回只能回退，这两条是 09「跳阶段一律拒绝」的实现位置。
把它和 SQL 分家，是因为这套规则要在没有 DuckDB 的情况下被测穷：状态机漏一条
口子，留痕就全废了，而留痕正是 09 §一 存在的理由。

枚举一律"库内存 ASCII 码、人机交互收/发中文标签"：SQL 里写中文常量既难对齐
也易被终端编码咬一口，而 09 的契约本身是中文写的。
"""

STAGES: tuple[str, ...] = ("dev", "test", "data_verify", "accept", "conclude")
STAGE_LABELS: dict[str, str] = {
    "dev": "开发",
    "test": "测试",
    "data_verify": "数据验证",
    "accept": "功能验收",
    "conclude": "结论",
}
STATUS_LABELS: dict[str, str] = {"open": "进行中", "suspended": "挂起", "closed": "已结"}
REJECT_REASONS: dict[str, str] = {
    "test_insufficient": "测试不充分",
    "data_anomaly": "数据异常",
    "contract_mismatch": "契约不符",
    "impl_defect": "实现缺陷",
    "other": "其他",
}
VERDICTS: dict[str, str] = {"pass": "通过", "fail": "不通过"}
ACTOR_LABELS: dict[str, str] = {"agent": "agent", "user": "用户"}
TASK_TYPE_LABELS: dict[str, str] = {"feat": "feat", "fix": "fix", "docs": "docs", "data": "data"}
BACKTEST_STATUS_LABELS: dict[str, str] = {"pass": "通过", "fail": "失败", "void": "作废"}

# 09 §四：同一任务打回 ≥3 次自动挂起升级用户。阈值可调，改这里即可。
SUSPEND_THRESHOLD = 3


class StageError(ValueError):
    """非法迁移 / 非法枚举值。CLI 捕获后转成退出码 2，不写库。"""


def _resolve(table: dict[str, str], text: str, what: str) -> str:
    if text in table:
        return text
    for code, label in table.items():
        if text == label:
            return code
    choices = "、".join(f"{c}（{label}）" for c, label in table.items())
    raise StageError(f"未知{what}：{text!r}；可选：{choices}")


def parse_stage(text: str) -> str:
    return _resolve(STAGE_LABELS, text, "阶段")


def parse_reason(text: str) -> str:
    return _resolve(REJECT_REASONS, text, "打回原因")


def parse_verdict(text: str) -> str:
    return _resolve(VERDICTS, text, "结论")


def parse_actor(text: str) -> str:
    return _resolve(ACTOR_LABELS, text, "操作者")


def parse_task_type(text: str) -> str:
    return _resolve(TASK_TYPE_LABELS, text, "任务类型")


def parse_backtest_status(text: str) -> str:
    return _resolve(BACKTEST_STATUS_LABELS, text, "回测状态")


def stage_label(code: str) -> str:
    return STAGE_LABELS.get(code, code)


def status_label(code: str) -> str:
    return STATUS_LABELS.get(code, code)


def backtest_status_label(code: str) -> str:
    return BACKTEST_STATUS_LABELS.get(code, code)


def next_stage(current: str) -> str:
    """当前阶段通过后的下一阶段。跳阶段在这里就没有出口。"""
    if current not in STAGES:
        raise StageError(f"未知阶段：{current!r}")
    idx = STAGES.index(current)
    if idx == len(STAGES) - 1:
        raise StageError(f"{stage_label(current)} 已是最后阶段，不能再前进")
    return STAGES[idx + 1]


def check_reject_target(current: str, target: str) -> None:
    """打回只能回到更早的阶段：往前 = 绕过阶段，原地 = 无意义刷计数。"""
    if current not in STAGES or target not in STAGES:
        raise StageError(f"打回目标阶段非法：{target!r}")
    if STAGES.index(target) >= STAGES.index(current):
        raise StageError(
            f"只能打回到更早的阶段：当前 {stage_label(current)}，目标 {stage_label(target)}"
        )


def should_suspend(reject_count: int, threshold: int = SUSPEND_THRESHOLD) -> bool:
    """计数触达阈值即挂起；resume 不清零，所以再次打回会继续挂起（09 §四 升级用户）。"""
    return reject_count >= threshold
