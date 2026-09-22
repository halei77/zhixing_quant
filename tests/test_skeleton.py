"""骨架与文档契约的一致性测试（Step 0b 验收标准 3、铁律 1）。

代码结构必须与 02 §六 的目录约定逐项对齐：文档说什么，仓库里就该有什么，
多一个少一个都测红——不允许"代码先跑起来、文档以后再补"。
"""

import importlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_DOC = REPO_ROOT / "docs" / "02-工程规范.md"


def _subpackages_declared_in_doc() -> list[str]:
    """从 02 §六 的目录树代码块里解析 src/zhixing_quant/ 下的子包名。"""
    lines = SPEC_DOC.read_text(encoding="utf-8").splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("## 六、目录约定"))
    except StopIteration as exc:  # pragma: no cover - 文档标题被改名时触发
        raise AssertionError("02 §六 目录约定小节不存在或标题被改名") from exc
    names: list[str] = []
    inside = False
    for ln in lines[start:]:
        if "src/zhixing_quant/" in ln:
            inside = True
            continue
        if not inside:
            continue
        if "tests/" in ln:
            break
        match = re.search(r"([a-z_]+)/", ln)
        if match:
            names.append(match.group(1))
    return names


def test_doc_declares_nonempty_package_list() -> None:
    """解析器本身要先可信：文档里确实列出了子包，否则下面的测试会空转通过。

    这个数就是"文档树里有几个包"。它跟着包列表走：加一个包（Step 5 的 `backup/`）就 +1，
    而它拦住的是另一种事——解析器失效时会返回空列表，于是下面三条 parametrized 测试全部空转。
    """
    assert len(_subpackages_declared_in_doc()) == 11


@pytest.mark.parametrize("name", _subpackages_declared_in_doc())
def test_declared_subpackage_exists(name: str) -> None:
    init = REPO_ROOT / "src" / "zhixing_quant" / name / "__init__.py"
    assert init.is_file(), f"02 §六 声明了 {name}/ 但仓库里没有：{init}"


def test_no_undeclared_subpackage_exists() -> None:
    """文档没说的包不许存在——防止悄悄扩约定而不改契约。"""
    declared = set(_subpackages_declared_in_doc())
    actual = {
        p.name
        for p in (REPO_ROOT / "src" / "zhixing_quant").iterdir()
        if p.is_dir() and "__pycache__" not in p.name
    }
    assert actual == declared, (
        f"仓库与 02 §六 不一致，多出 {actual - declared}，缺 {declared - actual}"
    )


@pytest.mark.parametrize("name", _subpackages_declared_in_doc())
def test_subpackage_is_importable(name: str) -> None:
    importlib.import_module(f"zhixing_quant.{name}")


def test_tests_mirror_declared_structure() -> None:
    """02 §六：tests/ 与 src 结构镜像，且黄金样本/属性测试目录必须就位（03 L2/L3 的家）。"""
    for d in ("golden", "properties"):
        assert (REPO_ROOT / "tests" / d).is_dir(), f"tests/{d}/ 缺失"
