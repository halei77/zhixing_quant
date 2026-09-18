"""源行取值归一（03 §二 L2）。每个源都过这一层，所以它单独测：源特有适配器只测映射。

核心取向只有一句：**归一不出来就留 None，交给门禁判**（04 §二 R010）。因此这里的断言
一半在测"None"，另一半在测"没被清洗成看起来有值"——NaN 必须还是 NaN，布尔必须变 None。
"""

import math
from datetime import date, datetime

import pytest

from zhixing_quant.sources.rows import pick, to_date, to_float


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("-", None),
        ("1685.01", 1685.01),
        (1685, 1685.0),
        (True, None),  # 布尔出现在数值列里说明列映射错了，让它缺着被门禁抓
        (object(), None),  # 没见过的类型不硬转，也不抛
    ],
)
def test_to_float_reads_numbers(value: object, expected: float | None) -> None:
    assert to_float(value) == expected


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_to_float_keeps_non_finite_instead_of_cleaning_it_away(value: float) -> None:
    """NaN 是 CSV 空值最常见的形状：归成 None 会让门禁报"字段没给"，而不是"源给了坏值"。"""
    got = to_float(value)
    assert got is not None and not math.isfinite(got)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2024, 1, 2), date(2024, 1, 2)),
        (datetime(2024, 1, 2, 15, 0), date(2024, 1, 2)),
        ("2024-01-02", date(2024, 1, 2)),
        ("2024/01/02", date(2024, 1, 2)),  # 新浪用斜杠
        ("2024-01-02 15:00:00", date(2024, 1, 2)),
        (" 2024-01-02 ", date(2024, 1, 2)),  # 源爱在数字两侧塞空格
        ("20240102", date(2024, 1, 2)),  # ISO basic：腾讯/东财历史接口原样给这个
        ("not-a-date", None),
        ("2024-13-02", None),
        (1704153600, None),  # epoch 秒不猜时区与单位
        (None, None),
    ],
)
def test_to_date_normalizes_every_shape(value: object, expected: date | None) -> None:
    """datetime 必须降回 date：留着时分秒，(symbol, trade_date) 主键就成两个字段了。"""
    assert to_date(value) == expected


def test_pick_takes_the_first_alias_that_exists() -> None:
    """同一列有三种叫法（证券代码/A股代码/code），但顺序必须是"源自己的名字在前"。"""
    row = {"code": "600519", "证券代码": "X"}
    assert pick(row, "code", "证券代码") == "600519"
    assert pick(row, "证券代码", "code") == "X"


def test_pick_does_not_invent_a_value_for_a_missing_column() -> None:
    """列不在 → None：这是协议问题（源改名列了），和"列在而值为空"的数据问题到此分开。"""
    assert pick({"close": None}, "close") is None
    assert pick({"close": None}, "open", "high") is None
