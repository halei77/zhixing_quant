"""备份的"账"：扫描数据根、判谁变了、state.json 与每代 manifest（ADR-0005 补充决定二、三）。

这一层不拷文件、也不读备份里的内容，只回答两个问题：**盘上现在有哪些文件**、**跟上次的账比谁变
了**。拆开不是为了好看——备份工具的测试最怕"跑一遍就得真拷几百 MB"，而判定与拷贝一分家，判定这
一段在 tmp 里几 KB 就能判完；真拷那一段（`run.py`）反过来只需要几 KB 的样本就能判它对盘做了什么。

全盘哈希的取舍写在 ADR-0005 补充决定三（含它那个明确接受的盲点），这里不重述；代码里只体现为一句
话：`diff` 比的是 `(size, mtime_ns)`，`sha256` 只跟着**被拷过**的文件进 manifest。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

#: 一代备份的两种形状。`full` 是完整树，`delta` 是自基线以来的变化（补充决定二）。
FULL = "full"
DELTA = "delta"


class BackupLayoutError(ValueError):
    """备份根与数据根互相包含（补充决定一）。这不是"配置得不理想"，是会算错账。"""


@dataclass(frozen=True)
class Stat:
    """一个文件在扫描那一刻的形状。`sha256` 只有**被拷过**的文件才非 None（补充决定三）。"""

    size: int
    mtime_ns: int
    sha256: str | None = None


@dataclass(frozen=True)
class Changes:
    """一次增量要拷的（`added` + `changed`）与要记一笔"没了"的（`deleted`）。"""

    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()

    @property
    def to_copy(self) -> tuple[str, ...]:
        return (*self.added, *self.changed)

    @property
    def empty(self) -> bool:
        return not (self.added or self.changed or self.deleted)


@dataclass(frozen=True)
class State:
    """上次备份留下的账：基线是哪一天的 full，加上每个文件当时的形状。"""

    base: date
    files: Mapping[str, Stat]


@dataclass(frozen=True)
class Manifest:
    """一代备份自己说清了它装了什么。有它才能不靠 `state.json` 就拼出任意一天的树。"""

    kind: str
    when: date
    base: date | None
    files: Mapping[str, Stat]
    deleted: tuple[str, ...] = ()


def check_layout(data: Path, backup: Path) -> None:
    """备份根不许套在数据根里，也不许反过来套。

    自我包含的一棵树，每一代都会把上一代套进自己的扫描：`<数据根>/backup/` 那个最省事的写法恰好
    就是这个写法，而它的表现是"备份成功"——直到第二次备份开始把第一次的备份当成新数据。
    """
    mine, theirs = data.resolve(), backup.resolve()
    if mine == theirs or mine in theirs.parents or theirs in mine.parents:
        raise BackupLayoutError(
            f"备份根 {backup} 与数据根 {data} 互相包含（ADR-0005 补充决定一）。"
            "备份必须落在数据根之外：套在一起的两棵树，第二代会把第一代当成新数据拷进去"
        )


def scan(root: Path) -> dict[str, Stat]:
    """数据根整棵树的当前形状：`{仓库相对路径: Stat}`，`sha256` 一律 None（扫描不算哈希）。

    两件事在这函数里判掉，因为它们都是"账会不会错"级别的：

    - **根不存在要响**。缺目录扫出来是空字典，而空字典在下游就是"没有要备份的文件"——那句话
      听起来像备份成功，实际是盘没挂上。补充决定一拒绝的是同一种"一切正常"的失败。
    - **软链整个跳过**（文件与目录都跳）。测试与临时 root 惯于用软链把真数据根指过来
      （`data -> /…/zhixing_data/data`），跟着链走就是把另一个目录当成这次备份的内容；更要命的
      是那条链若指向备份根本身，两棵树就成环了。
    """
    if not root.is_dir():
        raise FileNotFoundError(f"数据根读不到目录：{root}。盘没挂上就退出，不写一份空备份")
    out: dict[str, Stat] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not (here / d).is_symlink()]
        for name in filenames:
            path = here / name
            if path.is_symlink():
                continue
            info = path.stat()
            out[path.relative_to(root).as_posix()] = Stat(info.st_size, info.st_mtime_ns)
    return out


def _stamp(stat: Stat) -> tuple[int, int]:
    """判"变没变"看的那两个数。`sha256` 故意不在其中，见 `diff` 的说明。"""
    return stat.size, stat.mtime_ns


def diff(prev: Mapping[str, Stat], cur: Mapping[str, Stat]) -> Changes:
    """两份账一比，得出这一次增量。

    比的是 `_stamp` 而不是整个 `Stat`：`prev` 从 `state.json` 读回来时带着 sha256，而 `cur`
    由 `scan` 出的永远是 None——直接 `!=` 会让**每个文件每次都变**，增量退化成全量而没有任何地方
    报错。这条是这个模块最容易被改坏的一行，所以它单独有一条测试。
    """
    return Changes(
        added=tuple(sorted(p for p in cur if p not in prev)),
        changed=tuple(sorted(p for p in cur if p in prev and _stamp(cur[p]) != _stamp(prev[p]))),
        deleted=tuple(sorted(p for p in prev if p not in cur)),
    )


def state_file(backup: Path) -> Path:
    return backup / "state.json"


def generation_dir(backup: Path, kind: str, when: date) -> Path:
    """一代备份的目录：`<备份根>/full|delta/<YYYY-MM-DD>`。"""
    return generations_dir(backup, kind) / when.isoformat()


def generations_dir(backup: Path, kind: str) -> Path:
    """某一种备份的容器目录：`<备份根>/full` 或 `<备份根>/delta`。"""
    if kind not in (FULL, DELTA):
        raise ValueError(f"没听过这一代备份：{kind}（只有 {FULL} 与 {DELTA}）")
    return backup / kind


def manifest_file(backup: Path, kind: str, when: date) -> Path:
    return generation_dir(backup, kind, when) / "manifest.json"


def _dumps(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def _entry(stat: Stat) -> list[int | str]:
    return [stat.size, stat.mtime_ns, stat.sha256 if stat.sha256 is not None else ""]


def _stat_of(raw: object) -> Stat:
    """`_entry` 的读回。写成函数是为了读写只有一处认识这个数组的形状。"""
    if not isinstance(raw, list) or len(raw) != 3:
        raise ValueError(f"账目形状不对，期望 [大小, mtime_ns, sha256]，实际：{raw!r}")
    size, mtime_ns, sha = raw
    if not isinstance(size, int) or not isinstance(mtime_ns, int) or not isinstance(sha, str):
        raise ValueError(f"账目字段类型不对：{raw!r}")
    return Stat(size, mtime_ns, sha or None)


def _files_of(raw: object) -> dict[str, Stat]:
    if not isinstance(raw, dict):
        raise ValueError("文件账目不是一个字典")
    return {str(path): _stat_of(entry) for path, entry in raw.items()}


def _date_of(raw: object, label: str) -> date:
    if not isinstance(raw, str):
        raise ValueError(f"{label} 要是一个 ISO 日期字符串，实际：{raw!r}")
    return date.fromisoformat(raw)


def save_state(backup: Path, state: State) -> None:
    """写 `state.json`——**这一步是这次备份的提交点**，所以它永远是最后一个动作。

    先写 `.tmp` 再 `os.replace`：网盘同步客户端会在文件写到一半时就把"已经存在"这个事实抓走，
    一个看不见内容的半成品比一次失败更糟——下一次运行会把账读坏，而坏账的表现是"什么都没变"。
    """
    path = state_file(backup)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        _dumps({"base": state.base.isoformat(), "files": _files_json(state.files)}),
        encoding="utf-8",
    )
    tmp.replace(path)


def _files_json(files: Mapping[str, Stat]) -> dict[str, list[int | str]]:
    return {path: _entry(stat) for path, stat in sorted(files.items())}


def load_state(backup: Path) -> State:
    """读那本账。**缺文件要响**，不能当成"还没有账"：那会让一次增量悄悄改出一份新基线，
    而新基线意味着上一代 full 与它之间的 delta 全成了孤儿——恢复那天才发现拼不出来。
    第一次跑请走 `zx-backup full`，那才是建立基线的动作。
    """
    raw = json.loads(state_file(backup).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "base" not in raw or "files" not in raw:
        raise ValueError(f"{state_file(backup)} 里缺 base 或 files，不接受半份账")
    return State(base=_date_of(raw["base"], "base"), files=_files_of(raw["files"]))


def save_manifest(backup: Path, manifest: Manifest) -> Path:
    """把一代备份的清单写进那一代自己的目录里（读写同一处形状）。"""
    path = manifest_file(backup, manifest.kind, manifest.when)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        _dumps(
            {
                "kind": manifest.kind,
                "when": manifest.when.isoformat(),
                "base": manifest.base.isoformat() if manifest.base is not None else "",
                "files": _files_json(manifest.files),
                "deleted": list(manifest.deleted),
            }
        ),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def load_manifest(backup: Path, kind: str, when: date) -> Manifest:
    raw = json.loads(manifest_file(backup, kind, when).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{manifest_file(backup, kind, when)} 里不是一个字典")
    for key in ("kind", "when", "files"):
        if key not in raw:
            raise ValueError(f"清单里缺 {key}：一代备份说不清自己装了什么就不该被认出来")
    base = raw.get("base", "")
    return Manifest(
        kind=str(raw["kind"]),
        when=_date_of(raw["when"], "when"),
        base=_date_of(base, "base") if isinstance(base, str) and base else None,
        files=_files_of(raw["files"]),
        deleted=tuple(str(p) for p in raw.get("deleted", [])),
    )
