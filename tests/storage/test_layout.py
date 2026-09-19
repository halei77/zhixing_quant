"""落盘格式（ADR-0003 决定 1、ADR-0007 决定 2）。

分区路径与列形状是存储层对外的全部承诺，也是备份脚本、以后换机器读数据的人唯一能依赖
的东西，所以这里钉的是"字面形状"而不是"函数返回值自洽"：路径写错一个等号，仓库外的
一份数据就永久躺在错误的目录名下面。
"""

from datetime import date
from pathlib import Path

import pytest

from zhixing_quant import config
from zhixing_quant.domain.bar import Bar
from zhixing_quant.domain.symbol import UnknownCode
from zhixing_quant.storage import layout

#: ADR-0003 的原样形状，逐字符钉住。
ADR_0003 = "daily/year=2024/symbol=600519.parquet"


def test_partition_path_is_the_adr_layout() -> None:
    assert layout.partition_path("600519", 2024, root=Path("/d")).as_posix() == f"/d/{ADR_0003}"


def test_the_root_comes_from_config_and_nowhere_else(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """根目录只能由配置给（02 §六）：这里不传 root 时它必须落在 `ZX_DATA_ROOT/data/` 下。"""
    monkeypatch.setenv(config.ENV_VAR, str(tmp_path))
    assert layout.dataset_dir(layout.DAILY) == config.parquet_dir() / "daily"
    assert layout.partition_path("600519", 2024).parent == tmp_path / "data" / "daily" / "year=2024"


@pytest.mark.parametrize("writing", ["600519", "sh600519", "600519.SH", " sh.600519 "])
def test_every_writing_of_one_code_shares_one_partition(writing: str) -> None:
    """一只票两个路径等于两份历史：写进去之后查询读哪一份都没人说得清。"""
    assert layout.partition_path(writing, 2024, root=Path("/d")) == layout.partition_path(
        "600519", 2024, root=Path("/d")
    )


@pytest.mark.parametrize("junk", ["../evil", "60051", "symbol=600519", "select 1", ""])
def test_a_code_the_layout_cannot_normalize_never_reaches_a_path(junk: str) -> None:
    """代码可能来自命令行，所以 `normalize_code` 顺手就是路径遍历的防线：不认识的形状抛错。

    判的不是"抛不抛"这一件事，是"抛在拼路径之前"——晚一步就可能出现 `..` 已经进了
    Path、随后靠字符串清洗兜底的写法，那种代码每次评审都要重新解释一遍为什么安全。
    """
    with pytest.raises(UnknownCode):
        layout.partition_path(junk, 2024, root=Path("/d"))


def test_a_window_spans_every_year_it_touches() -> None:
    paths = layout.partitions("600519", date(2022, 12, 30), date(2024, 1, 3), root=Path("/d"))
    assert [p.parent.name for p in paths] == ["year=2022", "year=2023", "year=2024"]


def test_existing_partitions_sort_by_year_and_ignore_other_symbols(tmp_path: Path) -> None:
    """`glob` 的次序不保证，年份又是零填充的定长串：不显式排序，因子阶梯的顺序就靠运气。"""
    for year in (2024, 2021, 2023):
        path = layout.partition_path("600519", year, root=tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        other = layout.partition_path("600520", year, root=tmp_path)
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"")
    found = layout.existing_partitions("600519", root=tmp_path)
    assert [layout.year_of(p) for p in found] == [2021, 2023, 2024]


def test_a_symbol_never_written_reads_as_no_partitions(tmp_path: Path) -> None:
    """目录还不存在不是错误：查询空票与查询不存在的目录是同一件事——没有数据。"""
    assert layout.existing_partitions("600519", root=tmp_path / "not-yet") == ()


def test_columns_are_the_bar_fields_and_the_order_is_the_record_order() -> None:
    """列清单与 `Record` 形状一一对应：加一列时漏改一处，写和读会各自按不同顺序解读同一份文件。"""
    assert tuple(name for name, _ in layout.COLUMNS) == layout.NAMES
    assert set(layout.NAMES) == set(Bar.model_fields)


def test_record_of_follows_the_column_order() -> None:
    bar = Bar(
        source="akshare_daily",
        symbol="sh600519",
        trade_date=date(2024, 1, 2),
        open=10.0,
        high=11.0,
        low=9.0,
        close=10.5,
        volume=1000.0,
        amount=10500.0,
        adj_factor=8.0,
        is_suspended=True,
    )
    assert layout.record_of(bar) == (
        "akshare_daily",
        "600519",
        date(2024, 1, 2),
        10.0,
        11.0,
        9.0,
        10.5,
        1000.0,
        10500.0,
        8.0,
        True,
    )
