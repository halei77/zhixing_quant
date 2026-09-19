"""akshare 交易日历适配器（03 §二 L2；04 §五 第 2 项）。

这里测的重心是"坏行为什么必须整批拒收"：日线少一个字段只丢一行，日历少一天丢的是判据。
所以每个断言都在问同一件事——出错时它有没有可能表现为"全部通过"。
"""

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from zhixing_quant.sources.akshare import calendar as ac
from zhixing_quant.sources.akshare.calendar import calendar_from_rows
from zhixing_quant.sources.rows import SourceSchemaError

# sina `tool_trade_date_hist_sina` 的实际形状：单列、值可能是 date 也可能是 str。
ROWS: list[dict[str, object]] = [
    {"trade_date": date(2024, 1, 2)},
    {"trade_date": "2024-01-03"},
    {"trade_date": "2024/01/04"},
]


def test_reads_the_sina_shape_and_answers_the_calendar_questions() -> None:
    calendar = calendar_from_rows(ROWS)
    assert calendar.is_trading_day(date(2024, 1, 3))
    assert calendar.prev_trading_day(date(2024, 1, 4)) == date(2024, 1, 3)
    assert calendar.days[0] == date(2024, 1, 2)  # 乱序输入也要给升序


def test_out_of_order_and_duplicate_rows_are_not_a_problem() -> None:
    """源按倒序返回、同一天给两次：都不构成事实冲突，日历按集合装。"""
    calendar = calendar_from_rows([*reversed(ROWS), ROWS[0]])
    assert len(calendar.days) == 3


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"trade_date": ""}],
        [{"trade_date": None}],
        [{"trade_date": "not-a-date"}],
        [{"trade_date": 1704153600}],
        [{"交易日": "2024-01-02"}],  # 列名不在别名表里 = 取不到 = 这一行解析不出来
    ],
    ids=["empty", "blank", "null", "garbage", "epoch", "renamed-column"],
)
def test_an_unjudgeable_calendar_is_rejected_wholesale(rows: list[dict[str, object]]) -> None:
    """宁可不装，也不装一份"少了几天"的日历。

    少了那几天，R006 就在那几天上永久失明：真实数据缺那天，日报反而一片绿。空日历更糟——
    `is_trading_day` 对任何日期都答 False，整批K线看起来都不该存在。两种失效的共同点是
    "看起来正常"，所以这里只能抛，不能返回一个不完整的对象。
    """
    with pytest.raises(SourceSchemaError):
        calendar_from_rows(rows)


def test_the_rejection_names_the_row_that_broke() -> None:
    """日报要能指着一次抓取事故说"第 3 行不是日期"，而不是"日历装不起来"。"""
    with pytest.raises(SourceSchemaError) as caught:
        calendar_from_rows([{"trade_date": "2024-01-02"}, {"trade_date": "??"}])
    assert "第 1 行" in str(caught.value)


# --- 快照文件 → 日历（每日任务的离线入口）----------------------------------------


def write_calendar_csv(directory: Path, values: Sequence[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ac.SNAPSHOT_NAME
    path.write_text("trade_date\n" + "\n".join(values) + "\n", encoding="utf-8")
    return path


def test_the_snapshot_path_follows_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """快照位置只有一个来源：日历写在别处，等于第二天起每天现抓，费用与失效都不可见。"""
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))
    assert ac.snapshot_path() == tmp_path / "golden" / "tool_trade_date_hist_sina.csv"


def test_a_snapshot_file_is_read_back_into_a_calendar(tmp_path: Path) -> None:
    write_calendar_csv(tmp_path, ["2024-01-02", "2024-01-03"])
    loaded = ac.load_calendar(ac.snapshot_path(tmp_path))
    assert loaded.is_trading_day(date(2024, 1, 2))
    assert not loaded.is_trading_day(date(2024, 1, 6))  # 周六不在样本里


def test_a_missing_snapshot_says_which_file_to_fetch(tmp_path: Path) -> None:
    """快照不在是第一天就撞上的事：报错要直接给出该跑哪个工具，而不是一个 FileNotFoundError。"""
    with pytest.raises(SourceSchemaError, match="capture_golden"):
        ac.load_calendar(ac.snapshot_path(tmp_path))


def test_a_snapshot_that_stops_short_is_not_allowed_to_judge(tmp_path: Path) -> None:
    """日历不覆盖目标日时 R006 永远不会报"缺交易日"，门禁表现为"全部通过"。

    这是最坏的失效：它不响。所以 `until` 越过快照最后一天就抛，让人去重抓快照。
    """
    write_calendar_csv(tmp_path, ["2024-01-02", "2024-01-31"])
    with pytest.raises(SourceSchemaError, match="只到 2024-01-31"):
        ac.load_calendar(ac.snapshot_path(tmp_path), until=date(2024, 3, 1))
    assert ac.load_calendar(ac.snapshot_path(tmp_path), until=date(2024, 1, 31)).days
