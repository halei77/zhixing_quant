"""证明每条机器门禁真的会拦人（02 §2 DoD-5 生效时点条款、01 Step 0 验收标准 2）。

同一套门禁"配置存在"和"配置有效"是两件事：上一版项目的教训就是靠人自觉。
每个测试都成对写——先证明坏东西会被测红，再证明同一套命令跑干净东西会过，
否则无法区分"门禁生效"和"命令本身跑挂了"。
"""

import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOL_BIN = Path(sys.executable).parent


def _run(
    args: Sequence[str | Path], cwd: Path, *, check: bool = False
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"命令意外失败：{args}\n{proc.stdout}\n{proc.stderr}")
    return proc


def _tools_exist() -> None:
    missing = [t for t in ("ruff", "mypy", "pytest") if not (TOOL_BIN / t).is_file()]
    assert not missing, f"门禁工具不在虚拟环境里：{missing}（CI 应使用 uv sync 安装 dev 组）"


# --- ruff -----------------------------------------------------------------------


def test_ruff_config_is_loaded_and_selects_beyond_minimal(tmp_path: Path) -> None:
    """同一份坏样本：最小规则集放过、我们的配置测红 → 证明 pyproject 的规则集真的被加载。

    对照组用 --select E9 显式给到最小，而不是 ruff 的 --isolated 默认值——
    0.16 的默认集里已含 I001，拿它当"什么都不管"的基线会得到一个假绿的控制组。
    """
    _tools_exist()
    bad = tmp_path / "bad.py"
    bad.write_text("import os\n\n\nprint(os.path.join('a', 'b'))\n", encoding="utf-8")
    minimal = _run(
        [TOOL_BIN / "ruff", "check", "--isolated", "--select", "E9", str(bad)], cwd=tmp_path
    )
    assert minimal.returncode == 0, f"对照组本身在测红，比较失去意义：{minimal.stdout}"
    ours = _run(
        [TOOL_BIN / "ruff", "check", "--config", REPO_ROOT / "pyproject.toml", str(bad)],
        cwd=tmp_path,
    )
    assert ours.returncode != 0, "我们的 ruff 规则集没拦住 os.path：配置未生效"
    assert "PTH118" in ours.stdout


def test_ruff_passes_on_this_repo() -> None:
    """仓库自身必须干净，否则上面那条证明的只是配置，不是现状。"""
    _tools_exist()
    _run([TOOL_BIN / "ruff", "check", "."], cwd=REPO_ROOT, check=True)


# --- mypy -----------------------------------------------------------------------


def test_mypy_strict_rejects_untyped_and_wrong_annotations(tmp_path: Path) -> None:
    """--strict 必须真的在：未标注函数与返回类型不符都要测红，且干净文件要能过。"""
    _tools_exist()
    dirty = tmp_path / "dirty.py"
    dirty.write_text("def f(x):\n    return x\n", encoding="utf-8")
    wrong = tmp_path / "wrong.py"
    wrong.write_text("def g(x: int) -> str:\n    return x\n", encoding="utf-8")
    clean = tmp_path / "clean.py"
    clean.write_text("def h(x: int) -> int:\n    return x\n", encoding="utf-8")
    for name in ("dirty", "wrong"):
        proc = _run(
            [TOOL_BIN / "mypy", "--strict", "--cache-dir", tmp_path / "mc", f"{name}.py"],
            cwd=tmp_path,
        )
        assert proc.returncode != 0, f"--strict 没拦住 {name}.py，类型门禁是空转的"
    ok = _run(
        [TOOL_BIN / "mypy", "--strict", "--cache-dir", tmp_path / "mc", "clean.py"], cwd=tmp_path
    )
    assert ok.returncode == 0, f"干净文件意外测红，无法区分门禁与故障：{ok.stdout}"


# --- 覆盖率门禁 ------------------------------------------------------------------


def _make_low_coverage_project(root: Path) -> None:
    (root / "littlemod").mkdir()
    (root / "littlemod" / "__init__.py").write_text("", encoding="utf-8")
    (root / "littlemod" / "core.py").write_text(
        "def used() -> int:\n    return 1\n\n\ndef never_called() -> int:\n    return 2\n",
        encoding="utf-8",
    )
    (root / "test_core.py").write_text(
        "from littlemod.core import used\n\n\ndef test_used() -> None:\n    assert used() == 1\n",
        encoding="utf-8",
    )


def test_coverage_gate_bites(tmp_path: Path) -> None:
    """只有"测试全过 + 覆盖率不足"这一种情况才算门禁生效，别的红法都不算。"""
    _tools_exist()
    _make_low_coverage_project(tmp_path)
    base = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--cov=littlemod",
        "--cov-report=",
    ]
    denied = _run([*base, "--cov-fail-under=100"], cwd=tmp_path)
    assert "1 passed" in denied.stdout, f"用例没正常跑完，红得没有说服力：{denied.stdout}"
    assert denied.returncode != 0, "覆盖率 50% 却过了 100% 阈值——覆盖率门禁是空转的"
    allowed = _run([*base, "--cov-fail-under=0"], cwd=tmp_path)
    assert allowed.returncode == 0, f"同一套用例在 0% 阈值下应通过：{allowed.stdout}"


# --- pre-commit 配置 -------------------------------------------------------------


def test_precommit_config_valid_and_wires_the_gates() -> None:
    """钩子配置必须可解析，且真的挂了 ruff 与 mypy。

    "故意提交坏文件被拦"这一条在 Step 0 验收时人工演示并留痕。
    """
    hook = REPO_ROOT / ".pre-commit-config.yaml"
    assert hook.is_file(), "缺 .pre-commit-config.yaml"
    text = hook.read_text(encoding="utf-8")
    assert "ruff" in text and "mypy" in text, "pre-commit 没接 ruff/mypy"
    _run([TOOL_BIN / "pre-commit", "validate-config", str(hook)], cwd=REPO_ROOT, check=True)
