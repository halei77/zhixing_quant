"""akshare 证券主数据适配器（03 §二 L2；04 §五 第 3 项）。

行形状取自交易所上市列表的真实列名：沪市 `证券代码/证券简称/上市日期`、深市
`A股代码/A股简称/A股上市日期`。值用 2024 年在册的真实代码与上市日。

测的两类失效：一是"整列被跳过"必须表现为拒收而不是"股票池小了一圈但没人说"；二是
板块只认代码前缀，源自己写的"板块"列不能进来当第二份事实。

ST 帽是第三类，形状不一样：模型早就支持 ST 区间、R004 也真在读它，缺的是**没人往里填**
（坑 #19）。那种缺口的表现是"规则永远放行"，与"这条规则没必要"在日报上无法区分，所以
这里钉的是"名称前缀 → 一段有时点的 ST 区间"这条链，以及时点之外一律不判。
"""

from collections.abc import Mapping
from datetime import date
from pathlib import Path

import pytest

from zhixing_quant.domain.symbol import Board, board_of
from zhixing_quant.sources.akshare import master as am
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


# --- 快照文件 → 主数据（每日任务的离线入口）--------------------------------------


_HEADERS = {
    0: "证券代码,证券简称,上市日期",  # SH 主板
    1: "A股代码,A股简称,A股上市日期",  # SZ
    2: "证券代码,证券简称,上市日期",  # SH 科创板（列形与主板同）
    3: "ts_code,name,list_date",  # relay stock_basic 那份 BSE
}


def write_listings(tmp_path: Path, index: int, body: str) -> None:
    """只写 `index` 那一份。主数据 2026-09-22 扩成四份后，read_master 缺任何一份都拒收——
    要一次写全的用 `write_listings_all`。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / f"{am.SNAPSHOT_NAMES[index]}.csv").write_text(
        f"{_HEADERS[index]}\n{body}\n", encoding="utf-8"
    )


def write_listings_all(tmp_path: Path, bodies: Mapping[int, str]) -> None:
    """四份名单全写：`bodies` 给到的装内容，缺的只给表头（零行）——
    夹具不许再造"两份名单"的老形状，那形状现在过不了 read_master 的完整性检查。"""
    for i in range(len(am.SNAPSHOT_NAMES)):
        body = bodies.get(i, "")
        write_listings(tmp_path, i, body)


#: 快照的抓取日：ST 帽那段区间的起点，只能来自这里。
CAPTURED = date(2024, 1, 3)


def write_manifest(tmp_path: Path, days: Mapping[str, date] | None = None) -> None:
    """`tools/capture_golden.py` 写的那份清单，只留 ST 判定真正要读的 `captured_at`。

    省略 `days` 就是两份名单同一天抓的——那是这个工具的正常产出。
    """
    observed = dict.fromkeys(am.SNAPSHOT_NAMES, CAPTURED) if days is None else days
    lines = ["key,file,rows,columns,captured_at,status,detail"]
    lines += [
        f"{name},{name}.csv,2,,{day.isoformat()}T08:56:05,ok," for name, day in observed.items()
    ]
    (tmp_path / am.MANIFEST_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_the_snapshot_paths_follow_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))
    assert [p.parent.name for p in am.snapshot_paths()] == ["golden"] * 4
    assert [p.name for p in am.snapshot_paths()] == [f"{n}.csv" for n in am.SNAPSHOT_NAMES]


def test_read_master_merges_both_exchange_lists(tmp_path: Path) -> None:
    """两个入口迟早被用成一个：合读一份"那天在册的全市场"，才不会再出现少一半票的日报。"""
    write_listings_all(
        tmp_path,
        {0: "600519,贵州茅台,2001-08-27", 1: "300750,宁德时代,2018-06-11"},
    )
    write_manifest(tmp_path)
    load = am.read_master(tmp_path)
    assert [x.code for x in load.listings] == ["600519", "300750"]
    assert load.skipped == ()
    assert load.st_periods == ()  # 没人挂帽，就没有 ST 区间——这与"没判"是两件事，见下面的拒收测试


def test_a_single_missing_list_is_rejected_not_partial(tmp_path: Path) -> None:
    """只有三份名单在，看起来"还能用"：缺的那一份的票凭空消失而健康分照样满分，所以拒收。"""
    write_listings_all(tmp_path, dict.fromkeys(range(4), "600519,贵州茅台,2001-08-27"))
    write_manifest(tmp_path)
    (tmp_path / f"{am.SNAPSHOT_NAMES[3]}.csv").unlink()
    with pytest.raises(SourceSchemaError, match="没有主数据快照"):
        am.read_master(tmp_path)


def test_skips_survive_the_round_trip_from_a_file(tmp_path: Path) -> None:
    """跳过的行必须一路带到日报：B 股/债券不入库是对的，"对地少了 3 行"得说得出原文。"""
    write_listings_all(
        tmp_path,
        {
            0: "600519,贵州茅台,2001-08-27\n900901,B股示例,1992-02-21",
            1: "300750,宁德时代,2018-06-11",
        },
    )
    write_manifest(tmp_path)
    load = am.read_master(tmp_path)
    assert [(x.raw_code, x.position) for x in load.skipped] == [("900901", 1)]


def test_a_hatted_name_becomes_an_st_period_from_the_snapshot_day(tmp_path: Path) -> None:
    """真快照里 188 只票的简称挂着 ST 帽，而它们一段 ST 区间都没有（坑 #19）。

    帽的生效日期只能取自快照的抓取时间，所以区间是 `[抓取日, 持续中]`：往前不判（帽可能是那天
    之后才戴上的），往后判到下一次抓取为止（摘帽没有源可查，但下一次重抓会把它截断）。
    """
    write_listings_all(
        tmp_path,
        {
            0: "600519,贵州茅台,2001-08-27\n600119,*ST长投,1998-01-15",
            1: "000016,*ST康佳A,1992-03-27\n000001,平安银行,1991-04-03",
        },
    )
    write_manifest(tmp_path)
    master = am.read_master(tmp_path).to_master()
    assert master.st_on("000016", CAPTURED) and master.st_on("600119", CAPTURED)
    assert not master.st_on("600519", CAPTURED) and not master.st_on("000001", CAPTURED)
    assert master.state_on("000016", CAPTURED).is_st  # R004 读的就是这个字段
    # 快照日之前是"不知道"，不是"知道它非 ST"：R004 因此按非 ST 档判它，04 §二 写明了这条上限
    assert not master.st_on("000016", date(2024, 1, 2))


def test_the_hat_stays_on_until_the_next_capture(tmp_path: Path) -> None:
    """快照之后它就摘帽了怎么办？——等下一次抓取来截断这段区间，不在这里替源担保。

    这条也是"主数据要定期重抓"被写进代码的地方：ST 档的上限只对最近一次抓取负责。
    """
    write_listings_all(
        tmp_path,
        {0: "600119,*ST长投,1998-01-15", 1: "000001,平安银行,1991-04-03"},
    )
    write_manifest(tmp_path)
    master = am.read_master(tmp_path).to_master()
    assert master.st_on("600119", date(2024, 6, 30))
    assert not master.st_on("000001", date(2024, 6, 30))


def test_the_newer_capture_is_the_only_one_we_vouch_for(tmp_path: Path) -> None:
    """两份名单抓取日不同时，深市那份的 ST 票也从**最晚**那天起算。

    深市那份是 2023-10-01 抓的，此后这只票的名字我们没见过：替它把区间提前到那天，就是判那些
    并没有证据的日子。少判几天看得见（日报上是按宽档判的），多判看不见。
    """
    write_listings_all(
        tmp_path,
        {0: "600119,*ST长投,1998-01-15", 1: "000016,*ST康佳A,1992-03-27"},
    )
    write_manifest(
        tmp_path,
        {
            am.SNAPSHOT_NAMES[0]: date(2024, 1, 3),
            am.SNAPSHOT_NAMES[1]: date(2023, 10, 1),
            am.SNAPSHOT_NAMES[2]: date(2024, 1, 3),
            am.SNAPSHOT_NAMES[3]: date(2024, 1, 3),
        },
    )
    master = am.read_master(tmp_path).to_master()
    assert master.st_on("000016", date(2024, 1, 2)) is False
    assert master.st_on("000016", date(2024, 1, 3)) and master.st_on("600119", date(2024, 1, 3))


def test_a_manifest_without_both_capture_days_is_refused(tmp_path: Path) -> None:
    """清单在而两份名单的抓取时间不齐：ST 帽从哪天起算判不出来，而"判不出来"与"没有 ST 票"同形。"""
    write_listings(tmp_path, 0, "600119,*ST长投,1998-01-15")
    write_listings(tmp_path, 1, "000016,*ST康佳A,1992-03-27")
    write_manifest(tmp_path, {am.SNAPSHOT_NAMES[0]: CAPTURED})
    with pytest.raises(SourceSchemaError, match="抓取时间"):
        am.read_master(tmp_path)


def test_a_snapshot_without_a_manifest_is_refused(tmp_path: Path) -> None:
    """缺清单和缺名单是同一种病：一个判不了的时点，不该由读快照的人替它编一个。"""
    write_listings(tmp_path, 0, "600119,*ST长投,1998-01-15")
    write_listings(tmp_path, 1, "000016,*ST康佳A,1992-03-27")
    with pytest.raises(SourceSchemaError, match="没有抓取清单"):
        am.read_master(tmp_path)


@pytest.mark.parametrize(
    ("name", "hatted"),
    [
        ("ST人福", True),
        ("*ST九鼎", True),
        ("SST前锋", True),
        ("S*ST聚友", True),
        (" st华幸 ", True),
        ("贵州茅台", False),
        ("宁德时代", False),
        ("某某ST", False),
    ],
)
def test_the_hat_is_read_off_the_front_of_the_name(name: str, hatted: bool) -> None:
    """只认前缀：字母 ST 出现在名字中间或末尾的，都不是风险警示帽。"""
    assert am.is_st_name(name) is hatted
