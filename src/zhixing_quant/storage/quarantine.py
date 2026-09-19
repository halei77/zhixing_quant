"""隔离区落盘：被门禁拒收的行按运行日进 Parquet（ADR-0008；04 §一「不删不改」）。

干净区存的是"市场是什么"，这里存的是"源给了什么、我们为什么不要"。两处差别决定了这里的代码
和 `write.py` 长得不一样：

1. **没有主键，只有整行**。同一个 `(source, symbol, trade_date, rules)` 下原始值不同的两条，
   是两次各自成立的观测，不是一条事实的两种说法——所以合并是**集合并**，后到的不覆盖先到的。
   干净区正好相反：那里的两行是对同一天价格的竞争主张，存储层拒绝裁决，直接抛 `BarConflict`。
   "重跑不产生重复"这条验收要求（01 Step 3 验收 3）在这里由整行相等来保证。
2. **值可以是 NaN**。所以价格列是 `VARCHAR`（理由见 `layout.QUARANTINE_COLUMNS` 那段注释）：
   把 `nan` 绑进 `DOUBLE` 会变成 `NULL`，而"源给了 NaN"（命中 R001）与"源没给这个字段"
   （命中 R010）是两条不同的结论，审计文件里不许把它们混成一条。

一天一个文件、文件名是运行日：一条被拒收的草稿可能恰恰缺着它本该有的那个日期，而运行日永远
已知。于是"2024-01-03 那天到底拒收了什么"就是一次读文件，不再依赖当次进程的内存。

这里对门禁的两样东西都只有**形状**上的依赖（`BarDraft`、`Violation`、`QuarantinedRow`），不调用
任何判定代码：存储层不认识规则，只知道"每条条目带着规则编号"——那是 04 §四 的"抽样附编号、
全量在隔离区可查"要的东西。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb

from zhixing_quant.domain.bar import BarDraft
from zhixing_quant.quality.facts import Violation
from zhixing_quant.storage import layout, partition
from zhixing_quant.storage.layout import Entry

if TYPE_CHECKING:
    from zhixing_quant.quality.engine import QuarantinedRow

#: 末尾三列是平行数组（`rules` / `levels` / `reasons`），读回来时 DuckDB 给的是 `list`。
_ARRAYS = 3


@dataclass(frozen=True)
class QuarantineReport:
    """一次落隔离区的账。`recorded=0` 而 `entries>0` 就是"这些条目盘上已经有了"。"""

    entries: int
    recorded: int
    rewritten: int


def entry_of(draft: BarDraft, violations: Sequence[Violation]) -> Entry:
    """被拒收的草稿 + 命中它的规则 → 一行。

    三个数组是从同一个 `violations` 序列投影出来的，等长是构造的结果而不是约定。代码存**原样**、
    不归一：归一不出来正是有些行的罪名，而 `draft.code` 遇到那种写法会抛。
    """
    return Entry(
        source=draft.source,
        symbol=draft.symbol,
        trade_date=draft.trade_date,
        open=_text(draft.open),
        high=_text(draft.high),
        low=_text(draft.low),
        close=_text(draft.close),
        volume=_text(draft.volume),
        amount=_text(draft.amount),
        adj_factor=_text(draft.adj_factor),
        is_suspended=draft.is_suspended,
        rules=tuple(v.rule_id for v in violations),
        levels=tuple(v.level for v in violations),
        reasons=tuple(v.reason for v in violations),
    )


def store_quarantined(
    rows: Sequence[QuarantinedRow], run_on: date, *, root: Path | None = None
) -> QuarantineReport:
    """落一次运行的隔离条目。空批次什么都不做——不建目录，更不建一个空文件。

    收的是门禁的输出（`GateOutcome.quarantined`）而不是本模块的 `Entry`：把"一条拒收变成一行"
    这件事留在写盘这一处，任务层就不必替它排一次列序（排错了没人会在日报上看出来）。

    不兜落盘异常：这里没有"今天没有拒收"和"今天没记下拒收"两种结果的容身之处，让磁盘错误冒到
    退出码，比静默留下一份少了几条证据的日报好（ADR-0008 决定 5）。
    """
    entries = [entry_of(row.draft, row.violations) for row in rows]
    if not entries:
        return QuarantineReport(entries=0, recorded=0, rewritten=0)
    path = layout.quarantine_path(run_on, root=root)
    with duckdb.connect() as con:
        existing = partition.read(con, path, layout.QUARANTINE_NAMES, _from_row)
        merged, recorded = _merge(existing, entries)
        rewritten = 0
        if merged != existing:
            partition.rewrite(con, path, layout.QUARANTINE_COLUMNS, merged)
            rewritten = 1
    return QuarantineReport(entries=len(entries), recorded=recorded, rewritten=rewritten)


def entries_on(run_on: date, *, root: Path | None = None) -> tuple[Entry, ...]:
    """那天拒收的全量条目，按落盘顺序。文件不在就是空。"""
    path = layout.quarantine_path(run_on, root=root)
    with duckdb.connect() as con:
        return tuple(partition.read(con, path, layout.QUARANTINE_NAMES, _from_row))


def _merge(existing: Sequence[Entry], incoming: Sequence[Entry]) -> tuple[list[Entry], int]:
    """已有的 + 这次交来的 → 去重后按稳定顺序排列，外加"其中真正新记了几条"。"""
    seen: dict[Entry, None] = dict.fromkeys(existing)
    recorded = 0
    for entry in incoming:
        if entry not in seen:
            seen[entry] = None
            recorded += 1
    return sorted(seen, key=_sort_key), recorded


def _sort_key(entry: Entry) -> tuple[str, ...]:
    """逐项转字符串再排：`None` 与 `date` 不可比，而隔离区里 `trade_date` 为 `None` 是常态。

    这个顺序只有"两次写出的字节一样"这一个用途——它决定 `store_quarantined` 重跑时能不能认出
    "盘上已经有了"，所以不许掺进任何业务含义（按日、按源排序都不属于这里）。
    """
    return tuple(_text(value) or "" for value in entry)


def _text(value: object) -> str | None:
    """原始值 → 落盘的字符串。`None` 保持 `None`（"源没给这个字段"），其余一律 `str()`
    （"源给的是什么"）：两者的区别就是 R010 与 R001 的区别，不许在落盘时被抹平。"""
    return None if value is None else str(value)


def _from_row(*values: Any) -> Entry:
    """Parquet 读回的一行 → `Entry`。

    末尾三个列表列要变回 `tuple`：与 `entry_of` 交出去的形状一致，否则"从盘上读到的那条"和
    "这次算出来的那条"永不相等，重跑会把整个文件当成新条目再记一遍。
    """
    head, arrays = values[:-_ARRAYS], values[-_ARRAYS:]
    return Entry(*head, *(tuple(row) for row in arrays))
