"""隔离区落盘（ADR-0008；04 §一「不删不改」、§四「全量在隔离区可查」）。

判据全落在"盘上的文件与读回来的值"上：这一步要验收的是"拒收的行不再只活在当次运行的内存里"，
那是一句关于磁盘的话，不是关于返回值的话。这里最值钱的两条形状都是反直觉的，所以各钉一条看着：

1. **同一个 `(source, symbol, trade_date, rules)` 下原始值不同的两条，这里两条都留**。干净区遇到
   那种形状会抛 `BarConflict`（`test_write` 里钉着），因为那里的两行是对同一天价格的竞争主张；
   这里的两行是两次各自成立的观测，幂等键只能是整行。
2. **价格列是 `VARCHAR`**。DuckDB 1.5.5 把 Python 侧的 `nan` 绑进 `DOUBLE` 会写成 `NULL`（实测，
   `inf` 反而原样保住），那会把"源给了 NaN"（命中 R001）洗成"源没给这个字段"（命中 R010）——
   审计文件里出现这种替换等于伪造现场。

`store_quarantined` 收的是门禁的输出（`QuarantinedRow`）而不是本模块的 `Entry`，所以这里的
`draft()/row()` 两个助手就是"门禁交出什么"的形状：测试与真实调用方喂的是同一种东西。
"""

from datetime import date
from pathlib import Path
from typing import Any

import duckdb

from tests.fakes import bar
from zhixing_quant.domain.bar import BarDraft
from zhixing_quant.quality.engine import QuarantinedRow
from zhixing_quant.quality.facts import Violation
from zhixing_quant.storage import layout, quarantine, write
from zhixing_quant.storage.layout import Entry

#: 运行日与它的前一个交易日。一批带两日常态，所以文件名取哪个日子必须钉住（决定 2）。
RUN = date(2024, 1, 3)
PREV = date(2024, 1, 2)

DIRTY = Violation("R001", "reject", "high 低于 open/close 较高者")
OUT_OF_RANGE = Violation("R004", "reject", "不在主数据在册区间内")


def draft(**over: Any) -> BarDraft:
    """一条字段齐全、但 OHLC 逆序的草稿：默认值就是"被拒收的那行长什么样"。

    价格留成 `float`（不是 `Decimal`、不是字符串）：适配器交给门禁的就是 float，隔离区存的是它的
    `str()`，两端形状都得跟真链路一致。
    """
    base: dict[str, Any] = {
        "source": "akshare_daily",
        "symbol": "600519",
        "trade_date": PREV,
        "open": 10.0,
        "high": 9.9,
        "low": 10.1,
        "close": 10.0,
        "volume": 1000.0,
        "amount": 10000.0,
        "adj_factor": 8.0,
    }
    return BarDraft(**{**base, **over})


def row(*violations: Violation, **over: Any) -> QuarantinedRow:
    """门禁的一条拒收输出。不传规则时默认命中 R001。"""
    return QuarantinedRow(draft(**over), violations or (DIRTY,))


def file_on(root: Path, run_on: date = RUN) -> Path:
    return layout.quarantine_path(run_on, root=root)


def entries(tmp_path: Path) -> tuple[Entry, ...]:
    return quarantine.entries_on(RUN, root=tmp_path)


# --- 一条拒收变成一行 -------------------------------------------------------------


def test_the_rejected_row_lands_in_a_file_that_survives_the_process(tmp_path: Path) -> None:
    """这一步的全部意义：进程退出之后，"那天拒收了什么"还得答得出来。"""
    report = quarantine.store_quarantined([row()], RUN, root=tmp_path)
    assert (report.entries, report.recorded, report.rewritten) == (1, 1, 1)

    (entry,) = entries(tmp_path)
    assert entry.source == "akshare_daily"
    assert entry.symbol == "600519"
    assert entry.trade_date == PREV
    assert (entry.open, entry.high, entry.low, entry.close) == ("10.0", "9.9", "10.1", "10.0")
    assert (entry.volume, entry.amount, entry.adj_factor) == ("1000.0", "10000.0", "8.0")
    assert entry.rules == ("R001",)


def test_a_nan_price_stays_a_different_answer_from_a_missing_one(tmp_path: Path) -> None:
    """`nan` 与 `None` 必须读得出区别：前者命中 R001，后者命中 R010。

    这是隔离区价格列存 `VARCHAR` 的唯一理由，所以它要在**盘上**成立，而不是只在 `Entry` 里成立。
    """
    quarantine.store_quarantined(
        [row(close=float("nan")), row(symbol="600520", close=None)], RUN, root=tmp_path
    )
    got = {e.symbol: e.close for e in entries(tmp_path)}
    assert got == {"600519": "nan", "600520": None}
    with duckdb.connect() as con:
        still_nan: int = con.execute(
            "SELECT count(*) FROM read_parquet(?) WHERE isnan(TRY_CAST(close AS DOUBLE))",
            [str(file_on(tmp_path))],
        ).fetchone()[0]
    assert still_nan == 1


def test_the_three_arrays_describe_the_same_violations(tmp_path: Path) -> None:
    """一行之内三条数组按位对齐：`rules[i]/levels[i]/reasons[i]` 说的是同一次命中。

    多规则同行是常态（缺字段与逆序会一起命中），数组漂开一位，日报抽样的那 5 条就张冠李戴。
    """
    quarantine.store_quarantined([row(DIRTY, OUT_OF_RANGE)], RUN, root=tmp_path)
    (entry,) = entries(tmp_path)
    assert entry.rules == ("R001", "R004")
    assert entry.levels == ("reject", "reject")
    assert entry.reasons == (DIRTY.reason, OUT_OF_RANGE.reason)


def test_the_code_is_kept_exactly_as_the_source_gave_it(tmp_path: Path) -> None:
    """存原样而不是规范形：归一不出来正是有些行的罪名。

    `draft.code` 遇到 `sh600519` 会抛，所以在落盘前调它一次，就够让整批拒收条目变成异常——审计的
    现场反而没了。
    """
    quarantine.store_quarantined(
        [row(symbol="sh600519"), row(symbol="600519.SH")], RUN, root=tmp_path
    )
    assert {e.symbol for e in entries(tmp_path)} == {"sh600519", "600519.SH"}


def test_the_run_day_file_holds_the_whole_batch(tmp_path: Path) -> None:
    """上一日那行也在**这次运行**的文件里：它同样是今天抓到的证据（ADR-0008 代价二）。

    交易日为 `None` 的行根本没有别的文件可去，所以文件名只能回答"哪一次运行拒收了这些"；
    而"2024-01-02 那天跑的时候拒收了什么"仍然是读一个文件，不必扫目录。
    """
    quarantine.store_quarantined([row(), row(trade_date=RUN)], RUN, root=tmp_path)
    assert {e.trade_date for e in entries(tmp_path)} == {PREV, RUN}
    assert not file_on(tmp_path, PREV).exists()  # 交易日不是文件名


# --- 幂等：整行相等才算重复 -------------------------------------------------------


def test_a_rerun_adds_nothing_and_touches_nothing(tmp_path: Path) -> None:
    """重跑一遍，磁盘上什么都没发生——连 mtime 都没动。

    这条同时钉住读回的归一化：Parquet 把三个 LIST 列还给 Python 的是 `list`，不变回 `tuple` 的话
    "盘上那条"与"这次算出来的那条"永不相等，每次重跑都会把整个文件再记一遍。
    """
    rows = [row(), row(symbol="600520")]
    quarantine.store_quarantined(rows, RUN, root=tmp_path)
    stamp = file_on(tmp_path).stat().st_mtime_ns

    report = quarantine.store_quarantined(rows, RUN, root=tmp_path)
    assert (report.entries, report.recorded, report.rewritten) == (2, 0, 0)
    assert file_on(tmp_path).stat().st_mtime_ns == stamp


def test_the_same_day_with_two_different_values_keeps_both(tmp_path: Path) -> None:
    """干净区会为这个形状抛 `BarConflict`，隔离区两条都留。

    同一天、同一条规则、原始值不同，说的是"源先后给了两种东西"——那是要保留的现象本身，裁决它就
    等于销毁证据的一半。
    """
    first = quarantine.store_quarantined([row(close=10.0)], RUN, root=tmp_path)
    second = quarantine.store_quarantined([row(close=10.5)], RUN, root=tmp_path)
    assert (first.recorded, second.recorded) == (1, 1)
    assert {e.close for e in entries(tmp_path)} == {"10.0", "10.5"}


def test_a_partial_rerun_records_only_the_new_row(tmp_path: Path) -> None:
    """日报那句"新记 1/2 条"的证据：账要分得清"这次交来几条"与"其中几条是新的"。"""
    quarantine.store_quarantined([row()], RUN, root=tmp_path)
    report = quarantine.store_quarantined([row(), row(OUT_OF_RANGE)], RUN, root=tmp_path)
    assert (report.entries, report.recorded, report.rewritten) == (2, 1, 1)
    assert len(entries(tmp_path)) == 2


def test_the_same_row_twice_in_one_batch_lands_once(tmp_path: Path) -> None:
    """源把同一条坏行交来两遍（重抓、或者帧里本来就重复），盘上仍是一条。

    账要诚实：`entries=2` 是"交来几条"，`recorded=1` 是"盘上真多了几条"，两个数各自成立，
    合在一起才是日报那句"新记 1/2 条"。
    """
    report = quarantine.store_quarantined([row(), row()], RUN, root=tmp_path)
    assert (report.entries, report.recorded) == (2, 1)
    assert len(entries(tmp_path)) == 1


def test_a_row_without_a_date_sits_next_to_one_with(tmp_path: Path) -> None:
    """缺日期是 R010 的常客，它必须能和正常行同盘。

    排序键因此不能直接用 `trade_date`（`None` 与 `date` 不可比），否则整批落盘会炸在 `sorted` 上，
    而那正是隔离区最该记下来的那种行。
    """
    report = quarantine.store_quarantined([row(trade_date=None), row()], RUN, root=tmp_path)
    assert report.recorded == 2
    assert {e.trade_date for e in entries(tmp_path)} == {None, PREV}


# --- 文件形状与不残留 -------------------------------------------------------------


def test_an_empty_batch_creates_no_directory_and_no_file(tmp_path: Path) -> None:
    """干净的几天不该在数据根里长出 `quarantine/`：那会让"今天有没有拒收"看起来像个目录问题。"""
    report = quarantine.store_quarantined([], RUN, root=tmp_path)
    assert report == quarantine.QuarantineReport(entries=0, recorded=0, rewritten=0)
    assert entries(tmp_path) == ()  # 文件不在就是空，不抛：那几天确实什么都没有
    assert not tmp_path.exists() or list(tmp_path.rglob("*")) == []


def test_no_partial_file_is_left_behind(tmp_path: Path) -> None:
    """`.part` 只是 rename 前的落脚点：留在盘上说明原子替换没走完，而读者会 glob 到它。"""
    quarantine.store_quarantined([row()], RUN, root=tmp_path)
    quarantine.store_quarantined([row(symbol="600520")], RUN, root=tmp_path)
    assert list(tmp_path.rglob("*.part")) == []


def test_the_two_datasets_keep_their_own_declared_shapes(tmp_path: Path) -> None:
    """列名与列型是格式的一部分：改了它，去年那份文件就换了一种读法。

    `hive_partitioning=false` 不是可有可无的开关：DuckDB 1.x 默认按目录名推断 hive 分区，
    `year=2024` 会凭空多出一列，那时这份断言测的是目录命名而不是文件形状。两个 dataset 的读法
    本来也不同——干净区按 `symbol=` 分区，隔离区一天一个文件平铺。
    """
    write.store_bars([bar(PREV)], root=tmp_path)
    quarantine.store_quarantined([row()], RUN, root=tmp_path)
    assert _columns(layout.partition_path("600519", 2024, root=tmp_path)) == list(layout.COLUMNS)
    assert _columns(file_on(tmp_path)) == list(layout.QUARANTINE_COLUMNS)


def test_the_quarantine_answers_a_plain_sql_question(tmp_path: Path) -> None:
    """「全量可查」（04 §四）的读法不该要求本项目的代码：一条 SQL 就要能问出那天拒收了哪些规则。"""
    quarantine.store_quarantined(
        [row(), row(symbol="600520"), row(OUT_OF_RANGE, symbol="600521")], RUN, root=tmp_path
    )
    with duckdb.connect() as con:
        rows = con.execute(
            "SELECT symbol, rules FROM read_parquet(?) ORDER BY symbol", [str(file_on(tmp_path))]
        ).fetchall()
    assert rows == [("600519", ["R001"]), ("600520", ["R001"]), ("600521", ["R004"])]


def _columns(path: Path) -> list[tuple[str, str]]:
    """文件的列名与列型，按文件里的顺序——格式的可读部分，两份 `*_COLUMNS` 的对照物。"""
    with duckdb.connect() as con:
        described = con.execute(
            "DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning=false)", [str(path)]
        ).fetchall()
    return [(str(line[0]), str(line[1])) for line in described]
