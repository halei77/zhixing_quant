"""akshare 日线适配器：源行 → BarDraft 的映射（03 §二 L2；04 §五 第 1 项）。

取值归一（`to_float`/`to_date`）本身在 `tests/sources/test_rows.py` 测；这里只测源特有的
那一半：哪一列进哪一个字段、两帧怎么对齐。

黄金样本（真实响应快照 + 期望解析结果）要用户批准后才能进仓（05 Q1-③），这里先用
**合成行**覆盖映射逻辑：列名与行形状同 `stock_zh_a_daily`，值取自 2024-01-02 贵州茅台
的真实区间（不复权收盘 1685.01、后复权收盘 13601.13）。

重点测两类"看起来正常"的失效：源改了列名时适配器不能抛 KeyError 把采集炸成半批入库；
源给了 NaN 时不能替它洗成 None。
"""

import math
from datetime import date
from typing import Any

import pytest

from zhixing_quant.sources.akshare.daily import SOURCE, daily_drafts

#: `stock_zh_a_daily(adjust="")` 的一行（本层用不到的 outstanding_share/turnover 省掉）。
RAW_ROW: dict[str, Any] = {
    "date": date(2024, 1, 2),
    "open": 1715.00,
    "high": 1718.19,
    "low": 1678.10,
    "close": 1685.01,
    "volume": 3215644.0,
    "amount": 5440082548.0,
}
HFQ_ROW: dict[str, Any] = {**RAW_ROW, "close": 13601.13}


def _rows(**over: Any) -> list[dict[str, Any]]:
    return [{**RAW_ROW, **over}]


def test_maps_every_field_and_derives_the_factor() -> None:
    (draft,) = daily_drafts(_rows(), [HFQ_ROW], symbol="sh600519")
    assert (draft.source, draft.trade_date) == (SOURCE, date(2024, 1, 2))
    assert draft.symbol == "sh600519"  # 代码归一化属于契约层（Bar/GateInconsistency），不在这里做
    assert (draft.open, draft.high, draft.low, draft.close) == (1715.0, 1718.19, 1678.1, 1685.01)
    assert (draft.volume, draft.amount) == (3215644.0, 5440082548.0)
    assert draft.adj_factor == pytest.approx(13601.13 / 1685.01)
    assert draft.is_suspended is False


def test_hfq_frame_is_aligned_by_date_not_by_row_number() -> None:
    """后复权帧少给一天时，不能把别日的因子按行号顺移过来。

    顺移的结果是一条"合法"的行：OHLC 全对、量也全对，只有复权口径错位，等到回测里才以
    "某只票某天突然涨了 8 倍"的形式暴露——那时已经查不到是哪一批数据了。
    """
    other_day = [{"date": date(2024, 1, 3), "close": 13673.69}]
    assert daily_drafts(_rows(), other_day, symbol="600519")[0].adj_factor is None


@pytest.mark.parametrize(
    ("raw", "hfq"),
    [
        ({"close": None}, HFQ_ROW),  # 没有分母
        ({}, {**HFQ_ROW, "close": None}),  # 后复权列被改名：没有分子
        ({"close": 0.0}, HFQ_ROW),  # 除以 0 不是"因子无穷大"，是判不了
        ({"close": float("nan")}, HFQ_ROW),
        ({}, {**HFQ_ROW, "close": float("inf")}),
        ({"close": -1685.01}, HFQ_ROW),  # 负价格配负因子：符号两消会变成"合法"的正因子
    ],
)
def test_no_factor_instead_of_a_fabricated_one(raw: dict[str, Any], hfq: dict[str, Any]) -> None:
    """因子缺就缺着（R005 报"没有因子"），不用 1.0 兜底。

    1.0 的语义是"这只票从没除权过"——用它兜底等于把一次抓取事故写成一条历史结论，
    回测会照着这个结论算收益率。
    """
    (draft,) = daily_drafts(_rows(**raw), [hfq], symbol="600519")
    assert draft.adj_factor is None


def test_renamed_columns_become_missing_fields_not_an_exception() -> None:
    """03 §二 L2"模拟源字段变更能立即测红"的落点：适配器不抛，缺的字段交给 R010。

    采集任务在适配器抛 KeyError 时会留下"半批已入库"；整批 FATAL 则什么都不写进去。
    """
    (draft,) = daily_drafts(
        [{"日期": "2024-01-02", "开盘": 1715.0, "收盘": 1685.01}], symbol="600519"
    )
    assert draft.trade_date is None
    assert set(draft.missing_fields()) == {
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    }


def test_nan_from_the_source_survives_into_the_draft() -> None:
    """NaN 是 CSV 空值最常见的形状：原样交给门禁，不在这里替它"清洗"。"""
    (draft,) = daily_drafts(_rows(volume=math.nan), symbol="600519")
    assert draft.volume is not None and math.isnan(draft.volume)


def test_source_tag_is_the_scoring_key() -> None:
    """04 §三 按源打分、04 §四 按源分行：源名是主键，改名等于给历史分数换主语。"""
    assert SOURCE == "akshare_daily"
    assert daily_drafts(_rows(), symbol="600519")[0].source == SOURCE
