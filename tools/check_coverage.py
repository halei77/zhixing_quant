"""核心模块覆盖率门禁（02 §2 DoD-5）：读 pytest 产出的 .coverage.json 判定。

为什么不写成一条 pytest 测试：pytest-cov 在会话结束时才落 json，测试跑动时读到的是
上一轮的数——"文件不在就跳过"的检查等于把门禁交给执行顺序。所以它是 pytest 之后的
独立一步，本机与 CI 显式调用；判定逻辑本身由 tests/test_coverage_gate.py 测穷，
不依赖真实报告文件。

退出码：0 通过，1 有模块不达标（逐条列出是哪个模块、差多少）。
"""

import json
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


def module_thresholds(pyproject: Path) -> dict[str, int]:
    """阈值只认 pyproject 一处（02 §2 DoD-5：正文不复制数字）。"""
    data: Any = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    modules: Any = data.get("tool", {}).get("zhixing", {}).get("coverage", {}).get("modules")
    if not isinstance(modules, dict) or not modules:
        raise KeyError("pyproject 缺 [tool.zhixing.coverage].modules，覆盖率门禁无阈值可判")
    return {str(name): int(value) for name, value in modules.items()}


def module_statements(files: dict[str, Any], module: str) -> tuple[int, int]:
    """该模块的 (语句总数, 未覆盖语句数)；没有代码时为 (0, 0)。"""
    total = 0
    missing = 0
    needle = f"zhixing_quant/{module}/"
    for path, info in files.items():
        if needle not in path.replace("\\", "/"):
            continue
        summary: Any = info.get("summary", {})
        total += int(summary.get("num_statements", 0))
        missing += int(summary.get("missing_lines", 0))
    return total, missing


def evaluate(report: dict[str, Any], thresholds: dict[str, int]) -> list[str]:
    """不达标模块清单，空列表即通过。没有被测代码的模块不判定（Step 还没轮到）。"""
    files: dict[str, Any] = report.get("files", {})
    bad: list[str] = []
    for module, threshold in sorted(thresholds.items()):
        total, missing = module_statements(files, module)
        if total == 0:
            continue
        pct = 100.0 * (total - missing) / total
        if pct < threshold:
            bad.append(f"{module}: {pct:.1f}% < {threshold}%（{missing}/{total} 句未覆盖）")
    return bad


def main(argv: list[str] | None = None) -> int:
    report_path = Path(argv[0]) if argv else REPO_ROOT / ".coverage.json"
    if not report_path.is_file():
        print(f"拒绝：找不到覆盖率报告 {report_path}（先跑 pytest --cov）", file=sys.stderr)
        return 1
    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    bad = evaluate(report, module_thresholds(REPO_ROOT / "pyproject.toml"))
    for line in bad:
        print(f"覆盖率门禁：{line}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
