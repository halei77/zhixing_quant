"""一个分区文件的读回与原子替换：干净区与隔离区共用（ADR-0003 决定 1、ADR-0008 决定 2）。

两份数据对"文件"做的动作是同一个：整读、整写、先落 `.part` 再 `os.replace`。而这个动作里
有三处 DuckDB 的坑是付过学费的：`executemany` 比 `UNNEST` 慢 40 倍、列类型必须由建表语句写死
而不是让 `COPY` 去猜、`SELECT` 必须点名列。复制第二遍的代价就是其中一处只修一边。

排序不在这里做：两个数据集的排序键不同（干净区按交易日，隔离区按整行），共享的读函数不替它们
决定，返回的就是文件里原本的顺序。

`factory(*row)` 是"裸元组变成有名字的行"的那一步：列数一错就 `TypeError`，而不是往后带着一列
错位的数据安静走下去——列存格式里"一整列被读成了别的列"比报错难查一个量级。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any


def read[R](con: Any, path: Path, names: Sequence[str], factory: Callable[..., R]) -> list[R]:
    """整个文件读回，列序按 `names`。文件不在就是空——第一天没有第一天，不是错误。

    **schema 演进（#66）**：Spec 后加的列在老文件里不存在——缺的列读成 None 而不是让
    SELECT 报 Binder Error。「那条行落盘时还没有这一列」与「这一列是空的」在文件层面是
    同一个事实，都答 None；反过来按名硬 SELECT 会把"新列刚上线、还没重灌"的窗口读成生产
    事故，逼读端等回填收工——半老半新的盘只会更难查。新增列因此零停机：先容缺读，回填
    逐票把值盖上。
    """
    if not path.is_file():
        return []
    present = {
        str(row[0])
        for row in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
    }
    columns = ", ".join(name if name in present else "NULL" for name in names)
    fetched: list[tuple[Any, ...]] = con.execute(
        f"SELECT {columns} FROM read_parquet(?)", [str(path)]
    ).fetchall()
    return [factory(*row) for row in fetched]


def rewrite(
    con: Any,
    path: Path,
    columns: Sequence[tuple[str, str]],
    rows: Sequence[Sequence[object]],
) -> None:
    """整文件重写。增量写要引入 merge 语义与临时状态，而这些文件的体量不值得那点 IO。

    先落一张列类型写死的临时表再 `COPY`：直接从参数 `COPY` 时，一整个全 None 的列没有可推断的
    类型，落出来的 Parquet 列型就跟着猜——而"因子这列变成了 BOOLEAN"这种错，要等到第一次查询
    才炸，届时没人会想到是落盘那天的事。
    """
    names = ", ".join(name for name, _ in columns)
    table = ", ".join(f"{name} {type_}" for name, type_ in columns)
    #: 整批一次绑定，按列传。`executemany` 每行重新规划一次语句，250 行的分区要 155ms；
    #: 换成 `UNNEST(?)` 传列向量是 3.7ms——同一个动作，差 40 倍，而全市场一天就是 4430 个分区。
    insert = "INSERT INTO outgoing SELECT " + ", ".join("UNNEST(?)" for _ in columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.part")
    con.execute("DROP TABLE IF EXISTS outgoing")
    con.execute(f"CREATE TEMP TABLE outgoing ({table})")
    con.execute(insert, _as_columns(rows))
    con.execute(f"COPY (SELECT {names} FROM outgoing) TO ? (FORMAT PARQUET)", [str(partial)])
    partial.replace(path)


def _as_columns(rows: Sequence[Sequence[object]]) -> list[list[object]]:
    """行 → 列。`strict=True` 是有分的：长度不齐时 DuckDB 给短的那列补 NULL 而不是报错，
    少传一列就变成"那一列整天缺值"，那是最难查的一种落盘错误。"""
    return [list(column) for column in zip(*rows, strict=True)]
