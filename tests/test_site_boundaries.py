"""依赖边界（ADR-0011 决定 1）：`site/` 的组装层是纯的。

与 `tests/test_backtest_boundaries.py` 同一条理由，而这里的理由更硬：06 §八-3 要的验收是
"生成提示词中的数据与干净区抽样比对 100% 一致，且**自动化对照测试进 CI**"。那种对照测试要能
跑，前提就是"从一堆 Bar 到一段提示词"这一步不需要盘、不需要 HTTP、不需要服务器——一旦有人为了
方便在 `table.py` 里 `from zhixing_quant.storage import query`，比对就变成了"盘上恰好有什么"的
函数，而验收标准照样能打勾。

所以逐文件扫 AST：越界一条报一条。纯模块清单还要对着 ADR-0011 决定 1 那句逐字比——文档写了
哪几个文件，`site/` 下就该有哪几个；5b 加 `api.py` 时，得先改文档再改目录，顺序反了就红。
"""

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE = REPO_ROOT / "src" / "zhixing_quant" / "site"
ADR_0011 = REPO_ROOT / "docs" / "adr" / "0011-提示词组装的口径.md"

#: 纯模块往上只允许看见这两层：`domain/*`（纯数据结构）与 `site` 自己。
#: `config` 不在列：模板表的路径是调用方读好后传进来的（`load(path)`），纯层自己去翻仓库目录，
#: 等于给"这份配置从哪来"开一条暗道（与 ADR-0010 决定 1 同一条取舍）。
ALLOWED = frozenset({"site", "domain"})

#: 动过盘上或网络的那几个包。5a 一个都不许出现在 `site/` 里——包括 `config`（它会读环境变量
#: 与仓库目录）。5b 的 `site/api.py` 落地时，这份清单要跟着 ADR 一起改，两处一起改。
IO_PACKAGES = frozenset({"storage", "sources", "tasks", "config"})

PURE_LIST = re.compile(r"`site/\{([^}]*)\}\.py`")


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
    return sorted(p.name for p in SITE.glob("*.py") if p.name != "__init__.py")


def _declared_pure_modules() -> list[str]:
    """决定 1 那句 `site/{a,b,…}.py` 里列的名字。"""
    text = ADR_0011.read_text(encoding="utf-8")
    match = PURE_LIST.search(text)
    assert match, "ADR-0011 决定 1 的模块清单被改写，正则得跟着改"
    return [f"{name}.py" for name in (x.strip() for x in match.group(1).split(",")) if name]


def test_adr_module_list_matches_the_tree() -> None:
    """文档说什么，目录里就该有什么：纯模块清单逐字对（02 §一 铁律 1）。"""
    assert sorted(_declared_pure_modules()) == sorted(_modules())


@pytest.mark.parametrize("module", _modules())
def test_a_site_module_depends_only_on_the_pure_layers(module: str) -> None:
    outside = _sources_of(SITE / module) - ALLOWED
    assert not outside, f"{module} 越界 import：{sorted(outside)}；纯层只许 {sorted(ALLOWED)}"


def test_nothing_in_site_touches_disk_or_network_yet() -> None:
    """5a 的整层都不该有落盘点：`site/api.py` 出现之前，一个 import IO 的模块都没有。

    写成正向断言（`== set()`）而不是"每个模块各自不含"，是为了让 5b 加根模块时**必须**回来改
    这一行——那时改的是这条测试与 ADR 决定 1 两处，忘了改哪一处都会红，而不是让边界悄悄松掉。
    """
    io_sites = {name for name in _modules() if _sources_of(SITE / name) & IO_PACKAGES}
    assert io_sites == set(), f"5a 不该有读盘/起 HTTP 的模块，实际 {sorted(io_sites)}"
