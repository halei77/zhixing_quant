"""代码规范化与板块判定（04 §二 R004 的地基）。"""

import pytest

from zhixing_quant.domain.symbol import Board, UnknownCode, board_of, normalize_code

ALL_CODES = {
    "600519": Board.MAIN,
    "000001": Board.MAIN,
    "001979": Board.MAIN,
    "002415": Board.MAIN,
    "003816": Board.MAIN,
    "300750": Board.GEM,
    "301236": Board.GEM,
    "688981": Board.STAR,
    "689009": Board.STAR,
    "430047": Board.BSE,
    "830799": Board.BSE,
    "871981": Board.BSE,
    "889815": Board.BSE,
    "920002": Board.BSE,
}


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("600519", "600519"),
        ("sh600519", "600519"),
        ("SH600519", "600519"),
        ("600519.SH", "600519"),
        ("sz.600519", "600519"),
        (" 600519 ", "600519"),
        # 前缀与后缀互相矛盾时以 6 位代码为准：板块由代码判，市场前缀只是各源的书写习惯
        ("sh600519.SZ", "600519"),
        ("BJ.920002", "920002"),
    ],
)
def test_normalize_accepts_each_source_shape(code: str, expected: str) -> None:
    assert normalize_code(code) == expected


@pytest.mark.parametrize(
    "bad",
    ["", "60051", "6005190", "sh60051", "600519.XX", "AAPL", "港股00700", ".600519"],
)
def test_normalize_rejects_non_six_digit(bad: str) -> None:
    with pytest.raises(UnknownCode):
        normalize_code(bad)


@pytest.mark.parametrize(("code", "board"), sorted(ALL_CODES.items()))
def test_board_of_every_listed_prefix(code: str, board: Board) -> None:
    assert board_of(code) is board


@pytest.mark.parametrize("code", ["510300", "159915", "900901", "200011", "000001X"])
def test_unknown_prefix_raises_instead_of_defaulting_to_main(code: str) -> None:
    """ETF/B 股不在四档板块表里：给个默认主板会让 R004 静默用错阈值，比抛错难查得多。"""
    with pytest.raises(UnknownCode):
        board_of(code)


def test_board_of_accepts_source_shapes() -> None:
    assert board_of("300750.SZ") is Board.GEM
    assert board_of("sz300750") is Board.GEM
