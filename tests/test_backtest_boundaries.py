"""回测层的依赖边界（ADR-0010 决定 1）——"这一层是纯的"是 Step 6 全部测试可信的前提。

判的是 import，不是文档里的一句话：`engine.py` 多出一行 `from zhixing_quant.storage import query`，
03-4.1 那三条"跑 3 次逐位相等"就悄悄变成"取决于盘上恰好有什么"，而测试照样全绿、报告照样好看。
所以这里逐文件扫 AST：越界一条报一条，模块清单本身还要对着 ADR 逐字比——决定 1 那句话写了哪七个
文件，`backtest/` 下就该有哪七个。
"""

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKTEST = REPO_ROOT / "src" / "zhixing_quant" / "backtest"
ADR_0010 = REPO_ROOT / "docs" / "adr" / "0010-日内回测引擎形态.md"

#: composition root：`backtest/` 下唯一允许落盘、读主数据、写台账的文件（决定 1）。
ROOT_MODULE = "cli.py"

#: 纯模块往上只允许看见这两层：`domain/*`（纯数据结构与制度表）和 `backtest` 自己。
#: 注意 `config` 与 `quality` 不在列——配置是调用方读好后传进来的（`spec.load(path)`），
#: 让纯模块自己去读文件，等于给它开了一条"默认值从哪来"的暗道。
ALLOWED = frozenset({"backtest", "domain"})

#: 动过盘上或库里的东西的那几个包。出现它们的 import，只许在 `ROOT_MODULE` 里。
IO_PACKAGES = frozenset({"storage", "sources", "tasks"})

PURE_LIST = re.compile(r"`backtest/\{([^}]*)\}\.py`")


def _sources_of(path: Path) -> frozenset[str]:
    """一个文件 import 了哪些 `zhixing_quant` 的下层包。"""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        match node:
            case ast.ImportFrom(module=m, names=names, level=0):
                if m == "zhixing_quant":
                    out.update(a.name for a in names)
                elif m and m.startswith("zhixing_quant."):
                    out.add(m.removeprefix("zhixing_quant.").partition(".")[0])
            case ast.Import(names=names):
                out.update(
                    a.name.removeprefix("zhixing_quant.").partition(".")[0]
                    for a in names
                    if a.name.startswith("zhixing_quant.")
                )
    return frozenset(out)


def _modules() -> list[str]:
    return sorted(p.name for p in BACKTEST.glob("*.py") if p.name != "__init__.py")


def _pure_modules() -> list[str]:
    return [name for name in _modules() if name != ROOT_MODULE]


def _declared_pure_modules() -> list[str]:
    """决定 1 那句 `backtest/{a,b,…}.py` 里列的名字。"""
    text = ADR_0010.read_text(encoding="utf-8")
    match = PURE_LIST.search(text)
    assert match, "ADR-0010 决定 1 的模块清单被改写，正则得跟着改"
    return [f"{name}.py" for name in (x.strip() for x in match.group(1).split(",")) if name]


def test_adr_module_list_matches_the_tree() -> None:
    """文档说什么，目录里就该有什么：纯模块清单逐字对（02 §一 铁律 1）。"""
    assert sorted(_declared_pure_modules()) == sorted(_pure_modules())


@pytest.mark.parametrize("module", _pure_modules())
def test_pure_module_depends_only_on_domain(module: str) -> None:
    outside = _sources_of(BACKTEST / module) - ALLOWED
    assert not outside, f"{module} 越界 import：{sorted(outside)}；纯层只许 {sorted(ALLOWED)}"


def test_the_root_module_is_the_only_io_site() -> None:
    """两个方向都要成立：越界的一个不许有，而落盘这件事确实只发生在 `cli.py` 里。

    后半句防的是"根被改名或挪走"——那时前半句会空转通过（谁都没越界，因为根本没有根），
    而 ADR 里那句"由 composition root 注入"就成了假话。
    """
    io_sites = {name for name in _modules() if _sources_of(BACKTEST / name) & IO_PACKAGES}
    assert io_sites == {ROOT_MODULE}, (
        f"读盘/写库的位置应当只有 {ROOT_MODULE}，实际 {sorted(io_sites)}"
    )
