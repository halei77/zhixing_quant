"""K线契约（01 Step 1 验收 1；03 §二 L1"合法值通过 + 每类非法值各一个反例"）。

`BarDraft` 存在的理由也在这里测：脏数据必须能被装进对象，否则门禁无从判起，
隔离区永远是空的（04 §一）。
"""

from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from zhixing_quant.domain.bar import Bar, BarDraft, nonpositive_prices, ohlc_violations
from zhixing_quant.domain.symbol import Board

CLEAN: dict[str, Any] = {
    "symbol": "600519",
    "trade_date": date(2024, 1, 2),
    "open": 10.0,
    "high": 11.0,
    "low": 9.5,
    "close": 10.5,
    "volume": 1000.0,
    "amount": 10500.0,
}


def _draft(**over: Any) -> BarDraft:
    return BarDraft.model_validate({**CLEAN, **over})


def _bar(**over: Any) -> Bar:
    return Bar.model_validate({**CLEAN, **over})


# --- BarDraft：脏数据也要装得下 --------------------------------------------------


def test_draft_accepts_everything_the_gate_must_judge() -> None:
    dirty = _draft(
        symbol="sz.600519",
        trade_date=None,
        open=-1.0,
        high=float("nan"),
        low=0.0,
        close=None,
        volume=-5.0,
        amount=None,
    )
    assert dirty.code == "600519"
    assert set(dirty.missing_fields()) == {"trade_date", "close", "amount"}


def test_draft_flags_all_seven_required_fields() -> None:
    empty = BarDraft(symbol="600519")
    assert set(empty.missing_fields()) == {
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    }


def test_draft_rejects_unknown_field() -> None:
    """extra=forbid：适配器多塞字段说明口径没对齐，不能悄悄吞掉。"""
    with pytest.raises(ValidationError, match="Extra inputs"):
        BarDraft.model_validate({**CLEAN, "settle": 1.0})


def test_draft_rejects_unparseable_date() -> None:
    with pytest.raises(ValidationError):
        BarDraft.model_validate({**CLEAN, "trade_date": "不是日期"})


# --- 不变量判定式 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        (10.0, 9.0, 9.5, 10.0),  # high < low
        (10.0, 10.5, 9.5, 11.0),  # close > high
        (10.0, 9.8, 10.2, 9.7),  # open > high
        (10.0, 11.0, 10.5, 10.0),  # low > close
        (10.0, float("nan"), 9.0, 10.0),  # NaN：比较恒假，落在 R001 上
        (10.0, 11.0, 9.0, float("nan")),  # NaN 只在 close：max/min 会把它吞掉，靠显式判
    ],
)
def test_ohlc_violations_catches_each_inversion(case: tuple[float, ...]) -> None:
    assert ohlc_violations(*case), f"{case} 违反 OHLC 却判为合法"


def test_ohlc_violations_accepts_legal_and_degenerate() -> None:
    assert ohlc_violations(10.0, 10.0, 10.0, 10.0) == []  # 一字板
    assert ohlc_violations(10.0, 12.0, 9.0, 11.0) == []


@pytest.mark.parametrize("case", [(0.0, 1.0, 0.5, 1.0), (10.0, 11.0, -9.0, 10.5)])
def test_nonpositive_prices_catches_zero_and_negative(case: tuple[float, ...]) -> None:
    assert nonpositive_prices(*case)


def test_nonpositive_prices_ignores_nan() -> None:
    """NaN 由 R001 抓；R002 再抓一次会把同一条数据记到两个规则名下。"""
    assert not nonpositive_prices(float("nan"), 11.0, 9.0, 10.0)


# --- Bar：干净区形状 -------------------------------------------------------------


def test_bar_accepts_clean_row_and_derives_board() -> None:
    bar = _bar(symbol="300750.SZ")
    assert bar.board is Board.GEM
    assert bar.code == "300750"


@pytest.mark.parametrize(
    "over",
    [
        {"trade_date": None},
        {"open": None},
        {"open": 0.0},
        {"close": -1.0},
        {"volume": -1.0},
        {"amount": -0.5},
        {"high": 8.0},  # OHLC 逆序
        {"open": float("inf")},  # 价格为 inf：比较式判"合法"，契约必须判非法
        {"volume": float("inf")},
        {"amount": float("nan")},
        {"symbol": "AAPL"},
    ],
)
def test_bar_rejects_each_class_of_bad_value(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _bar(**over)


def test_bar_allows_zero_volume_but_not_missing_adj_factor() -> None:
    """停牌日 volume=0 是合法数据（R003 只是提示去查停牌标记，不是拒收）。"""
    assert _bar(volume=0.0, adj_factor=None).volume == 0.0


def test_from_draft_is_the_only_gate_to_clean_zone() -> None:
    bar = Bar.from_draft(_draft())
    assert bar.trade_date == date(2024, 1, 2)
    with pytest.raises(ValidationError, match="open"):
        Bar.from_draft(_draft(open=None))
    with pytest.raises(ValidationError):
        Bar.from_draft(_draft(high=8.0))


def test_bar_is_frozen() -> None:
    """契约对象可变就会被下游悄悄改出口径，干净区的数据必须不可写。"""
    with pytest.raises(ValidationError):
        _bar().open = 99.0
