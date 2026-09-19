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


def golden_dir(env: Mapping[str, str] | None = None) -> Path:
    """真实响应快照的落点（tools/capture_golden.py）。

    在数据根而不在仓库：快照是"外部服务器那天给了什么"，属于数据；批准之后才作为测试
    资产进 `tests/golden/`（05 Q1-③）。两处隔开，才分得清"抓下来的"和"认过的"。
    """
    return data_root(env) / "golden"


def reports_dir(env: Mapping[str, str] | None = None) -> Path:
    """质量日报归档目录（04 §四、ADR-0007 的 `reports/`）。"""
    return data_root(env) / "reports"


def repo_root() -> Path:
    """仓库根，由本文件位置反推：写死绝对路径会换机即废，也过不了路径扫描。"""
    return Path(__file__).resolve().parents[2]


def gate_config_file(root: Path | None = None) -> Path:
    """门禁规则表（04 §二）。

    放仓库不放数据根：阈值改动必须出现在 git diff 里，事后才追得清某天的大面积拒收
    是谁改的口径；躺在数据根里的配置文件没有版本，等于口径随时可变。
    """
    return (root if root is not None else repo_root()) / "config" / "gate.toml"
