"""股票搜索的三路匹配与那套全序（ADR-0013 决定 2）。

这个组件唯一的可观测行为是**顺序**：搜 600 出来的三只里谁排第一。06 §四 只写了"代码/名称/拼音
模糊搜索"，没定排序，而没定排序就写不出能判的测试——所以逐条钉：三路各自命中、三路是"或"不是
"与"、同一只票多路命中时记在哪一档、档内按位置、位置相同按代码。

`by` 也判：预览页要说"这只票是按你敲的哪一路找到的"，而它一旦与排序用的档位不符，用户看到的
理由就和实际顺序无关——那是最难查的那种"难看"。
"""

from collections.abc import Sequence
from datetime import date

import pytest

from zhixing_quant.domain.security import Listing
from zhixing_quant.site.search import Hit, Index

LISTED_ON = date(2000, 1, 1)


def _listing(code: str, name: str) -> Listing:
    return Listing(code=code, name=name, listed_on=LISTED_ON)


def _codes(hits: Sequence[Hit]) -> list[str]:
    return [hit.code for hit in hits]


#: 六只真实票配两只合成的：`东科技`/`科技东` 这对的代码顺序与命中位置顺序**相反**，
#: 它们存在的唯一理由是让"档内比位置"这条判得出来（见 test_within_a_route…）。
BOOK = Index(
    [
        _listing("600519", "贵州茅台"),
        _listing("600036", "招商银行"),
        _listing("600132", "重庆啤酒"),
        _listing("000858", "五粮液"),
        _listing("000100", "TCL科技"),
        _listing("300750", "宁德时代"),
        _listing("600690", "东科技"),
        _listing("600691", "科技东"),
        _listing("600848", "上海临港"),
    ]
)


def test_a_code_prefix_finds_every_code_that_starts_with_it() -> None:
    hits = BOOK.match("600", limit=10)
    assert _codes(hits) == ["600036", "600132", "600519", "600690", "600691", "600848"]
    assert {hit.by for hit in hits} == {"code"}


def test_the_market_prefix_and_the_suffix_spelling_reach_the_same_code() -> None:
    """`sh600519`、`600519.SH`、带空格的粘贴，都要走到同一只票。

    判的是同一结果而不是各自非空：搜索框里的字是人敲的，写法不统一才是常态。
    """
    for query in ("600519", "sh600519", "600519.SH", "  600519  "):
        hits = BOOK.match(query, limit=10)
        assert _codes(hits) == ["600519"], query
        assert hits[0].by == "code"


def test_a_market_prefix_is_not_mistaken_for_pinyin() -> None:
    """`sh600519` 只回那一只：前缀不剥的话 `SH` 会顺手把"上海"开头的票按首字母捞进来。

    这不是设想出来的边界，是真盘上第一次跑撞出来的——4602 只的字典里 `sh600519` 返回了 20 条，
    19 条是 `沙河股份`、`深华发Ａ` 那一类。所以这条判的是**不多**。
    """
    assert _codes(BOOK.match("sh600519", limit=10)) == ["600519"]
    assert _codes(BOOK.match("600519.SH", limit=10)) == ["600519"]


def test_a_bare_prefix_is_still_two_pinyin_letters() -> None:
    """反过来也要成：单独敲 `sh` 时它是拼音不是交易所，`上海临港`（SHLG）得搜得出来。

    两条夹在一起才封住实现的空间：既不能"见 sh 就剥"（那这条红），也不能"从不剥"（上一条红）。
    """
    hits = BOOK.match("sh", limit=10)
    assert _codes(hits) == ["600848"]
    assert hits[0].by == "pinyin"


def test_a_name_substring_hits_the_name_route() -> None:
    hits = BOOK.match("茅台", limit=10)
    assert _codes(hits) == ["600519"]
    assert hits[0].by == "name"


def test_pinyin_initials_are_matched_case_insensitively() -> None:
    """`gzmt` 与 `GZMT` 是同一个查询：搜索框不区分大小写，而首字母表是大写的。"""
    assert _codes(BOOK.match("gzmt", limit=10)) == ["600519"]
    assert _codes(BOOK.match("GZMT", limit=10)) == ["600519"]
    assert BOOK.match("gzmt", limit=10)[0].by == "pinyin"


def test_a_polyphone_misses_the_initials_route_but_the_hanzi_route_still_works() -> None:
    """代价二那条在代码里的样子：`重庆啤酒` 的首字母是 CQPJ（chóng），按 zhòng 搜落空。

    三路是**或**，所以这一次落空不意味着这只票搜不到——汉字那一路不看读音。这条测试同时钉住
    "落空"是真的落空（不是我们把它偷偷塞回来），否则它就成了一个永远为真的断言。
    """
    assert _codes(BOOK.match("zq", limit=10)) == []
    assert _codes(BOOK.match("重庆", limit=10)) == ["600132"]
    assert _codes(BOOK.match("cqpj", limit=10)) == ["600132"]


def test_a_hanzi_query_never_takes_the_initials_route() -> None:
    """汉字查询不进拼音路：首字母表是拉丁字母串，拿"贵"去比它不可能命中。

    判它不中，是为了让"三路是或"不被误当成"四路"——`贵州` 那次命中走的是名称路，`by` 记的也
    得是 name。
    """
    assert BOOK.match("贵", limit=10)[0].by == "name"


def test_the_earliest_route_wins_when_one_listing_matches_two() -> None:
    """同一只票被两路命中时记最靠前那档：`TCL科技` 既含拉丁子串又在首字母表里。"""
    hits = BOOK.match("tcl", limit=10)
    assert _codes(hits) == ["000100"]
    assert hits[0].by == "name"


def test_a_name_hit_ranks_above_a_pinyin_hit() -> None:
    """档序 code < name < pinyin：`t` 命中 `TCL科技`（名称）与 `贵州茅台`（GZMT 里有 T）。

    被"名称"解释的结果排在被"首字母"解释的结果之前——用户敲的是原样的字母时，名字里就带着它
    的那些票更可能是他要的。
    """
    hits = BOOK.match("t", limit=10)
    assert _codes(hits) == ["000100", "600519"]
    assert [hit.by for hit in hits] == ["name", "pinyin"]


def test_within_a_route_the_earlier_position_comes_first() -> None:
    """`科技东`（位置 0）排在 `东科技`（位置 1）前，而前者的代码**更大**。

    这一对合成票的存在理由就是这个：代码方向与位置方向一致时，"按位置排"与"按代码排"两条规则
    给出同一个答案，测试判不出实现了哪一条。这里让它们相反，位置赢才算判到了。
    `TCL科技` 也含"科技"，在位置 3，所以它垫底——三只同档，排序全由位置决定。
    """
    assert _codes(BOOK.match("科技", limit=10)) == ["600691", "600690", "000100"]


def test_same_position_falls_back_to_the_code_ascending() -> None:
    """600 那五只都在位置 0 命中前缀，于是顺序完全由代码升序决定。"""
    hits = BOOK.match("600", limit=10)
    assert _codes(hits) == sorted(_codes(hits))


def test_a_limit_truncates_without_losing_the_head() -> None:
    """截断砍的是尾巴不是头部：`limit` 是"这一屏放得下几条"，不是"随机给几条"。"""
    assert _codes(BOOK.match("600", limit=2)) == ["600036", "600132"]
    assert _codes(BOOK.match("600", limit=1)) == ["600036"]


@pytest.mark.parametrize("query", ["", "   ", "\t"])
def test_an_empty_query_returns_nothing_rather_than_the_whole_market(query: str) -> None:
    """空查询回 5000 条不叫搜索（ADR-0013 决定 2）：字典再小也不该被一次回车倒出来。"""
    assert BOOK.match(query, limit=10) == ()


def test_a_non_positive_limit_is_refused() -> None:
    """`limit=0` 与 `limit=-1` 都是调用方写错了，不是"要不要返回结果"的开关。"""
    for limit in (0, -1):
        with pytest.raises(ValueError, match="limit"):
            BOOK.match("600", limit=limit)


def test_an_index_with_no_listings_finds_nothing() -> None:
    """空字典是"快照没抓到"的样子，判它不响也不返回东西（`read_master` 才是响的那一层）。"""
    assert Index([]).match("600519", limit=10) == ()


def test_a_hit_carries_what_the_page_needs_to_render() -> None:
    hit = BOOK.match("600519", limit=10)[0]
    assert (hit.code, hit.name, hit.listed_on, hit.by) == ("600519", "贵州茅台", LISTED_ON, "code")
