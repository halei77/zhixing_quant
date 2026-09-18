"""文档健康机器门禁（01 Step 0 验收标准 3、README §6.2 容量上限）。

防膨胀规则写在 README 里，但没有机器执行就等于没有：死链、未登记文档、
缺导航行、超容量，全部在此测红。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
DOCS = REPO_ROOT / "docs"

LINK = re.compile(r"\]\(([^)#\s]+?)(?:#[^)]*)?\)")
EXTERNAL = ("http://", "https://", "mailto:")
L2A = {"02", "03", "04", "05", "09"}
L2B = {"06", "07", "08"}

README_MAX_LINES = 150  # README §6.2
DOC_MAX_LINES = 300  # README §6.2
L2A_MAX = 6
L2B_MAX = 6
ROUTE_TABLE_MAX = 12  # README §6.2


def _md_files() -> list[Path]:
    return sorted(DOCS.rglob("*.md"))


def _prefix(name: str) -> str:
    match = re.match(r"(\d{2})", name)
    return match.group(1) if match else ""


def test_no_dead_relative_links() -> None:
    broken: list[str] = []
    for f in [README, *_md_files()]:
        for target in LINK.findall(f.read_text(encoding="utf-8")):
            if target.startswith(EXTERNAL):
                continue
            if not (f.parent / target).resolve().exists():
                broken.append(f"{f.relative_to(REPO_ROOT)} -> {target}")
    assert not broken, "死链：\n" + "\n".join(broken)


def test_every_doc_is_indexed_in_readme() -> None:
    """产出物不进主地图（README §6.1），但契约型文档必须被 README 索引到。"""
    readme = README.read_text(encoding="utf-8")
    unindexed = [str(p.relative_to(REPO_ROOT)) for p in _md_files() if p.name not in readme]
    assert not unindexed, "未登记文档：\n" + "\n".join(unindexed)


def test_readme_links_point_into_docs_dir() -> None:
    """README 在仓库根，文档在 docs/：入口链接必须带前缀，防止移动后残留旧路径。"""
    readme = README.read_text(encoding="utf-8")
    stale = [
        t for t in LINK.findall(readme) if not t.startswith(EXTERNAL) and re.match(r"\d{2}-", t)
    ]
    assert not stale, f"README 链接缺 docs/ 前缀：{stale}"


def test_each_doc_has_navigation_line() -> None:
    """每篇必须带 "> **层级** … **何时读我**" 导航行（README §6.4）。"""
    headless = [
        str(p.relative_to(REPO_ROOT))
        for p in _md_files()
        if "**层级**" not in "".join(p.read_text(encoding="utf-8").splitlines()[:6])
    ]
    assert not headless, "缺导航行：\n" + "\n".join(headless)


def test_capacity_limits() -> None:
    """README §6.2 容量硬上限：触发即整理，不许无声膨胀。"""
    assert len(README.read_text(encoding="utf-8").splitlines()) <= README_MAX_LINES, (
        f"README 超 {README_MAX_LINES} 行，按 §6.2 该整理而非继续加"
    )
    for p in _md_files():
        n = len(p.read_text(encoding="utf-8").splitlines())
        assert n <= DOC_MAX_LINES, f"{p.name} 共 {n} 行，超单文档 {DOC_MAX_LINES} 行上限"
    tops = {p.name for p in DOCS.glob("*.md")}
    assert len({_prefix(n) for n in tops if _prefix(n) in L2A}) <= L2A_MAX, "L2a 工作规范超 6 篇"
    assert len({_prefix(n) for n in tops if _prefix(n) in L2B}) <= L2B_MAX, "L2b 产品定义超 6 篇"


def test_route_table_within_limit() -> None:
    """README §四 路由表 ≤ 12 行（只数正文条目，不含表头与分隔行）。"""
    lines = README.read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("## 四、任务路由表"))
    rows = 0
    for ln in lines[start + 1 :]:
        if ln.startswith("## "):
            break
        if ln.startswith("|") and not re.match(r"^\|\s*[-:| ]+\|$", ln) and "如果你要" not in ln:
            rows += 1
    assert rows <= ROUTE_TABLE_MAX, f"路由表已 {rows} 行，超上限 {ROUTE_TABLE_MAX}"
