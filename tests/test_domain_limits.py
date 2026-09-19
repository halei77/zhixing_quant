"""涨跌停档位的单一真源（04 §二 R004、ADR-0010 决定 4）。

`domain/limits.py` 是门禁与回测共用的那一层，所以这里测的是"同一个数从同一张表来"，不是
"两个模块各测一遍各自的判法"——后者的写法是两边各写一份断言，改一处漂一处，两边还都全绿。
"""

from collections.abc import Mapping
from typing import Any

import pytest

from zhixing_quant import config
from zhixing_quant.domain.limits import limit_pct, new_listing_days
from zhixing_quant.domain.symbol import Board
from zhixing_quant.quality.gate_config import load

#: 一张测试自用的档位表。数值不是重点（重点在下面那个接线测试），**选档规则**才是。
TABLE: Mapping[str, float] = {"main": 10.0, "st": 5.0, "gem": 20.0, "star": 20.0, "bse": 30.0}


@pytest.mark.parametrize(
    ("board", "is_st", "want"),
    [
        (Board.MAIN, False, 10.0),
        (Board.MAIN, True, 5.0),  # ST 帽压过板块档
        (Board.GEM, False, 20.0),
        (Board.GEM, True, 5.0),  # 创业板 ST 仍然 5%（04 §二）
        (Board.STAR, False, 20.0),
        (Board.BSE, False, 30.0),
        (Board.BSE, True, 30.0),  # 北交所不看帽：30% 与 5% 来自两套规则
    ],
)
def test_the_band_comes_from_board_and_st_flag(board: Board, is_st: bool, want: float) -> None:
    assert limit_pct(board, is_st=is_st, limits_pct=TABLE) == want


def test_the_gate_and_the_engine_read_the_same_numbers() -> None:
    """接线：改 `gate.toml` 的档位，回测的板价跟着变。

    上面那张表是测试自己写的，光测它证明不了引擎真读到配置——而"表有两份"正是这次提取要
    消灭的东西。R004 的参数是 `[defaults]` 与 `[rule.params]` 合并后的形状，取它一份就够。
    """
    params: Mapping[str, Any] = load(config.gate_config_file()).rule("R004").params
    limits: Mapping[str, float] = params["limits_pct"]
    assert limit_pct(Board.MAIN, is_st=False, limits_pct=limits) == 10.0
    assert limit_pct(Board.MAIN, is_st=True, limits_pct=limits) == 5.0
    assert limit_pct(Board.GEM, is_st=False, limits_pct=limits) == 20.0
    assert limit_pct(Board.BSE, is_st=True, limits_pct=limits) == 30.0
    assert new_listing_days(Board.BSE, params["new_listing_no_limit_days"]) == 1


def test_a_board_missing_from_the_window_table_has_no_window() -> None:
    """表里没写这个板块 = 没有豁免窗口，而不是"借用别的板块的天数"。"""
    assert new_listing_days(Board.STAR, {"main": 5}) == 0
