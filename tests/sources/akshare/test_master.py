"""akshare 证券主数据适配器（03 §二 L2；04 §五 第 3 项）。

行形状取自交易所上市列表的真实列名：沪市 `证券代码/证券简称/上市日期`、深市
`A股代码/A股简称/A股上市日期`。值用 2024 年在册的真实代码与上市日。

测的两类失效：一是"整列被跳过"必须表现为拒收而不是"股票池小了一圈但没人说"；二是
板块只认代码前缀，源自己写的"板块"列不能进来当第二份事实。
"""

import pytest

from zhixing_quant.domain.symbol import Board, board_of
from zhixing_quant.sources.akshare.master import MasterLoad, listings_from_rows
from zhixing_quant.sources.rows import SourceSchemaError

SH_ROWS: list[dict[str, object]] = [
    {"证券代码": "600519", "证券简称": "贵州茅台", "上市日期": "2001-08-27", "板块": "主板"},
    {"证券代码": "688235", "证券简称": "佰维存储", "上市日期": "2022-12-30", "板块": "科创板"},
]
SZ_ROWS: list[dict[str, object]] = [
    {"A股代码": "000001", "A股简称": "平安银行", "A股上市日期": "1991-04-03"},
    {"A股代码": "300750", "A股简称": "宁德时代", "A股上市日期": "2018-06-11"},
]


def test_reads_the_shanghai_shape() -> None:
    load = listings_from_rows(SH_ROWS)
    assert load.skipped == ()
    assert [(x.code, x.name, x.listed_on.isoformat()) for x in load.listings] == [
        ("600519", "贵州茅台", "2001-08-27"),
        ("688235", "佰维存储", "2022-12-30"),
    ]


def test_reads_the_shenzhen_shape_through_the_same_entry() -> None:
    """两个交易所列名不同、形状相同：一个入口 + 别名表，比两份适配器少一处会漂的逻辑。"""
    load = listings_from_rows(SZ_ROWS)
    assert [x.code for x in load.listings] == ["000001", "300750"]
    assert load.listings[1].listed_on.isoformat() == "2018-06-11"


def test_board_comes_from_the_code_not_from_the_sources_own_column() -> None:
    """源写"主板"而代码是 300750：按代码判创业板。两份事实不一致时必须有唯一胜者。"""
    load = listings_from_rows(
        [{"证券代码": "300750", "证券简称": "宁德时代", "上市日期": "2018-06-11", "板块": "主板"}]
    )
    assert board_of(load.listings[0].code) is Board.GEM


@pytest.mark.parametrize(
    ("row", "reason_part"),
    [
        ({"证券代码": "900901", "证券简称": "市北B股", "上市日期": "1994-02-04"}, "四档板块"),
        ({"证券代码": "600519", "证券简称": "", "上市日期": "2001-08-27"}, "简称"),
        ({"证券代码": "600519", "证券简称": "贵州茅台", "上市日期": ""}, "上市日期"),
        ({"证券代码": "600519", "证券简称": "贵州茅台"}, "上市日期"),
        ({"证券简称": "贵州茅台", "上市日期": "2001-08-27"}, "四档板块"),
    ],
)
def test_a_row_that_cannot_be_registered_is_skipped_with_a_reason(
    row: dict[str, object], reason_part: str
) -> None:
    """B 股/债券不是数据错误，是范围之外：跳，但跳过的行要带着原因回来。"""
    load = listings_from_rows([*SH_ROWS, row])
    assert len(load.listings) == 2
    (dropped,) = load.skipped
    assert dropped.position == 2
    assert reason_part in dropped.reason


def test_skipped_rows_keep_their_position_and_text() -> None:
    load = listings_from_rows(
        [*SH_ROWS, {"证券代码": "400001", "证券简称": "老三板", "上市日期": ""}]
    )
    (dropped,) = load.skipped
    assert (dropped.position, dropped.raw_code) == (2, "400001")


def test_code_is_normalized_on_the_way_in() -> None:
    """源偶尔给带前后空格或市场后缀的写法，主数据里只能有一种形状。"""
    load = listings_from_rows(
        [{"证券代码": " sh600519 ", "证券简称": "贵州茅台", "上市日期": "2001-08-27"}]
    )
    assert load.listings[0].code == "600519"


def test_all_rows_rejected_is_read_as_a_schema_break_not_an_empty_universe() -> None:
    """列名全变时会跳光所有行。此时"股票池为空"和"今天有 5000 只票没登记"必须是两回事。"""
    rows = [{"证券代号": "600519", "名字": "贵州茅台", "日期": "2001-08-27"}]
    with pytest.raises(SourceSchemaError) as caught:
        listings_from_rows(rows)
    assert "四档板块" in str(caught.value)  # 原因要跟着报出来，否则无从下手


def test_empty_input_is_rejected() -> None:
    with pytest.raises(SourceSchemaError):
        listings_from_rows([])


def test_duplicates_are_left_for_the_master_to_refuse() -> None:
    """这里不去重：`SecurityMaster` 已经判过"同一代码重复登记"，判两遍迟早不一致。"""
    load = listings_from_rows([SH_ROWS[0], SH_ROWS[0]])
    assert [x.code for x in load.listings] == ["600519", "600519"]
    assert isinstance(load, MasterLoad)
