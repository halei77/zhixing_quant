"""全局配置：数据根与由它派生的路径（ADR-0007）。

本文件是全仓唯一允许出现绝对路径字面量的地方——"路径只能从配置读"这句话要成立，
默认值总得写死在某一个文件里。tests/test_path_hygiene.py 把这里登记为唯一豁免点：
第二处硬编码路径出现时，要么改成从这里读，要么就是拆门禁。
"""

import os
from collections.abc import Mapping
from pathlib import Path

ENV_VAR = "ZX_DATA_ROOT"
DEFAULT_DATA_ROOT = "/home/lei/zhixing_data"


def data_root(env: Mapping[str, str] | None = None) -> Path:
    """数据根目录。环境变量优先，未设置时回落到默认值。"""
    raw = (os.environ if env is None else env).get(ENV_VAR) or DEFAULT_DATA_ROOT
    return Path(raw).expanduser()


def taskdb_file(env: Mapping[str, str] | None = None) -> Path:
    """任务流水线库文件（09 §二：存 ${ZX_DATA_ROOT}/taskdb/，零运维单文件）。"""
    return data_root(env) / "taskdb" / "tasks.duckdb"
