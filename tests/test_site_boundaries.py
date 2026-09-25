"""依赖边界（ADR-0011 决定 1 + ADR-0012 决定 1 + ADR-0013 决定 1）：组装层是纯的，碰盘只有两处。

与 `tests/test_backtest_boundaries.py` 同一条理由，而这里的理由更硬：06 §八-3 要的验收是
"生成提示词中的数据与干净区抽样比对 100% 一致，且**自动化对照测试进 CI**"。那种对照测试要能
跑，前提就是"从一堆 Bar 到一段提示词"这一步不需要盘、不需要 HTTP、不需要服务器——一旦有人为了
方便在 `table.py` 里 `from zhixing_quant.storage import query`，比对就变成了"盘上恰好有什么"的
函数，而验收标准照样能打勾。

所以逐文件扫 AST：越界一条报一条。纯模块清单还要对着 ADR-0013 决定 1 那句逐字比——文档写了
哪几个文件，`site/` 下就该有哪几个；加模块时得先改文档再改目录，顺序反了就红。

碰盘的位置从 ADR-0012 的一个变成两个（`prompt.py` 读K线，`api.py` 读主数据与日历），于是边界
也从一条变成两条：`storage` 仍然只在 `prompt.py`，而 `sources`/`config` 只许出现在这两个具名
文件里。**新增第三个碰盘位置时必须回来改这里**——这是拆成两个具名位置而不是放开成"谁都能读"
的全部理由。
"""

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SITE = REPO_ROOT / "src" / "zhixing_quant" / "site"
ADR_0013 = REPO_ROOT / "docs" / "adr" / "0013-站点壳的搜索与鉴权.md"

#: 组合根：`site/` 下唯一能读干净区的文件（ADR-0012 决定 1）。
ROOT_MODULE = "prompt.py"

#: 壳：读主数据快照与日历，不 import `storage`（ADR-0013 决定 1）。
SHELL_MODULE = "api.py"

#: 分钟K活取能力层（ADR-0026 决定 1 的分工，用户拍板 2026-09-25）：只许联网——
#: `sources` 给 sina/ngw，`storage` 永远不许 import：复权因子与口径换算留在组合根
#: （`prompt.py`），"全项目只有一处换算式"不因来了个新文件就变两处。
LIVE_MODULE = "live.py"
LIVE_ALLOWED = frozenset({"site", "domain", "sources"})

#: 纯模块往上只允许看见这两层：`domain/*`（纯数据结构）与 `site` 自己。
#: `config` 不在列：模板表的路径是调用方读好后传进来的（`load(path)`），纯层自己去翻仓库目录，
#: 等于给"这份配置从哪来"开一条暗道（与 ADR-0010 决定 1 同一条取舍）。
ALLOWED = frozenset({"site", "domain"})

#: 干净区那一层，只有组合根碰得着。
STORAGE_PACKAGES = frozenset({"storage"})

#: 动过盘上或网络、而壳也需要的那几个包。
SHELL_PACKAGES = frozenset({"sources", "tasks", "config"})

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


def _pure_modules() -> list[str]:
    return [name for name in _modules() if name not in {ROOT_MODULE, SHELL_MODULE, LIVE_MODULE}]


def _declared_pure_modules() -> list[str]:
    """ADR-0013 决定 1 那句 `site/{a,b,…}.py` 里列的名字。"""
    text = ADR_0013.read_text(encoding="utf-8")
    match = PURE_LIST.search(text)
    assert match, "ADR-0013 决定 1 的模块清单被改写，正则得跟着改"
    return [f"{name}.py" for name in (x.strip() for x in match.group(1).split(",")) if name]


def test_adr_module_list_matches_the_tree() -> None:
    """文档说什么，目录里就该有什么：纯模块清单逐字对（02 §一 铁律 1）。"""
    assert sorted(_declared_pure_modules()) == sorted(_pure_modules())


@pytest.mark.parametrize("module", _pure_modules())
def test_a_site_module_depends_only_on_the_pure_layers(module: str) -> None:
    outside = _sources_of(SITE / module) - ALLOWED
    assert not outside, f"{module} 越界 import：{sorted(outside)}；纯层只许 {sorted(ALLOWED)}"


def test_the_clean_zone_is_read_in_exactly_one_place() -> None:
    """`storage` 只出现在组合根：ADR-0012 决定 1 那半句到今天一个字没改。

    判的是"恰好一个"而不是"至多一个"——根被改名或挪走时，前一种说法会空转通过（谁都没越界，
    因为根本没有根），而文档里那句"集中在一个文件"就成了假话。
    """
    readers = {name for name in _modules() if _sources_of(SITE / name) & STORAGE_PACKAGES}
    assert readers == {ROOT_MODULE}, f"读干净区的位置应当只有 {ROOT_MODULE}，实际 {sorted(readers)}"


def test_live_stays_a_network_capability() -> None:
    """live.py 的专属边界：可见 sources（联网），永不可见 storage（碰盘只此一家）。"""
    seen = _sources_of(SITE / LIVE_MODULE)
    assert not seen - LIVE_ALLOWED, f"live.py 越界 import：{sorted(seen - LIVE_ALLOWED)}"
    assert not seen & STORAGE_PACKAGES, (
        "复权因子读盘在 prompt.py——live 碰 storage 就有了第二处口径换算"
    )


def test_auxiliary_io_stays_inside_the_two_named_files() -> None:
    """主数据、日历、仓库路径：只许组合根与壳碰，其余模块一个都不许有。

    这一条与上一条分开判，是因为它们放宽的方向不同：上一条永远不该多出一个位置，这一条随着
    壳的落地从"一个"变"两个"、而且以后可能跟着端点变多。写在一起就会看不出哪半句动了。
    """
    allowed = {ROOT_MODULE, SHELL_MODULE}
    offenders = {name for name in _pure_modules() if _sources_of(SITE / name) & SHELL_PACKAGES}
    assert not offenders, f"{sorted(offenders)} 越界读辅助数据；具名位置是 {sorted(allowed)}"
