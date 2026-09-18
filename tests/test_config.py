"""数据根配置（ADR-0007、02 §六）。"""

from pathlib import Path

from zhixing_quant import config


def test_env_var_overrides_default() -> None:
    root = config.data_root({"ZX_DATA_ROOT": "/tmp/other_root"})
    assert root == Path("/tmp/other_root")


def test_empty_env_falls_back_to_default() -> None:
    """空字符串视同未设置：`ZX_DATA_ROOT=` 会让所有路径变成相对路径，比报错更糟。"""
    assert config.data_root({"ZX_DATA_ROOT": ""}) == Path(config.DEFAULT_DATA_ROOT)
    assert config.data_root({}) == Path(config.DEFAULT_DATA_ROOT)


def test_taskdb_file_layout_follows_adr_0007() -> None:
    path = config.taskdb_file({"ZX_DATA_ROOT": "/tmp/zxdata"})
    assert path == Path("/tmp/zxdata/taskdb/tasks.duckdb")
