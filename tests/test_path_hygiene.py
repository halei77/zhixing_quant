"""禁止硬编码绝对路径与 Windows 盘符（02 §六「仓库外的数据根」对应检查）。

数据根只能从配置项读（ADR-0007）。写死路径的后果不是难看，是换机器/换盘即
整批失效，而 02 §六 承诺了"CI 有一个扫描测试"，这条就是那个测试。

豁免点只有一个，且必须按仓库相对路径精确列出：将来 ZX_DATA_ROOT 的默认值总得写在
某个配置文件里，那是全仓唯一允许出现绝对路径的地方，届时把它加进 EXEMPT。按文件名
豁免（会被同名文件绕过）或放宽正则（会被静默清空）都不算豁免，算拆门禁——所以文件
末尾有一条自检测试盯着正则本身。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 本文件持有下面这些字面量，不豁免自己就会把样本判为违规
EXEMPT = {"tests/test_path_hygiene.py"}

SCAN_TARGETS = ["src", "tests", ".github", "pyproject.toml", ".pre-commit-config.yaml"]

FORBIDDEN: dict[str, re.Pattern[str]] = {
    # (?<!\w) 排除 "http://" 与注解返回值 "-> str:\n" 这类样本字符串造成的假阳性
    "Windows 盘符路径": re.compile(r"(?<!\w)[A-Za-z]:[\\/]"),
    "绝对 home 路径": re.compile(r"/home/|/Users/|/root/"),
    "挂载点路径": re.compile(r"/mnt/"),
}


def _scan_files() -> list[Path]:
    out: list[Path] = []
    for target in SCAN_TARGETS:
        path = REPO_ROOT / target
        candidates = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        out += [
            f
            for f in candidates
            if f.is_file()
            and f.relative_to(REPO_ROOT).as_posix() not in EXEMPT
            and "__pycache__" not in f.parts
        ]
    return out


def test_scan_targets_actually_exist() -> None:
    """扫描器要有东西可扫：目标全空说明骨架被挪走，本测试随即失去意义。"""
    files = _scan_files()
    assert len(files) >= 10, f"待扫文件仅 {len(files)} 个，扫描范围配置有误"


def test_no_hardcoded_paths() -> None:
    offenders: list[str] = []
    for f in _scan_files():
        text = f.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, pattern in FORBIDDEN.items():
                if pattern.search(line):
                    offenders.append(
                        f"{f.relative_to(REPO_ROOT)}:{lineno} [{label}] {line.strip()[:70]}"
                    )
    assert not offenders, "硬编码路径，应改从 ZX_DATA_ROOT 配置读取：\n" + "\n".join(offenders)


def test_scanner_itself_bites() -> None:
    """门禁的门禁：真违规必须被抓、样本假阳性必须不抓。

    删规则或把正则改松，前两条测试照样全绿——只有这条会红。02 §2 DoD-5
    要求"证明门禁会拦"，对本文件而言拦不拦由这条说了算。
    """
    dirty = 'p = "D:/data/x"\nq = "/home/y/z"\nr = "/mnt/c/w"\n'
    caught = {label for label, pat in FORBIDDEN.items() if pat.search(dirty)}
    assert caught == set(FORBIDDEN), f"三类违规只抓到 {sorted(caught)}"

    # 这两条正是本文件初版误报过的写法：URL 协议头、样例代码里的 `int:` 换行
    lookalikes = 'u = "https://example.com/a"\ng = "def f() -> int:\\n    return 1\\n"\n'
    false_alarms = {label for label, pat in FORBIDDEN.items() if pat.search(lookalikes)}
    assert not false_alarms, f"误报了正常写法：{sorted(false_alarms)}"
