"""股票搜索：代码 / 名称 / 拼音首字母三路匹配（ADR-0013 决定 2）。

三路是**或**：一路不中另两路照走。所以拼音是加速器不是过滤器——`pypinyin` 把多音字按它的默认
读音拼（`重庆啤酒` 给 CQPJ，不是 ZQPJ），用户按另一个读法搜首字母落空时，汉字子串那一路仍然会
把这只票交回来。这条性质是这套匹配规则全部用处的所在，测试里钉着。

纯层：吃 `domain.security.Listing` 序列，不读盘也不联网（ADR-0012 决定 1 的边界由
`tests/test_site_boundaries.py` 逐文件扫 import 判）。首字母表在构造时算一次，不在 `match` 里
算：4602 只逐名现拼实测 88ms，而扫完全表匹配只要 1ms 量级——每次按键白付八九十倍，而 06 §四
那格写的"模糊搜索"是个输入即搜的框。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal, NamedTuple

from pypinyin import Style, lazy_pinyin

from zhixing_quant.domain.security import Listing

#: 主要指数名册（站点"查指数"的固定入口；2026-09-22 用户要求指数可查）。
#: 代码带市场后缀——个股与指数的 6 位数字空间重叠（000001 是平安银行也是上证指数），
#: 后缀是唯一无歧义的写法。
INDEXES: tuple[tuple[str, str, str], ...] = (
    ("000001.SH", "上证指数", "szzs"),
    ("399001.SZ", "深证成指", "szcz"),
    ("399006.SZ", "创业板指", "cybz"),
    ("000688.SH", "科创50", "kc50"),
    ("000300.SH", "沪深300", "hs300"),
    ("000905.SH", "中证500", "zz500"),
    ("000016.SH", "上证50", "sz50"),
)

#: 三档的名字。`Matched` 定义在前：`RANKS` 的元素类型要用它。
Matched = Literal["code", "name", "pinyin"]

#: 三档的先后（ADR-0013 决定 2）：代码最具体，名称次之，拼音最弱。同一只票被多路命中时记在最
#: 靠前那档，所以 `Hit.by` 说的是"它是按哪一路显示的"，不是"它只属于这一路"。
RANKS: tuple[Matched, ...] = ("code", "name", "pinyin")

#: 交易所的写法：`sh600519`、`600519.SH`、`bj920819`。剥它的条件在 `_parse` 里，不在这条正则里。
_MARKET = re.compile(r"^(?:sh|sz|bj)\.?|\.(?:sh|sz|bj)$", re.IGNORECASE)


@dataclass(frozen=True)
class Hit:
    """一条搜索结果。`code` 是后面每一步（选模板、生成）唯一要用的东西，`name` 只为渲染。"""

    code: str
    name: str
    listed_on: date
    by: Matched


class _Row(NamedTuple):
    listing: Listing
    initials: str


class Index:
    """一份搜索字典加上它的首字母表。构造一次，查多次。"""

    def __init__(self, listings: Sequence[Listing]) -> None:
        self._rows = tuple(_Row(listing, _initials(listing.name)) for listing in listings)

    def match(self, query: str, *, limit: int) -> tuple[Hit, ...]:
        """三路各判一遍，按 `(档, 命中位置, 代码)` 全序排，取前 `limit` 条。

        空查询返回空而不回全市场（ADR-0013 决定 2）：一次回全市场的响应不叫搜索。`limit` 由
        调用方给——纯层不藏一个"觉得够用"的数。
        """
        if limit <= 0:
            raise ValueError(f"limit 要大于 0，现在是 {limit}")
        raw = query.strip()
        if not raw:
            return ()
        parsed = _parse(raw)
        scored = [
            (key, row.listing) for row in self._rows if (key := _score(row, parsed)) is not None
        ]
        scored.sort(key=lambda pair: (*pair[0], pair[1].code))
        return tuple(
            Hit(code=x.code, name=x.name, listed_on=x.listed_on, by=RANKS[rank])
            for (rank, _), x in scored[:limit]
        )


class _Query(NamedTuple):
    """一次查询的三个形状：三路各看一个，判之前先归一次，免得每只票各归一遍。"""

    text: str
    digits: str
    letters: str


def _parse(query: str) -> _Query:
    """把搜索框里的字拆成三路各自要的形状。

    市场前后缀只在查询里**带着数字**时才剥：`sh600519` 里的 `sh` 是交易所，而单独敲的 `sh` 是两
    个拼音字母（`上海临港` 的首字母就是 SHLG）。这条不是想出来的，是真盘上第一次跑撞出来的——
    不剥的话 `sh600519` 顺手把"沙河股份""深华发Ａ"按 SH 捞了进来，20 条里 19 条是噪音。
    """
    digits = "".join(char for char in query if char.isdigit())
    body = _MARKET.sub("", query) if digits else query
    return _Query(
        text=query.lower(),
        digits=digits,
        letters="".join(char for char in body.upper() if char.isascii() and char.isalpha()),
    )


def _initials(name: str) -> str:
    """简称的逐字首字母串，大写。非汉字字符原样穿过（`TCL科技` → `TCLKJ`）。"""
    return "".join(lazy_pinyin(name, style=Style.FIRST_LETTER)).upper()


def _score(row: _Row, query: _Query) -> tuple[int, int] | None:
    """这只票是被哪一路命中的、排在档内第几。不中返回 None——三档之外没有第四档。

    早退的顺序就是档序，所以一只票既被代码又被名称命中时它记在代码档。代码是定长 6 位
    （`normalize_code` 保证），于是"完全相等"与"只是前缀"不可能是两只票之间的差别：相等就只
    有一只有，不相等就没有相等的。档内位置对代码路恒为 0，真正比位置的是名称与拼音两档。
    """
    if query.digits and row.listing.code.startswith(query.digits):
        return (0, 0)
    position = row.listing.name.lower().find(query.text)
    if position >= 0:
        return (1, position)
    if query.letters and (at := row.initials.find(query.letters)) >= 0:
        return (2, at)
    return None


class IndexHit(NamedTuple):
    """一条指数搜索结果。`code` 带市场后缀（/api/kline 的 kind=index 就吃它）。"""

    code: str
    name: str
    by: Matched
    kind: str = "index"


def search_indexes(query: str) -> tuple[IndexHit, ...]:
    """指数的两路匹配：数字串（忽略后缀与市场前缀）与名称子串/别名。

    指数只有 7 只，不做拼音全拼——别名表（szzs/hs300 这类惯用缩写）比拼音实用，
    名称子串兜底（"创业"中创业板指）保证另一路落空时仍能回来（与个股三路的同一哲学）。
    """
    stripped = _MARKET.sub("", query.strip())
    digits = re.sub(r"\D", "", stripped)
    hits: list[IndexHit] = []
    for code, name, alias in INDEXES:
        if digits and code[:6].startswith(digits):
            hits.append(IndexHit(code, name, "code"))
        elif query.strip() and (query.strip() in name or query.strip().lower() == alias):
            hits.append(IndexHit(code, name, "name"))
    return tuple(hits)
