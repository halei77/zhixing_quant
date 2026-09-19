"""干净区落盘：按主键合并、按分区原子替换（01 Step 3 验收 3「断点续传不产生重复」）。

幂等在这里是三件可分别验证的事，不是一句口头承诺：

1. **主键去重**：`(symbol, trade_date)` 已存在就是覆盖，不是追加。
2. **没变就不写**：合并结果与文件里已有的一致时直接跳过，不碰 mtime、不留一次重写。
   于是"抓到一半断了、整个重跑一遍"在文件系统层面看不出发生过——这正是断点续传要的形状。
3. **原子替换**：先写同目录的 `.part`，再 `os.replace`。同目录 rename 是原子操作，
   断在写一半时旧文件还完整在那儿，不会出现"半个 Parquet"被下游读到。

后到覆盖先到：重抓是修复坏数据唯一可行的手段。反过来"已有就不动"会让一个错日子永久
留在干净区，只能人工删文件——那等于把幂等换成"要幂等请先手工清理"。但同一批里给同一天
两条**不同**的值不是新旧关系，是冲突（多半是跨源对账没裁决完就交给存储层），直接抛
`BarConflict`：存储层不替谁对谁错做主（裁决在 04 §五 的门禁里，不在文件系统的写入顺序里）。

DuckDB 在本项目里只做它擅长的一段：读写 Parquet。行的合并与比较留在 Python，因为
"哪条算重复"要跟 `Bar` 讲同一种语言；写进 SQL 之后这条规则就没人能在评审时读懂了。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from zhixing_quant.domain.bar import Bar
from zhixing_quant.storage import layout
from zhixing_quant.storage.layout import NAMES, Record, partition_path, record_of

_COLUMNS = ", ".join(NAMES)
_TABLE = ", ".join(f"{name} {type_}" for name, type_ in layout.COLUMNS)
#: 整批一次绑定，按列传。`executemany` 每行重新规划一次语句，250 行的分区要 155ms；
#: 换成 `UNNEST(?)` 传列向量是 3.7ms——同一个动作，差 40 倍，而全市场一天就是 4430 个分区。
_INSERT = f"INSERT INTO outgoing SELECT {', '.join('UNNEST(?)' for _ in NAMES)}"


class BarConflict(ValueError):
    """同一批里，一个 (symbol, trade_date) 给了两条内容不同的行。"""


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
    """
    groups: dict[Path, dict[date, Bar]] = {}
    for bar in bars:
        path = partition_path(bar.symbol, bar.trade_date.year, dataset=dataset, root=root)
        slot = groups.setdefault(path, {})
        previous = slot.get(bar.trade_date)
        if previous is not None and previous != bar:
            raise BarConflict(
                f"{bar.symbol}@{bar.trade_date} 在这一批里给了两条不同的行"
                f"（{previous.source} 与 {bar.source}）：存储层不替它们裁决谁对"
            )
        slot[bar.trade_date] = bar

    added = repaired = rewritten = 0
    if groups:
        # 分区顺序固定：同一批数据重跑时触碰文件的顺序一致，日志与故障复现才对得上。
        with duckdb.connect() as con:
            for path in sorted(groups):
                existing = _read(con, path)
                merged, gained, changed = _merge(existing, groups[path])
                added += gained
                repaired += changed
                if merged == existing:
                    continue
                _rewrite(con, path, merged)
                rewritten += 1
    return WriteReport(partitions=len(groups), added=added, repaired=repaired, rewritten=rewritten)


def _merge(existing: Sequence[Record], incoming: dict[date, Bar]) -> tuple[list[Record], int, int]:
    """已有行 + 新行 → 按日排序的合并结果，外加"新增几天 / 改了几天"。

    已有文件里若有一天多行（人工改过、或更早的版本写的），这里一并收掉：输出恒为一天一行。
    """
    by_day: dict[date, Record] = {row.trade_date: row for row in existing}
    added = repaired = 0
    for day, bar in incoming.items():
        row = record_of(bar)
        current = by_day.get(day)
        if current is None:
            added += 1
        elif current != row:
            repaired += 1
        by_day[day] = row
    return [by_day[day] for day in sorted(by_day)], added, repaired


def _read(con: Any, path: Path) -> list[Record]:
    """整个分区读回。分区文件是"一只票一年"，几百行，读得起。

    DuckDB 交回来的是裸元组，`Record(*row)` 是它变成有名字的东西的那一步：列数一错就
    `TypeError`，而不是往后带着一列错位的数据安静走下去。
    """
    if not path.is_file():
        return []
    fetched: list[tuple[Any, ...]] = con.execute(
        f"SELECT {_COLUMNS} FROM read_parquet(?) ORDER BY trade_date", [str(path)]
    ).fetchall()
    return [Record(*row) for row in fetched]


def _rewrite(con: Any, path: Path, rows: Sequence[Record]) -> None:
    """整分区重写。增量写要引入 merge 语义与临时状态，而一个分区的体量不值得那点 IO。

    先落一张列类型写死的临时表再 `COPY`：直接从参数 `COPY` 时，一整个全 None 的
    `adj_factor` 列没有可推断的类型，落出来的 Parquet 列型就跟着猜——而"因子这列变成了
    BOOLEAN"这种错，要等到第一次复权查询才炸，届时没人会想到是落盘那天的事。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.part")
    con.execute("DROP TABLE IF EXISTS outgoing")
    con.execute(f"CREATE TEMP TABLE outgoing ({_TABLE})")
    con.execute(_INSERT, _columns(rows))
    con.execute(f"COPY (SELECT {_COLUMNS} FROM outgoing) TO ? (FORMAT PARQUET)", [str(partial)])
    partial.replace(path)


def _columns(rows: Sequence[Record]) -> list[list[object]]:
    """行 → 列。`strict=True` 是有分的：长度不齐时 DuckDB 给短的那列补 NULL 而不是报错，
    少传一列就变成"那一列整天缺值"，那是最难查的一种落盘错误。"""
    return [list(column) for column in zip(*rows, strict=True)]
