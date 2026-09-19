"""落盘格式的唯一出处：分区路径与列形状（ADR-0003 决定 1、ADR-0007 决定 2）。

布局与列形状必须只有一份：写和读各写一遍是这类代码最典型的腐烂方式——改了其中一边，
另一边会静默按旧形状读。Parquet 是列存，列名错了才报错，列序错了照样给数据，所以
连"哪一列在第几位"都在这里定，两处都从 `NAMES` 取顺序。

主键是 `(symbol, trade_date)`，与门禁 R008 用的同一个键（04 §二）：一天一行是干净区
对策略层的承诺，同一天并排两行会让回测把成交量算两遍。分钟线把键扩成 `(symbol, ts)`——
一根K线的收盘时刻已含日期，所以两种粒度用的是同一条"键 = 行自己说得出的一刻"（ADR-0009 决定 3）。

两个 dataset 两套形状，各在自己的 `*_COLUMNS` 里定一份：干净区一只票一年一个文件、按主键合并；
隔离区一天一个文件、幂等键是整行（ADR-0008）。相同的是"写和读从同一处取列序"。

代码可能来自命令行（`zx-daily --symbols`），所以路径里的代码一律先过 `normalize_code`：
它只放行 6 位数字，`../etc/passwd` 这类形状进不了路径。这既是归一，也是路径遍历的防线。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

from zhixing_quant import config
from zhixing_quant.domain.bar import Bar
from zhixing_quant.domain.symbol import normalize_code

#: 数据集名即目录名。分钟线（Step 4）另起名字：布局同构，但体量和口径都不同，不混在一份文件里。
DAILY = "daily"
#: 分钟线三个 dataset（ADR-0009 决定 7）。周期写在名字里而不是写在列里：一个文件全是
#: 5 分钟的行，读它的人不需要先 `WHERE period=5`，也不必担心一次查询把两种周期平均到一起。
MINUTE_5 = "minute_5"
MINUTE_30 = "minute_30"
MINUTE_60 = "minute_60"
#: 隔离区（ADR-0008）：与干净区同一个数据根、不同 dataset 目录。读写两侧都按 dataset 取目录，
#: 所以读干净区的代码看不见隔离区，反之也一样。
QUARANTINE = "quarantine"

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

#: 分钟线 = 日线列 + `ts`，且 `ts` **加在末尾**：`Record` 的字段序就是它，两个 dataset 的
#: 行由同一个类装配，读回来的元组按各自的列序构造都成立（日线文件 11 列、`ts` 走默认值）。
#: 日线 dataset 不加这一列也是同一取舍：全空的一列只会让盘上已有的日线文件与形状表对不上。
MINUTE_COLUMNS: tuple[tuple[str, str], ...] = (*COLUMNS, ("ts", "TIMESTAMP"))
MINUTE_NAMES: tuple[str, ...] = tuple(name for name, _ in MINUTE_COLUMNS)


#: 一行落盘记录。命名元组而不是裸 tuple：`(symbol, trade_date)` 是主键，取值写成
#: `row.trade_date` 才有时效——按位置取第 3 个元素在列序调整时会静默错位，而静默错位
#: 在列存格式里表现为"一整列被读成了别的列"，比报错难查一个量级。
#:
#: 两种粒度共用这一个形状：`ts` 在末尾，日线恒为 None（它因此**不是**日线文件的一列，
#: 见 `COLUMNS`），分钟线才有值。一份形状两处的列序与读写都从它投影，不必担心"分钟线
#: 少了一列而没人报错"；写成两个类的话，mypy 连"`Record(*那11个值, ts)`"都判不过，
#: 只能把 12 个值抄两遍——那是真的会漂。
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
    ts: datetime | None = None


def record_of(bar: Bar) -> Record:
    """`Bar` → 落盘行。写路径与"内容有没有变"的比较共用它，两边才不会用两套值。

    `ts` 直接跟 `Bar` 走：日线为 None，落盘时按 dataset 的列名投影（`_values`），那一列
    因此不会出现在日线文件里。同一只票同一天的两种粒度在盘上是两个 dataset 的两份形状，
    而不是"一列有时有一列没有"。
    """
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
        bar.ts,
    )


@dataclass(frozen=True)
class DatasetSpec:
    """一个 dataset 的全部形状差异：列、分区内的行标识、落盘顺序。

    写在这里而不是散在 `write.py`/`query.py` 里，是因为这三项必须一起对：换了主键不换排序键，
    "没变就不写"就会每次都判成"变了"，于是重跑留下一次没人看得见的整文件重写。
    """

    name: str
    columns: tuple[tuple[str, str], ...]
    #: 分区内一行的标识。日线是交易日，分钟线是那一根的收盘时刻（它已含日期，不必再带交易日）。
    key: tuple[str, ...]
    #: 读回与合并后的排序键；`store_bars` 的"相等就不写"要求两侧同序。
    order: tuple[str, ...]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns)


SPECS: tuple[DatasetSpec, ...] = (
    DatasetSpec(DAILY, COLUMNS, ("trade_date",), ("trade_date",)),
    DatasetSpec(MINUTE_5, MINUTE_COLUMNS, ("ts",), ("trade_date", "ts")),
    DatasetSpec(MINUTE_30, MINUTE_COLUMNS, ("ts",), ("trade_date", "ts")),
    DatasetSpec(MINUTE_60, MINUTE_COLUMNS, ("ts",), ("trade_date", "ts")),
)

#: 干净区 dataset 名 → 形状。认不出的名字立刻抛：拼错一个 dataset 名的表现是"写进了一
#: 个没人读的新目录"，那比报错难查。
_BY_NAME = {spec.name: spec for spec in SPECS}


def dataset_spec(dataset: str) -> DatasetSpec:
    try:
        return _BY_NAME[dataset]
    except KeyError:
        raise ValueError(f"未知 dataset {dataset!r}，可选 {sorted(_BY_NAME)}") from None


def minute_dataset(period: str) -> str:
    """周期（`5`/`30`/`60`）→ dataset 名。源那边给的就是这三个字符串，这里只做一遍映射。"""
    name = f"minute_{period}"
    if name not in _BY_NAME:
        raise ValueError(f"不支持的分钟周期 {period!r}：ADR-0009 只定了 5/30/60")
    return name


#: 隔离区的列形状（ADR-0008 决定 3）：`BarDraft` 全字段 + 三条等长数组。与干净区最大的不同是
#: **除 `is_suspended` 外全部可空**——空就是"源没给"，那正是被拒收的那几种行本来的样子。
#:
#: 价格与量额存 `VARCHAR` 而不是 `DOUBLE`，理由只有一条：DuckDB 1.5.5 把 Python 侧的 `nan`
#: 绑定成 `NULL`（实测，`inf` 反而原样保住），而"源给了 NaN"与"源没给这个字段"在这里是两条
#: 不同的结论——前者命中 R001，后者命中 R010。隔离区是审计证据，把其中一条洗成另一条等于伪造
#: 现场。代价是要按数值筛时先 `CAST`（`'nan'`、`'inf'` 都能转回去，实测），而审计时的读法本来就是
#: "那天的原始值是什么"。
QUARANTINE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("source", "VARCHAR"),
    ("symbol", "VARCHAR"),
    ("trade_date", "DATE"),
    ("open", "VARCHAR"),
    ("high", "VARCHAR"),
    ("low", "VARCHAR"),
    ("close", "VARCHAR"),
    ("volume", "VARCHAR"),
    ("amount", "VARCHAR"),
    ("adj_factor", "VARCHAR"),
    ("is_suspended", "BOOLEAN"),
    # 一条被拒收的分钟K线，"哪一根"正是审计要问的那一句；缺了它，一天 48 根在文件里看起来
    # 一模一样。位置在三个平行数组**之前**是有约束的：`quarantine._from_row` 认"末尾三列是
    # 数组"，插到它们后面就会把 rules 读成 ts。老文件没这一列，读它会报错而不是给空——
    # 2026-09-19 改形状时数据根里只有一份隔离文件，已就地补列。
    ("ts", "TIMESTAMP"),
    ("rules", "VARCHAR[]"),
    ("levels", "VARCHAR[]"),
    ("reasons", "VARCHAR[]"),
)

QUARANTINE_NAMES: tuple[str, ...] = tuple(name for name, _ in QUARANTINE_COLUMNS)


class Entry(NamedTuple):
    """一条隔离区条目：被拒收的那条原始行，加"为什么"。

    `rules` / `levels` / `reasons` 位置一一对应，由同一个 `violations` 序列投影出来（ADR-0008
    决定 3）：等长是构造保证的，不是约定。`symbol` 是源给的**原样**，不归一——归一不出来正是
    有些行的罪名。数值列存的是它们的 `str()`，`None` 才是"源没给"（理由见上面那段）。
    `ts` 是日线/分钟线的分界：日线上它是 `NULL`，分钟线上它说"这一天里的哪一根"。
    """

    source: str
    symbol: str
    trade_date: date | None
    open: str | None
    high: str | None
    low: str | None
    close: str | None
    volume: str | None
    amount: str | None
    adj_factor: str | None
    is_suspended: bool
    ts: datetime | None
    rules: tuple[str, ...]
    levels: tuple[str, ...]
    reasons: tuple[str, ...]


def quarantine_path(run_on: date, *, root: Path | None = None) -> Path:
    """一次运行的隔离区文件：一天一个，文件名是**运行日**（ADR-0008 决定 2）。

    不按 `year/symbol` 分区：一条被拒收的草稿，它的 `trade_date` 可能恰恰是缺的（R010 就判这个），
    而运行日永远已知，与行的质量无关。也不做 hive 子目录：一天一文件没有分区可裁剪。

    "运行日"是**那次运行的报告日**，不是墙上时钟那天（ADR-0008 代价二）：十天之后补抓
    2024-01-03，那条拒收证据仍然落在 01-03 这个文件里，跟当天的日报对得上。
    """
    return dataset_dir(QUARANTINE, root) / f"{run_on.isoformat()}.parquet"


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


def dataset_partitions(dataset: str, *, root: Path | None = None) -> tuple[Path, ...]:
    """一个 dataset 在盘上的全部分区文件，按年升序。

    与下面那个的分工：`existing_partitions` 问"这只票有哪几格"，这个问"整块盘有哪几格"——
    `query.depth` 要的起点/末点是 dataset 级的事实，只能这么读。glob 留在本模块：让读的一侧
    自己拼 `year=*/symbol=*` 的话，布局一改它就安静地扫到空集合，而"这条管道还没数据"与
    "目录改名了"在结果里长得一模一样。
    """
    base = dataset_dir(dataset, root)
    return tuple(sorted(base.glob("year=*/symbol=*.parquet"), key=year_of))


def existing_partitions(
    symbol: str, *, dataset: str = DAILY, root: Path | None = None
) -> tuple[Path, ...]:
    """这只票已有的全部分区，按年升序。

    要整段历史而不是查询区间：复权因子是阶梯函数，区间首日之前发生过的除权也得算进来，
    否则一只 2015 年分红、2020 年起没分红的票在 2020 年这段会掉回 1.0。

    模式里带着代码而不是全量扫一遍再筛：`read_bars` 每只票每问一次就走这里，池子 4430 只时
    全量扫是每次四千多个文件名字。同一个形状，两种代价。
    """
    code = normalize_code(symbol)
    base = dataset_dir(dataset, root)
    return tuple(sorted(base.glob(f"year=*/symbol={code}.parquet"), key=year_of))
