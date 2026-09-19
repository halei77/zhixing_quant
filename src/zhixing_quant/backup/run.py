"""把数据根拷进备份根：一代全量、一代增量、留 3 代（ADR-0005 补充决定二）。

`state.py` 管账，这里管字节。分界的判据是"这段逻辑要不要碰盘上的文件内容"：要，就在这层；
不要，就在 `state.py`。于是"增量该拷哪几个文件"在测试里是纯的，而这里只判"说拷的确实拷到了、
说不拷的确实没动"。

三个动作的**最后一个动作都是写账**（`save_state`）：账是这次备份的提交点。中途崩了，盘上会留下
一代内容齐全但账没更新的备份——下一次运行会把它当成"还没备份过"重拷一遍，那是对的；反过来
（账更新了而文件没拷全）才会备份出一代缺文件的树。
"""

from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from zhixing_quant.backup.state import (
    DELTA,
    FULL,
    Manifest,
    Stat,
    State,
    check_layout,
    diff,
    generation_dir,
    generations_dir,
    load_manifest,
    load_state,
    manifest_file,
    save_manifest,
    save_state,
    scan,
)

#: 读盘算哈希的块大小：够不到内存峰值，也够不满系统调用的开销。
CHUNK = 1 << 20

#: 保留几代全量（ADR-0005 决定 3）。
KEEP_FULLS = 3


class EmptyDataRoot(ValueError):
    """数据根扫出**零个文件**。这不是"今天没什么新东西"，而是盘没挂上或路径配错了。

    拒它的理由与 `scan` 拒绝"根不存在要响"是同一条：这两种失败都长得像"一切正常"。让空扫描过去，
    全量会建出一份空基线（顺带把真基线裁掉），增量会把整本账判成"全删了"——两者都要到恢复那天
    才现形。真正的空数据根（新机器、还没采过数）是存在的，但那不该由备份任务来备份。
    """


def _require_files(current: dict[str, Stat], data: Path) -> None:
    if not current:
        raise EmptyDataRoot(
            f"数据根 {data} 里一个文件都没有，不接受这份备份（ADR-0005 补充决定一）"
        )


def copy_file(src: Path, dst: Path) -> Stat:
    """拷一个文件，顺手算出它的 sha256（补充决定三：哈希只跟着被拷的文件）。

    先写 `dst.tmp` 再改名：备份根在网盘同步目录里，同步客户端看见的是"这个文件存在了"而不是
    "这个文件在写"——一个半截的 Parquet 躺在备份里，要等到恢复那天才暴露。
    返回的 `Stat` 带**源文件**的形状与**这次拷过去那份**的哈希，正是账要记的那三样。
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    info = src.stat()
    tmp = dst.with_name(f"{dst.name}.tmp")
    digest = hashlib.sha256()
    with src.open("rb") as reader, tmp.open("wb") as writer:
        while block := reader.read(CHUNK):
            digest.update(block)
            writer.write(block)
    tmp.replace(dst)
    return Stat(info.st_size, info.st_mtime_ns, digest.hexdigest())


@dataclass(frozen=True)
class Report:
    """一次备份跑完要报出来的东西。`markdown` 是给人看的那份，计数是日报与告警要用的。"""

    kind: str
    when: date
    base: date | None
    target: Path
    added: int
    changed: int
    deleted: int
    copied: int
    total_bytes: int
    seconds: float
    pruned: tuple[date, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def markdown(self) -> str:
        """一页纸的备份报告：先结论，再数，再落点（与 04 §四 的日报同一个形状）。"""
        head = "全量" if self.kind == FULL else "增量"
        lines = [
            f"# 备份 · {head} · {self.when.isoformat()}",
            "",
            f"- 落点：`{self.target}`",
            f"- 基线：{self.base.isoformat() if self.base else '本次就是基线'}",
            (
                f"- 变化：新增 {self.added}、改动 {self.changed}、删除 {self.deleted}；"
                f"拷了 {self.copied} 个文件 / {self.total_bytes / 1024 / 1024:.2f} MiB"
            ),
            f"- 耗时：{self.seconds:.1f} 秒",
        ]
        if self.pruned:
            gone = "、".join(day.isoformat() for day in self.pruned)
            lines.append(f"- 裁掉旧代（保留最近 {KEEP_FULLS} 代全量）：{gone}")
        lines += [f"- 注：{note}" for note in self.notes]
        return "\n".join(lines) + "\n"


def _day(name: str, *, where: Path) -> date:
    """目录名 → 日期。认不出的要响：静默跳过就会把该删的那代留到永远，或把不该算的算进名额。"""
    try:
        return date.fromisoformat(name)
    except ValueError as exc:
        raise ValueError(f"{where / name} 不像一代备份的目录名（要的是 YYYY-MM-DD）") from exc


def list_generations(backup: Path, kind: str) -> tuple[date, ...]:
    """`full/` 或 `delta/` 下已有的代，按日期升序。没有那个目录就是没有代，不是错。"""
    root = generations_dir(backup, kind)
    if not root.is_dir():
        return ()
    return tuple(sorted(_day(child.name, where=root) for child in root.iterdir() if child.is_dir()))


def delta_base(backup: Path, when: date) -> date | None:
    """某一增量的基线是哪一代全量——从它自己的 manifest 读，不从目录名猜。"""
    return load_manifest(backup, DELTA, when).base


def prune(backup: Path, *, keep: int = KEEP_FULLS) -> tuple[date, ...]:
    """只留最近 `keep` 代全量；基线比最老那代还早的增量一起删（补充决定二）。

    顺序是先写新、后删旧（`full()` 在写完账之后才调这里），所以任何时刻盘上都至少有一代能拼出
    完整树。删增量的判据是"它的基线已经不在了"——那种增量留着也拼不出任何东西。
    """
    fulls = list_generations(backup, FULL)
    if len(fulls) <= keep:
        return ()
    stale = fulls[: len(fulls) - keep]
    oldest_kept = fulls[len(fulls) - keep]
    for day in stale:
        shutil.rmtree(generation_dir(backup, FULL, day))
    gone = list(stale)
    for when in list_generations(backup, DELTA):
        base = delta_base(backup, when)
        if base is None or base < oldest_kept:
            shutil.rmtree(generation_dir(backup, DELTA, when))
            gone.append(when)
    return tuple(gone)


def full(when: date, *, data: Path, backup: Path, keep: int = KEEP_FULLS) -> Report:
    """建立一代基线：整棵数据根拷进 `full/<日期>/`，写清单，把账指到它身上，最后裁旧代。

    全量**不看旧的账**——它判的是"今天盘上有什么就是什么"。这是它能当基线的全部理由：账写坏了、
    被人删了、甚至 `state.json` 还是上个月的，跑一次全量都仍然是一份正确的备份。
    """
    check_layout(data, backup)
    started = time.monotonic()
    current = scan(data)
    _require_files(current, data)
    target = generation_dir(backup, FULL, when)
    shutil.rmtree(target, ignore_errors=True)
    files: dict[str, Stat] = {}
    for path in sorted(current):
        files[path] = copy_file(data / path, target / path)
    save_manifest(backup, Manifest(kind=FULL, when=when, base=None, files=files))
    save_state(backup, State(base=when, files=files))
    pruned = prune(backup, keep=keep)
    return Report(
        kind=FULL,
        when=when,
        base=None,
        target=target,
        added=len(files),
        changed=0,
        deleted=0,
        copied=len(files),
        total_bytes=sum(stat.size for stat in files.values()),
        seconds=time.monotonic() - started,
        pruned=pruned,
    )


def incremental(when: date, *, data: Path, backup: Path) -> Report:
    """一代增量：只拷账上说变了的那些，落 `delta/<日期>/`，账随之更新。

    同一天跑第二次不丢东西：这一代的 manifest 是**读回来合并**再写回去的，所以第一次拷过去的文件
    不会因为第二次"没变化"就被一份空清单替掉。

    啥也没变就不建目录（网盘上多一个空代只是噪音），但这件事写在报告的 `notes` 里而不是抛成异常：
    备份工具最坏的失败是"跑了、什么都没备、退出码 0"，那句话得由报告替人说出来，而不是靠调用方
    记得 catch 哪一种异常。
    """
    check_layout(data, backup)
    started = time.monotonic()
    state = load_state(backup)
    current = scan(data)
    _require_files(current, data)
    changes = diff(state.files, current)
    earlier: Manifest | None = (
        load_manifest(backup, DELTA, when) if manifest_file(backup, DELTA, when).is_file() else None
    )
    if changes.empty and earlier is None:
        return Report(
            kind=DELTA,
            when=when,
            base=state.base,
            target=generation_dir(backup, DELTA, when),
            added=0,
            changed=0,
            deleted=0,
            copied=0,
            total_bytes=0,
            seconds=time.monotonic() - started,
            notes=(f"相对基线 {state.base.isoformat()} 没有任何变化，这一代没写盘",),
        )
    target = generation_dir(backup, DELTA, when)
    merged = dict(state.files)
    files: dict[str, Stat] = dict(earlier.files) if earlier else {}
    total_bytes = 0
    for path in changes.to_copy:
        copied = copy_file(data / path, target / path)
        files[path] = copied
        merged[path] = copied
        total_bytes += copied.size
    for path in changes.deleted:
        merged.pop(path, None)
    # 同一代里"先删后又出现"（网盘把文件抽走又同步回来）不该记成删除：它此刻就在 `current` 里，
    # 而恢复是"先加后删"，留着那条删除会把刚拷过去的文件抹掉。
    wiped = set(changes.deleted) | set(earlier.deleted if earlier else ())
    deleted = tuple(sorted(wiped - set(current)))
    save_manifest(
        backup, Manifest(kind=DELTA, when=when, base=state.base, files=files, deleted=deleted)
    )
    save_state(backup, State(base=state.base, files=merged))
    return Report(
        kind=DELTA,
        when=when,
        base=state.base,
        target=target,
        added=len(changes.added),
        changed=len(changes.changed),
        deleted=len(deleted),
        copied=len(changes.to_copy),
        total_bytes=total_bytes,
        seconds=time.monotonic() - started,
        notes=(f"这一代清单 {len(files)} 项，累计删掉 {len(deleted)} 项",),
    )
