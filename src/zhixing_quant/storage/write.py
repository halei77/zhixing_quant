"""干净区落盘：按主键合并、按分区原子替换（01 Step 3 验收 3「断点续传不产生重复」）。

幂等在这里是三件可分别验证的事，不是一句口头承诺：

1. **主键去重**：主键已存在就是覆盖，不是追加。日线的键是 `(symbol, trade_date)`，分钟线是
   `(symbol, ts)`（ADR-0009 决定 3）——两边的键都从 `layout.DatasetSpec` 拿，不在这里分粒度。
2. **没变就不写**：合并结果与文件里已有的一致时直接跳过，不碰 mtime、不留一次重写。
   于是"抓到一半断了、整个重跑一遍"在文件系统层面看不出发生过——这正是断点续传要的形状。
3. **原子替换**：先写同目录的 `.part`，再 `os.replace`。同目录 rename 是原子操作，
   断在写一半时旧文件还完整在那儿，不会出现"半个 Parquet"被下游读到。这个动作连同 DuckDB
   的几处坑一起在 `partition.py`，因为隔离区（`quarantine.py`）做的是同一件事。

后到覆盖先到：重抓是修复坏数据唯一可行的手段。反过来"已有就不动"会让一个错日子永久
留在干净区，只能人工删文件——那等于把幂等换成"要幂等请先手工清理"。但同一批里给同一天
两条**不同**的值不是新旧关系，是冲突（多半是跨源对账没裁决完就交给存储层），直接抛
`BarConflict`：存储层不替谁对谁错做主（裁决在 04 §五 的门禁里，不在文件系统的写入顺序里）。
——隔离区正相反，那里的两行是两次各自成立的观测，所以那边合并、不裁决（ADR-0008）。

DuckDB 在本项目里只做它擅长的一段：读写 Parquet。行的合并与比较留在 Python，因为
"哪条算重复"要跟 `Bar` 讲同一种语言；写进 SQL 之后这条规则就没人能在评审时读懂了。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from operator import attrgetter
from pathlib import Path
from typing import Any

import duckdb

from zhixing_quant.domain.bar import Bar
from zhixing_quant.storage import layout, partition
from zhixing_quant.storage.layout import Record, partition_path, record_of


class BarConflict(ValueError):
    """同一批里，一个主键给了两条内容不同的行。"""


@dataclass(frozen=True)
class WriteReport:
    """一次落盘的账。`rewritten=0` 就是"这次重跑什么都没改"，它是验收 3 的直接证据。"""

    partitions: int
    added: int
    repaired: int
    rewritten: int


def store_bars(
    bars: Sequence[Bar], *, dataset: str = layout.DAILY, root: Path | None = None
) -> WriteReport:
    """落一批干净区K线。空批次什么都不做（不建目录、不碰文件）。

    调用方给的是 `GateOutcome.clean_zone`——门禁放行之后的那些行。本函数不复核门禁，
    但 `Bar` 构造不出违规行，所以能进来的必定字段齐全、OHLC 有序。

    主键与排序都从 `layout.DatasetSpec` 拿（dataset 的形状差异集中在那一处）：日线按交易日
    合并，分钟线按那根K线的收盘时刻合并。同一批里键值不同就是冲突，与粒度无关。行的粒度与
    dataset 不匹配也是抛而不是"写进去再看"——两种粒度各是一份形状，没有哪一方该被静默投影掉。
    """
    spec = layout.dataset_spec(dataset)
    groups: dict[Path, dict[tuple[object, ...], Bar]] = {}
    for bar in bars:
        path = partition_path(bar.symbol, bar.trade_date.year, dataset=dataset, root=root)
        key = tuple(getattr(bar, name) for name in spec.key)
        if any(part is None for part in key):
            # 分钟线的键是 ts：它为空说明这条行"属于哪天但不知是几点"，落进去就会和同一天
            # 其它缺 ts 的行并成一个键——两根K线存成一根。门禁的 R010 判的是缺日期，管不到
            # 这种情况，所以存储层自己响。
            raise ValueError(
                f"{bar.symbol} 要落进 {spec.name}，但它的主键列为空（{','.join(spec.key)}）："
                "存储层不接受没有身份的行"
            )
        if bar.ts is not None and "ts" not in spec.names:
            # 上面那条的反面：日线文件没有 `ts` 列，带着时刻的行落进去会**静默丢掉**那一列。
            # 键没变、行数没变、`Bar` 也构造得出来，一次 dataset 参数写错就把一天 48 根K线压成
            # 一根，而事后从盘上看不出发生过——所以这里也只有一条路：响。
            raise ValueError(
                f"{bar.symbol} 带着收盘时刻 {bar.ts} 却要落进 {spec.name}："
                "这个 dataset 没有 ts 列，存储层不替它把粒度扔掉"
            )
        slot = groups.setdefault(path, {})
        previous = slot.get(key)
        if previous is not None and previous != bar:
            raise BarConflict(
                f"{bar.symbol}@{bar.trade_date} {bar.ts or ''} 在这一批里给了两条不同的行"
                f"（{previous.source} 与 {bar.source}）：存储层不替它们裁决谁对"
            )
        slot[key] = bar

    added = repaired = rewritten = 0
    if groups:
        # 分区顺序固定：同一批数据重跑时触碰文件的顺序一致，日志与故障复现才对得上。
        with duckdb.connect() as con:
            for path in sorted(groups):
                existing = _read(con, path, dataset=dataset)
                merged, gained, changed = _merge(existing, groups[path], dataset=dataset)
                added += gained
                repaired += changed
                if merged == existing:
                    continue
                partition.rewrite(con, path, spec.columns, [_values(row, spec) for row in merged])
                rewritten += 1
    return WriteReport(partitions=len(groups), added=added, repaired=repaired, rewritten=rewritten)


def _merge(
    existing: Sequence[Record],
    incoming: dict[tuple[object, ...], Bar],
    dataset: str = layout.DAILY,
) -> tuple[list[Record], int, int]:
    """已有行 + 新行 → 按主键排序的合并结果，外加"新增几行 / 改了几行"。

    已有文件里若有一个键多行（人工改过、或更早的版本写的），这里一并收掉：输出恒为一键一行。
    """
    spec = layout.dataset_spec(dataset)
    by_key: dict[tuple[object, ...], Record] = {
        tuple(getattr(row, name) for name in spec.key): row for row in existing
    }
    added = repaired = 0
    for key, bar in incoming.items():
        row = record_of(bar)
        current = by_key.get(key)
        if current is None:
            added += 1
        elif current != row:
            repaired += 1
        by_key[key] = row
    return sorted(by_key.values(), key=attrgetter(*spec.order)), added, repaired


def _read(con: Any, path: Path, dataset: str = layout.DAILY) -> list[Record]:
    """整个分区读回。分区文件是"一只票一年"，日线几百行、分钟线一万多行，都读得起。

    顺序在这里定，不在 SQL 里排：`store_bars` 拿它和合并结果比"相等就不写"，而这个判据只有在
    两边同序时才有意义。共享的读函数不替两个数据集各自的排序键做主（见 `partition` 的模块说明）。
    """
    spec = layout.dataset_spec(dataset)
    return sorted(partition.read(con, path, spec.names, Record), key=attrgetter(*spec.order))


def _values(row: Record, spec: layout.DatasetSpec) -> tuple[object, ...]:
    """按 dataset 的列名投影一行。

    不直接 `tuple(row)`：`Record` 是两种粒度共用的 12 个字段，而日线文件只有 11 列。按**名字**
    取而不是切片，是为了让"列序变了"在这里没有藏身之处——切片会安静地把 close 写成 volume。
    """
    return tuple(getattr(row, name) for name in spec.names)
