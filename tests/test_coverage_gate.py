"""核心模块覆盖率门禁 tools/check_coverage.py（02 §2 DoD-5）。

判定逻辑不吃真实 .coverage.json：报告形状在这里手工构造，才能同时验证"不达标会
测红"和"没代码的模块不判定"。真实报告由 CI 的下一步消费，本文件保证的是它的
判定函数没错。
"""

import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import check_coverage as gate  # noqa: E402  # tools/ 不在包内，按脚本路径导入

THRESHOLDS = {"tasks": 90, "quality": 90}


def _report(**files: tuple[int, int]) -> dict[str, Any]:
    return {
        "files": {
            f"src/zhixing_quant/{name}/store.py": {
                "summary": {"num_statements": total, "missing_lines": missing}
            }
            for name, (total, missing) in files.items()
        }
    }


def test_thresholds_come_from_pyproject() -> None:
    """单一来源：脚本读的必须就是正文引用的那张表（02 §2 DoD-5）。"""
    thresholds = gate.module_thresholds(REPO_ROOT / "pyproject.toml")
    assert thresholds == {"tasks": 90, "quality": 90, "backtest": 90, "strategy": 90}


def test_pyproject_thresholds_match_doc_core_modules() -> None:
    """02 §2 点名的核心模块与 pyproject 的表必须一致，否则口径漂移无人知晓。"""
    doc = (REPO_ROOT / "docs" / "02-工程规范.md").read_text(encoding="utf-8")
    line = next(ln for ln in doc.splitlines() if "覆盖率门禁通过" in ln)
    named = {m for m in ("quality", "backtest", "strategy", "tasks") if m in line}
    assert named == set(gate.module_thresholds(REPO_ROOT / "pyproject.toml"))


def test_missing_threshold_table_is_an_error_not_a_pass() -> None:
    bad = Path(__file__).parent / "_no_thresholds.pyproject.toml"
    bad.write_text("[tool.zhixing.coverage]\nmodules = {}\n", encoding="utf-8")
    try:
        with pytest.raises(KeyError, match="无阈值可判"):
            gate.module_thresholds(bad)
    finally:
        bad.unlink()


def test_module_below_threshold_is_reported() -> None:
    report = _report(tasks=(100, 20), quality=(50, 0))
    bad = gate.evaluate(report, THRESHOLDS)
    assert bad == ["tasks: 80.0% < 90%（20/100 句未覆盖）"]


def test_module_at_threshold_passes() -> None:
    assert gate.evaluate(_report(tasks=(100, 10)), THRESHOLDS) == []


def test_module_without_code_is_skipped_not_failed() -> None:
    """Step 还没轮到的模块不参与判定，否则 Step 0 永远绿不了（02 §2 生效时点）。"""
    assert gate.evaluate(_report(quality=(0, 0)), THRESHOLDS) == []
    assert gate.evaluate(_report(), THRESHOLDS) == []


def test_backslashes_in_paths_do_not_hide_a_module() -> None:
    report: dict[str, Any] = {
        "files": {
            "src\\zhixing_quant\\tasks\\store.py": {
                "summary": {"num_statements": 10, "missing_lines": 10}
            }
        }
    }
    assert len(gate.evaluate(report, THRESHOLDS)) == 1


def test_missing_report_file_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gate.main([str(tmp_path / "absent.json")]) == 1
    assert "找不到覆盖率报告" in capsys.readouterr().err
