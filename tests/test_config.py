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


def test_golden_snapshots_land_in_the_data_root_not_in_the_repo(tmp_path: Path) -> None:
    """样本先落数据根、批准后才进仓（05 Q1-③）：这个位置本身就是口径，不钉住就会被改。"""
    assert config.golden_dir({"ZX_DATA_ROOT": str(tmp_path)}) == tmp_path / "golden"


def test_reports_are_archived_under_the_data_root(tmp_path: Path) -> None:
    """日报路径由 04 §四 钉死为 `reports/YYYY-MM-DD.md`，这里只钉它的根。"""
    assert config.reports_dir({"ZX_DATA_ROOT": str(tmp_path)}) == tmp_path / "reports"


def test_gate_rules_are_read_from_the_repo() -> None:
    """门禁表在仓库而不在数据根：阈值改动必须出现在 git diff 里，事后才追得清谁改的口径。"""
    assert config.gate_config_file() == config.repo_root() / "config" / "gate.toml"
    assert config.gate_config_file().is_file()
