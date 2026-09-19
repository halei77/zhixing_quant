"""落盘格式的唯一出处：分区路径与列形状（ADR-0003 决定 1、ADR-0007 决定 2）。

布局与列形状必须只有一份：写和读各写一遍是这类代码最典型的腐烂方式——改了其中一边，
另一边会静默按旧形状读。Parquet 是列存，列名错了才报错，列序错了照样给数据，所以
连"哪一列在第几位"都在这里定，两处都从 `NAMES` 取顺序。

主键是 `(symbol, trade_date)`，与门禁 R008 用的同一个键（04 §二）：一天一行是干净区
对策略层的承诺，同一天并排两行会让回测把成交量算两遍。

代码可能来自命令行（`zx-daily --symbols`），所以路径里的代码一律先过 `normalize_code`：
它只放行 6 位数字，`../etc/passwd` 这类形状进不了路径。这既是归一，也是路径遍历的防线。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import NamedTuple

from zhixing_quant import config
from zhixing_quant.domain.bar import Bar
from zhixing_quant.domain.symbol import normalize_code

#: 数据集名即目录名。分钟线（Step 4）另起名字：布局同构，但体量和口径都不同，不混在一份文件里。
DAILY = "daily"

#: 列名 + DuckDB 类型。类型只此一份，建表与读回都以它为准（`REAL`/`FLOAT` 是单精度，不用）。
COLUMNS: tuple[tuple[str, str], ...] = (
    ("source", "VARCHAR"),
    ("symbol", "VARCHAR"),
    ("trade_date", "DATE"),
    ("open", "DOUBLE"),
    ("high", "DOUBLE"),
    ("low", "DOUBLE"),
    ("close", "DOUBLE"),
    ("volume", "DOUBLE"),
    ("amount", "DOUBLE"),
    ("adj_factor", "DOUBLE"),
    ("is_suspended", "BOOLEAN"),
)

#: 列顺序。写 `SELECT`、建表、比对行，全部用它，不用 `COLUMNS` 现拆。
NAMES: tuple[str, ...] = tuple(name for name, _ in COLUMNS)


#: 一行落盘记录。命名元组而不是裸 tuple：`(symbol, trade_date)` 是主键，取值写成
#: `row.trade_date` 才有时效——按位置取第 3 个元素在列序调整时会静默错位，而静默错位
#: 在列存格式里表现为"一整列被读成了别的列"，比报错难查一个量级。
class Record(NamedTuple):
    source: str
    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    adj_factor: float | None
    is_suspended: bool


def record_of(bar: Bar) -> Record:
    """`Bar` → 落盘行。写路径与"内容有没有变"的比较共用它，两边才不会用两套值。"""
    return Record(
        bar.source,
        bar.symbol,
        bar.trade_date,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.amount,
        bar.adj_factor,
        bar.is_suspended,
    )


def dataset_dir(dataset: str, root: Path | None = None) -> Path:
    """数据集目录。根只从配置拿（02 §六：路径不许自己拼），`root` 是给测试的注入点。"""
    return (config.parquet_dir() if root is None else root) / dataset


def partition_path(
    symbol: str, year: int, *, dataset: str = DAILY, root: Path | None = None
) -> Path:
    """一只票一年的文件。`symbol=NNNNNN.parquet` 这个写法照 ADR-0003 决定 1 原样落地。"""
    code = normalize_code(symbol)
    return dataset_dir(dataset, root) / f"year={year}" / f"symbol={code}.parquet"


def partitions(
    symbol: str, start: date, end: date, *, dataset: str = DAILY, root: Path | None = None
) -> tuple[Path, ...]:
    """区间可能落到的分区，按年升序；不保证存在（存在与否由读的一侧筛，见 `query`）。"""
    return tuple(
        partition_path(symbol, year, dataset=dataset, root=root)
        for year in range(start.year, end.year + 1)
    )


def year_of(path: Path) -> int:
    """从 `year=YYYY` 目录名取年份。分区是代码建的，解不出来就是目录被人手改过。"""
    return int(path.parent.name.removeprefix("year="))


def existing_partitions(
    symbol: str, *, dataset: str = DAILY, root: Path | None = None
) -> tuple[Path, ...]:
    """这只票已有的全部分区，按年升序。

    要整段历史而不是查询区间：复权因子是阶梯函数，区间首日之前发生过的除权也得算进来，
    否则一只 2015 年分红、2020 年起没分红的票在 2020 年这段会掉回 1.0。
    """
    code = normalize_code(symbol)
    base = dataset_dir(dataset, root)
    return tuple(sorted(base.glob(f"year=*/symbol={code}.parquet"), key=year_of))
