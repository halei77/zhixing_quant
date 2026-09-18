"""DuckDB 连接与 schema（09 §二、§六）。

零运维是 09 §二 挑 DuckDB 的理由：单文件、跟数据根一起备份，不需要起服务。
0d 只需要 tasks / task_events / pitfalls / backtests 四张表——signals 与
positions 是 Step 8（07 盯盘）的字段，现在建等于凭空猜接口，届随 Step 8 落地。

DDL 全部 IF NOT EXISTS，打开即幂等建表；0d 没有历史数据要迁移，等真有 schema
变更时再引入版本号，不提前造迁移框架。
"""

from pathlib import Path
from typing import Any

# duckdb 带 py.typed 标记但一个 .pyi 都没发，mypy --strict 会把它的每个调用判成
# 未标注函数调用；pyproject 里用 follow_imports="skip" 把整个包当 Any，边界上由
# 本模块收口（对外只暴露 int/str/list 这类已定型的返回值），不把 Any 漏给 store/cli。
import duckdb

SCHEMA: tuple[str, ...] = (
    """
    CREATE SEQUENCE IF NOT EXISTS task_id_seq START 1
    """,
    """
    CREATE SEQUENCE IF NOT EXISTS event_id_seq START 1
    """,
    """
    CREATE SEQUENCE IF NOT EXISTS pitfall_id_seq START 1
    """,
    """
    CREATE SEQUENCE IF NOT EXISTS backtest_id_seq START 1
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id BIGINT PRIMARY KEY DEFAULT nextval('task_id_seq'),
        title VARCHAR NOT NULL,
        type VARCHAR NOT NULL CHECK (type IN ('feat','fix','docs','data')),
        step VARCHAR NOT NULL,
        refs VARCHAR NOT NULL DEFAULT '',
        stage VARCHAR NOT NULL CHECK (stage IN
            ('dev','test','data_verify','accept','conclude')),
        status VARCHAR NOT NULL CHECK (status IN ('open','suspended','closed')),
        reject_count INTEGER NOT NULL DEFAULT 0 CHECK (reject_count >= 0),
        summary VARCHAR NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS task_events (
        id BIGINT PRIMARY KEY DEFAULT nextval('event_id_seq'),
        task_id BIGINT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        actor VARCHAR NOT NULL CHECK (actor IN ('agent','user')),
        action VARCHAR NOT NULL,
        from_stage VARCHAR NOT NULL DEFAULT '',
        to_stage VARCHAR NOT NULL DEFAULT '',
        reason VARCHAR NOT NULL DEFAULT '',
        evidence VARCHAR NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pitfalls (
        id BIGINT PRIMARY KEY DEFAULT nextval('pitfall_id_seq'),
        symptom VARCHAR NOT NULL,
        root_cause VARCHAR NOT NULL,
        workaround VARCHAR NOT NULL,
        related_tasks VARCHAR NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backtests (
        id BIGINT PRIMARY KEY DEFAULT nextval('backtest_id_seq'),
        task_id BIGINT,
        strategy VARCHAR NOT NULL,
        version VARCHAR NOT NULL,
        params_hash VARCHAR NOT NULL,
        data_start VARCHAR NOT NULL,
        data_end VARCHAR NOT NULL,
        cost_assumption VARCHAR NOT NULL,
        metrics_in VARCHAR NOT NULL DEFAULT '',
        metrics_out VARCHAR NOT NULL DEFAULT '',
        run_no INTEGER NOT NULL,
        report_path VARCHAR NOT NULL,
        status VARCHAR NOT NULL CHECK (status IN ('pass','fail','void')),
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
)


def connect(path: Path) -> Any:
    """打开（必要时创建）任务库，并保证 schema 就位。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    for stmt in SCHEMA:
        con.execute(stmt)
    return con
