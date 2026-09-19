"""幂等落盘（01 Step 3 验收 3）。

判据全部落在"文件与读回来的值"上，不测内部函数：验收说的是"重跑不产生重复数据"，
那是一句关于磁盘和查询的话。mtime 也被钉住——"没变就不写"是断点续传的第二个证据，
只数行数的话，一次悄悄重写全部 4430 个分区的改动照样过测试。

DuckDB 只在这里当"能读 Parquet 的读者"用（`peek`）：干净区不是本项目的私有格式，
备份脚本、以后的分析人员都该能直接打开它，所以有一条测试专门验证"绕开本层也读得到"。
"""

from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import pytest

from tests.fakes import bar
from zhixing_quant.storage import layout, query, write
from zhixing_quant.storage.layout import NAMES

DAY1 = date(2024, 1, 2)
DAY2 = date(2024, 1, 3)


def peek(path: Path) -> list[tuple[Any, ...]]:
    """绕开存储层，用 DuckDB 直接看文件：证明落的是标准 Parquet，不是私有格式。"""
    with duckdb.connect() as con:
        rows: list[tuple[Any, ...]] = con.execute(
            f"SELECT {', '.join(NAMES)} FROM read_parquet(?) ORDER BY trade_date",
            [str(path)],
        ).fetchall()
        return rows


def partition(tmp_path: Path, year: int = 2024, symbol: str = "600519") -> Path:
    return layout.partition_path(symbol, year, root=tmp_path)


def test_first_write_creates_the_adr_partition(tmp_path: Path) -> None:
    report = write.store_bars([bar(DAY1), bar(DAY2)], root=tmp_path)
    assert (report.partitions, report.added, report.repaired, report.rewritten) == (1, 2, 0, 1)
    assert partition(tmp_path).is_file()


def test_empty_batch_touches_nothing(tmp_path: Path) -> None:
    """空批不建目录：定时任务连着几天交白卷时，数据根里不该长出 `data/daily/` 假装有数据。"""
    report = write.store_bars([], root=tmp_path)
    assert report == write.WriteReport(0, 0, 0, 0)
    assert not tmp_path.exists() or list(tmp_path.rglob("*")) == []


def test_a_second_identical_run_writes_no_file(tmp_path: Path) -> None:
    """断点续传的全部意义：重跑一遍，磁盘上什么都没发生（mtime 都没动）。"""
    bars = [bar(DAY1), bar(DAY2)]
    write.store_bars(bars, root=tmp_path)
    stamp = partition(tmp_path).stat().st_mtime_ns

    report = write.store_bars(bars, root=tmp_path)
    assert (report.added, report.repaired, report.rewritten) == (0, 0, 0)
    assert partition(tmp_path).stat().st_mtime_ns == stamp


def test_a_repeated_day_stays_one_row(tmp_path: Path) -> None:
    """验收 3 的字面判据：同一天的行交两次，文件里仍是一天一行。"""
    write.store_bars([bar(DAY1), bar(DAY1, close=10.0)], root=tmp_path)
    assert [row[2] for row in peek(partition(tmp_path))] == [DAY1]


def test_refetch_repairs_a_day(tmp_path: Path) -> None:
    """后到覆盖先到：重抓是修错数据唯一可行的手段，"已有就不动"会让错日子永久留在干净区。"""
    write.store_bars([bar(DAY1), bar(DAY2, close=99.0)], root=tmp_path)
    report = write.store_bars([bar(DAY2, close=11.0)], root=tmp_path)
    assert (report.added, report.repaired) == (0, 1)
    assert [(row[2], row[6]) for row in peek(partition(tmp_path))] == [(DAY1, 10.0), (DAY2, 11.0)]


def test_a_batch_with_two_values_for_one_day_is_a_conflict(tmp_path: Path) -> None:
    """同一天两个不同的值不是新旧，是跨源没裁决完。存储层不猜，直接抛。"""
    with pytest.raises(write.BarConflict, match="不替它们裁决谁对"):
        write.store_bars([bar(DAY1, close=10.0), bar(DAY1, close=10.5)], root=tmp_path)


def test_a_conflict_leaves_nothing_behind(tmp_path: Path) -> None:
    """抛在写文件之前：炸成"半批已入库"比抛错本身更糟，那正是适配器不用 KeyError 的理由。"""
    with pytest.raises(write.BarConflict):
        write.store_bars(
            [bar(DAY1), bar(DAY2, symbol="600520"), bar(DAY2, symbol="600520", close=1.0)],
            root=tmp_path,
        )
    assert list(tmp_path.rglob("*.parquet")) == []


def test_two_sources_of_the_same_day_conflict(tmp_path: Path) -> None:
    """`source` 是列不是键：一天两行会让回测把成交量算两遍，所以跨源得先在门禁里对齐。"""
    with pytest.raises(write.BarConflict):
        write.store_bars(
            [bar(DAY1, source="akshare_daily"), bar(DAY1, source="tencent_daily", close=10.1)],
            root=tmp_path,
        )


def test_a_batch_spreads_across_symbols_and_years(tmp_path: Path) -> None:
    bars = [
        bar(DAY1),
        bar(date(2023, 12, 29)),
        bar(DAY1, symbol="600520"),
    ]
    report = write.store_bars(bars, root=tmp_path)
    assert report.partitions == 3 and report.added == 3
    assert sorted(p.parent.name for p in tmp_path.rglob("*.parquet")) == [
        "year=2023",
        "year=2024",
        "year=2024",
    ]


def test_missing_factor_and_suspension_survive_the_round_trip(tmp_path: Path) -> None:
    """None 必须是 None：落成 0.0 会变成"因子为零"，落成 1.0 会把"没抓到 hfq"洗成"没除过权"。"""
    write.store_bars(
        [bar(DAY1, factor=None), bar(DAY2, factor=None, suspended=True)], root=tmp_path
    )
    rows = peek(partition(tmp_path))
    assert [row[9] for row in rows] == [None, None]
    assert [row[10] for row in rows] == [False, True]
    loaded = query.read_bars("600519", DAY1, DAY2, root=tmp_path)
    assert [b.adj_factor for b in loaded] == [None, None]
    assert [b.is_suspended for b in loaded] == [False, True]


def test_rows_are_stored_in_date_order(tmp_path: Path) -> None:
    """落盘顺序固定为交易日升序：不然"内容没变"的比较会因插入顺序而误判，每次重跑都重写文件。"""
    write.store_bars([bar(DAY2), bar(DAY1), bar(date(2024, 1, 4))], root=tmp_path)
    assert [row[2] for row in peek(partition(tmp_path))] == [DAY1, DAY2, date(2024, 1, 4)]


def test_a_hand_duplicated_file_is_collapsed_on_next_write(tmp_path: Path) -> None:
    """一天两行不该存在。下一次落盘顺手收掉，而不是把坏形状一直带下去。"""
    write.store_bars([bar(DAY1), bar(DAY2)], root=tmp_path)
    path = partition(tmp_path)
    with duckdb.connect() as con:
        con.execute("CREATE TABLE dup AS SELECT * FROM read_parquet(?)", [str(path)])
        con.execute("INSERT INTO dup SELECT * FROM dup")
        columns = ", ".join(NAMES)
        con.execute(
            f"COPY (SELECT {columns} FROM dup) TO ? (FORMAT PARQUET)", [str(path) + ".swap"]
        )
    Path(f"{path}.swap").replace(path)
    assert len(peek(path)) == 4

    report = write.store_bars([bar(date(2024, 1, 5))], root=tmp_path)
    assert report.rewritten == 1
    assert [row[2] for row in peek(path)] == [DAY1, DAY2, date(2024, 1, 5)]


def test_no_partial_file_is_left_behind(tmp_path: Path) -> None:
    """`.part` 只是 rename 前的落脚点：留在盘上就说明原子替换没走完，而读者会 glob 到它。"""
    write.store_bars([bar(DAY1)], root=tmp_path)
    assert list(tmp_path.rglob("*.part")) == []


def test_the_clean_zone_is_readable_without_this_layer(tmp_path: Path) -> None:
    """备份与审计的前提：绕开本层，列名也是自解释的（ADR-0003 的"文件可直接归档"）。

    `hive_partitioning=false` 不是可有可无的开关：DuckDB 1.x 默认按目录名推断 hive 分区，
    `year=2024` 会凭空多出一列，那时这份断言测的是目录命名而不是文件形状。
    """
    write.store_bars([bar(DAY1)], root=tmp_path)
    with duckdb.connect() as con:
        described = con.execute(
            "DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning=false)",
            [str(partition(tmp_path))],
        ).fetchall()
    assert [row[0] for row in described] == list(NAMES)
