"""把备份拼回来，并证明拼回来的那份**读得动**（ADR-0005 决定 5、补充决定四）。

四个函数一层比一层碰盘：`plan` 只读目录名与清单，`restore` 才拷字节，`verify` 在拷完之后用查询
层真读一遍。拆开是因为"恢复计划算错了"与"网盘把文件写坏了"是两种病，要的证据不是一回事——前者
对目录名与清单就能判，后者必须把字节读出来。

叠代的顺序在 `plan` 与 `restore` 里各写一次就迟早会漂，所以"恢复完应该有什么"这件事只由
`plan` 说：它给出代与日期，`restore` 按那个顺序把清单叠起来，删项在每一代的**新增之后**应用
（同一天先加后删与先删后加是两棵不同的树，而 ADR 定的语义是"这一代结束时它不在了"）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath

import duckdb

from zhixing_quant.backup.run import copy_file, delta_base, list_generations
from zhixing_quant.backup.state import DELTA, FULL, load_manifest
from zhixing_quant.storage import layout
from zhixing_quant.storage.query import Cover, depth

#: `clean_zone`（干净区在数据根里的那一段目录名）允许的写法。由调用方从 `config` 读出来。
CleanZone = str | PurePosixPath


class NoGeneration(ValueError):
    """要恢复的那天没有可用的基线：要么日期比最早一代还早，要么备份根里根本没做过全量。"""


class NothingToVerify(ValueError):
    """那几代里干净区没有已知的 dataset 可读。

    单独一个类型是因为它是"演练没做成"而不是"演练通过得很难看"：报 0 的那个退出码给了它，
    就是决定 5 那句"没演练过的备份等于没有备份"最响的一次。
    """


@dataclass(frozen=True)
class Plan:
    """恢复某天要用的那些代：一个全量 + 其后连续的增量。"""

    as_of: date
    base: date
    deltas: tuple[date, ...]

    @property
    def generations(self) -> tuple[tuple[str, date], ...]:
        """按应用的先后排出 `(kind, 日期)`。`restore` 只认这个顺序。"""
        return ((FULL, self.base), *((DELTA, when) for when in self.deltas))


@dataclass(frozen=True)
class Restored:
    """一次恢复跑完的事实：拼出来的那棵树有哪些文件、多大，以及哪些字节与清单对不上。

    `total_bytes` 说的是**结果那棵树**，不是"一共搬了多少字节"：某一代拷过去、后一代又删掉的
    文件不计进来，哈希对不上的也不计（它已经在 `bad` 里被点名了）。
    """

    plan: Plan
    files: tuple[str, ...]
    bad: tuple[str, ...]
    total_bytes: int

    @property
    def intact(self) -> bool:
        return not self.bad


@dataclass(frozen=True)
class Verification:
    """恢复演练的结论。`ok` 的判据是可读，不是存在（补充决定四）。

    `read_error` 非空表示"字节都拷过去了，查询层却读不出行"——Parquet 被同步客户端在半截处补齐、
    footer 坏了都是这种坏法，它抛的是 duckdb 的异常而不是返回值。演练的职责是把这句话变成报告，
    不是变成一个 traceback：一个会崩的演练与一个没有演练只差在崩的那次有人盯着看。
    """

    restored: Restored
    dataset: str
    cover: Cover | None
    read_error: str = ""

    @property
    def ok(self) -> bool:
        return self.restored.intact and self.cover is not None

    @property
    def markdown(self) -> str:
        """演练报告：先给结论，再把"读到了什么"原样报出来。"""
        plan = self.restored.plan
        head = "通过" if self.ok else "不通过"
        lines = [
            f"# 恢复演练 · {plan.as_of.isoformat()} · {head}",
            "",
            f"- 拼的代：全量 {plan.base.isoformat()}"
            + (
                "（其后的增量：" + "、".join(when.isoformat() for when in plan.deltas) + "）"
                if plan.deltas
                else "（其后没有增量）"
            ),
            f"- 恢复出 {len(self.restored.files)} 个文件 / "
            f"{self.restored.total_bytes / 1024 / 1024:.2f} MiB，"
            f"哈希对不上 {len(self.restored.bad)} 个",
            f"- 用查询层读 `{self.dataset}`：{self._cover_line()}",
        ]
        if self.restored.bad:
            lines.append("")
            lines.append("## 备份里那些读坏了的文件")
            lines += [f"- `{path}`" for path in self.restored.bad]
        return "\n".join(lines) + "\n"

    def _cover_line(self) -> str:
        if self.read_error:
            return f"读不出行——{self.read_error}"
        if self.cover is None:
            return "一份 K线都没读到——恢复出来的干净区是空的"
        return (
            f"首末 {self.cover.first.isoformat()} ~ {self.cover.last.isoformat()}，"
            f"有行的交易日 {self.cover.days} 天，票 {self.cover.symbols} 只"
        )


def _wanted(path: str, only: str | None) -> bool:
    return only is None or path.startswith(only)


def plan(backup: Path, as_of: date) -> Plan:
    """哪天要恢复 → 用哪些代。基线取"不晚于那天里最新的一个 full"，增量只收**基线确实是它**的。

    后者不是洁癖：被裁掉的旧全量会留下无主的增量（`prune` 会删，但网盘同步中途失败就可能没删
    干净），把它们叠进来会拼出一棵哪一天都不存在的树。
    """
    fulls = tuple(when for when in list_generations(backup, FULL) if when <= as_of)
    if not fulls:
        raise NoGeneration(
            f"{backup / FULL} 里没有不晚于 {as_of.isoformat()} 的全量，"
            f"{as_of.isoformat()} 那天拼不出来（先跑一次 zx-backup full 建立基线）"
        )
    base = fulls[-1]
    deltas = tuple(
        when
        for when in list_generations(backup, DELTA)
        if base <= when <= as_of and delta_base(backup, when) == base
    )
    return Plan(as_of=as_of, base=base, deltas=deltas)


def restore(backup: Path, into: Path, what: Plan, *, only: str | None = None) -> Restored:
    """按 `what` 把树叠进 `into`，一边拷一边把字节与清单上的 sha256 对一遍。

    校验放在这里而不是"拷完再全树扫一遍"：读的就是要落进去的那段字节，一次 IO 办两件事；而它
    验的是**备份里那份**此刻还完好——清单上的哈希是当年拷进备份时算的，对不上就是网盘又把它写
    坏了（补充决定四说的那种坏法：字节大小都正常，只有读出来才知道）。

    `into` 必须是空的：叠代只对"这一代里有的文件"发言，一个不空的目录里那些**没人写它**的文件
    会被当成恢复出来的结果——那是一棵哪一天都不存在的树，而它看起来完全正常。
    """
    if into.resolve() == backup.resolve() or backup.resolve() in into.resolve().parents:
        raise ValueError(
            f"恢复目标 {into} 套在备份根 {backup} 里：往备份里面恢复，等于把结果当成又一代备份"
        )
    if into.exists() and any(into.iterdir()):
        raise ValueError(f"恢复目标 {into} 不空：叠代只会覆盖清单点名的文件，多出来的一律算不进去")
    present: set[str] = set()
    bad: list[str] = []
    #: 按路径记字节，最后再求和：某一代拷过去、后一代又删掉的文件不该计进"恢复出多少 MiB"。
    sizes: dict[str, int] = {}
    for kind, when in what.generations:
        manifest = load_manifest(backup, kind, when)
        for path, stat in sorted(manifest.files.items()):
            if not _wanted(path, only):
                continue
            copied = copy_file(backup / kind / when.isoformat() / path, into / path)
            if copied.sha256 != stat.sha256:
                bad.append(path)
            else:
                sizes[path] = stat.size
            present.add(path)
        for path in manifest.deleted:
            if _wanted(path, only):
                (into / path).unlink(missing_ok=True)
                present.discard(path)
                sizes.pop(path, None)
    return Restored(
        plan=what,
        files=tuple(sorted(present)),
        bad=tuple(bad),
        total_bytes=sum(sizes.values()),
    )


def _dataset_of(path: str, zone: PurePosixPath) -> str | None:
    """这个备份内相对路径属于干净区里的哪个 dataset。不属于、或不是已知的那四个 → None。

    dataset 名单从 `layout.SPECS` 拿：备份不自己记一份"什么算一个数据集"，否则隔离区
    （`data/quarantine/`，它不是 dataset）与将来新加的 dataset 都会让这里悄悄多一份或少一份账。
    """
    parts = PurePosixPath(path).parts
    known = {spec.name for spec in layout.SPECS}
    if len(parts) <= len(zone.parts) or parts[: len(zone.parts)] != zone.parts:
        return None
    name = parts[len(zone.parts)]
    return name if name in known else None


def pick_dataset(backup: Path, *, clean_zone: CleanZone, as_of: date) -> str | None:
    """演练该挑哪个数据集：那几代里覆盖文件**最多**的那个 dataset。

    决定 5 的原话是"随机挑一个数据集"，但随机在测试里不可复现、在 Runbook 里不可检查，所以换成
    一条确定规则：挑最全的那个。它与随机的差别只有一个方向——不会挑到空的那一侧；而名字从清单来，
    不是代码里写死的一个，这一点与"随机"给的是同一种保护。
    """
    names: dict[str, int] = {}
    zone = PurePosixPath(clean_zone)
    for kind, when in plan(backup, as_of).generations:
        for path in load_manifest(backup, kind, when).files:
            if (dataset := _dataset_of(path, zone)) is not None:
                names[dataset] = names.get(dataset, 0) + 1
    return max(names, key=lambda name: (names[name], name)) if names else None


def verify(
    backup: Path,
    *,
    clean_zone: CleanZone,
    into: Path,
    dataset: str | None = None,
    as_of: date | None = None,
) -> Verification:
    """跑一次决定 5 要的演练：挑一个数据集恢复到临时目录 → 逐文件对哈希 → 用查询层真读一段。

    `clean_zone` 是干净区在数据根里的那一段（今天就是 `data`），由调用方从 `config` 读进来——
    备份层不自己拼那个名字，否则"数据根里干净区叫什么"就有两处答案。

    为什么非要用 `storage.query` 读一遍：字节对得上只说明"这份拷贝与当年那份一致"，而 Parquet
    被同步客户端在半截处补齐、或 DuckDB 认不出这个 footer，都是**字节一致而读不出行**的坏法。
    `depth()` 扫的正是分区目录 + DuckDB 聚合，站点取数走的是同一条路。
    """
    when = as_of if as_of is not None else date.today()
    what = plan(backup, when)
    zone = PurePosixPath(clean_zone)
    picked = dataset if dataset is not None else pick_dataset(backup, clean_zone=zone, as_of=when)
    if picked is None:
        raise NothingToVerify(
            f"{what.base.isoformat()} 那几代的清单里没有 `{zone}` 下任何已知 dataset："
            "没有东西可验，这不算演练通过"
        )
    only = f"{zone.as_posix()}/{picked}/"
    restored = restore(backup, into, what, only=only)
    try:
        cover = depth(picked, root=into.joinpath(*zone.parts))
        read_error = ""
    except duckdb.Error as exc:
        # 恢复出来的字节读不出行。这正是演练存在的理由，所以它变成一个结论，不是一个 traceback。
        read_error = f"{type(exc).__name__}: {exc}"
        cover = None
    return Verification(restored=restored, dataset=picked, cover=cover, read_error=read_error)
