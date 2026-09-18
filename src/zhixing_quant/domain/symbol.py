"""证券代码的规范化与板块判定（04 §二 R004 的地基）。

板块决定涨跌幅上限，所以这张表判错的后果不是难看，是 R004 静默失效：把创业板
按主板 10.5% 判，20% 的真K线会被整批拒收；反过来则放行越界脏数据。两种都比报错
难查，因此未知前缀一律抛错而不是给个"默认主板"。

各源写法不一（600519 / sh600519 / 600519.SH），normalize_code 是适配器唯一的
入口形状：把归一化收在一处，板块表只管 6 位数字，不必为每种写法各判一遍。
"""

import re
from enum import Enum

_CODE_RE = re.compile(r"^\d{6}$")
_PREFIX_RE = re.compile(r"^(?:(?:sh|sz|bj)\.?)?(\d{6})(?:\.(?:sh|sz|bj))?$", re.IGNORECASE)


class Board(Enum):
    """交易所板块。涨跌幅档位在 04 §二 R004，容差 0.5% 由 quality 层配置持有。"""

    MAIN = "main"
    GEM = "gem"
    STAR = "star"
    BSE = "bse"


# 前缀 → 板块。深市 002/003 是原中小板，2021 年并入主板后涨跌幅同为 10%，
# 所以不单独设档：这里区分的是"限幅是多少"，不是"历史上叫过什么"。
_PREFIX_TABLE: dict[str, Board] = {
    "60": Board.MAIN,
    "000": Board.MAIN,
    "001": Board.MAIN,
    "002": Board.MAIN,
    "003": Board.MAIN,
    "300": Board.GEM,
    "301": Board.GEM,
    "688": Board.STAR,
    "689": Board.STAR,
    "43": Board.BSE,
    "83": Board.BSE,
    "87": Board.BSE,
    "88": Board.BSE,
    "920": Board.BSE,
}


class UnknownCode(ValueError):
    """代码不属于本表覆盖的四档板块。"""


def normalize_code(text: str) -> str:
    """把各源写法收成 6 位数字。

    认 sh/sz/bj 前缀与 .SH/.SZ/.BJ 后缀；不认市场后缀与代码矛盾的情况（那是适配器
    的活，04 §五 交叉对账会抓），因为 6 位代码本身已足以定板块。
    """
    raw = text.strip()
    m = _CODE_RE.match(raw)
    if m:
        return raw
    m = _PREFIX_RE.match(raw)
    if m:
        return m.group(1)
    raise UnknownCode(f"无法识别的证券代码：{text!r}")


def board_of(text: str) -> Board:
    """代码 → 板块。未知前缀抛 UnknownCode，不默认主板。"""
    code = normalize_code(text)
    for prefix in (3, 2):
        board = _PREFIX_TABLE.get(code[:prefix])
        if board is not None:
            return board
    raise UnknownCode(f"代码 {code} 不属于四档板块（主板/创业板/科创板/北交所）")
