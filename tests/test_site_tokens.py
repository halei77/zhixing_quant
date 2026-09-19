"""token 估算：一条写死的字符启发式（ADR-0011 决定 6）。

判的是**方向**而不是精度：这个数不等于任何一家的真实 token 数（代价一），所以这里不问"算得准
不准"，只问三件能判的事——分段取整会不会把方向搞反（低估比高估危险）、CJK 码位段是不是真按
表覆盖、以及阈值那条线是不是"严格大于"（06 §八-5 说的是"超阈值警告"，正好等于不该警告）。

最后一条测试对着 06 §六 那个"≈1080 根K线 ≈ 2 万 token"的参考值：它把常数钉在正确的量级上。
把 4 字符改成 1 字符，那条立刻红，而任何"只看相对关系"的测试都不会发现数已经翻了几倍。
"""

from datetime import date, timedelta

import pytest

from tests.fakes import daily_span
from zhixing_quant.site import tokens
from zhixing_quant.site.table import render
from zhixing_quant.site.tokens import ASCII_CHARS_PER_TOKEN, estimate, over

BASE = date(2022, 1, 1)
TABLE_FIELDS = ("open", "high", "low", "close", "volume", "amount")
#: 每段一个代表字符：假名、统一表意文字、全角标点、扩展 B（生僻字，退市票简称里有）。
CJK_SAMPLES = ("中", "あ", "カ", "、", "　", "０", "\U00020000", "䶿")
#: 刻意**不**算 CJK 的那些：半角 ASCII、拉丁带音调、希腊、俄文、emoji。
#: 它们各自落在哪一段是有意的（`CJK_RANGES` 上面那段注释），抽错方向会让英文行的 token 数翻倍。
NON_CJK_SAMPLES = ("a", " ", "|", "é", "Ω", "Я", "🙂")


def test_empty_text_has_no_tokens() -> None:
    assert estimate("") == 0


@pytest.mark.parametrize("char", CJK_SAMPLES)
def test_each_cjk_region_counts_one_token_per_char(char: str) -> None:
    assert estimate(char) == 1
    assert estimate(char * 5) == 5


def test_the_documented_rate_is_the_constant_in_use() -> None:
    """ADR-0011 决定 6 写死了"其余每 4 字符 1 token"：常数与文档改的必须是同一趟。

    方向也一起钉住：余数向上取整（宁可高估）。哪天有人为了"算得更准"改成向下取整，报红的会是
    这条与下面那条混排测试，而不是某条恰好抽到余数的行为断言。
    """
    assert tokens.ASCII_CHARS_PER_TOKEN == 4
    assert estimate("a" * 3) == 1, "余数进位"
    assert estimate("a" * 4) == 1 and estimate("a" * 5) == 2


@pytest.mark.parametrize("char", NON_CJK_SAMPLES)
def test_what_is_not_cjk_counts_by_the_ascii_rate(char: str) -> None:
    assert estimate(char * ASCII_CHARS_PER_TOKEN) == 1
    assert estimate(char * (ASCII_CHARS_PER_TOKEN + 1)) == 2


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("abcd", 1),
        ("abcdefg", 2),
        ("中 ab", 2),
        ("中文", 2),
        ("hello world", 3),
    ],
    ids=["整四", "带余数", "中英混", "纯中文", "英文短语"],
)
def test_the_counts_this_heuristic_actually_produces(text: str, want: int) -> None:
    assert estimate(text) == want


def test_mixed_text_is_ceiled_per_run_so_the_error_goes_upward() -> None:
    """分段各自向上取整 → 混合文本比整段连续算得更多（宁可高估）。

    `"中a"` × 4：整段算是 4 个汉字 + 4 个字母 ≈ 5；分段算是每段一个汉字各带一次余数 = 8。
    低估会让用户在临近阈值时以为安全，高估只是提前一句警告——方向必须固定成后者。
    """
    assert estimate("中a" * 4) == 8
    assert estimate("中a" * 4) > estimate("中中中中" + "aaaa")


def test_the_threshold_is_a_strict_greater_than() -> None:
    """正好等于阈值不警告：06 §八-5 的措辞是"超阈值"，而阈值是给"会不会爆"用的分界线。"""
    limit = estimate("中文" * 10)
    assert not over("中文" * 10, limit)
    assert over("中文" * 10 + "中", limit)


def test_a_nonpositive_limit_warns_only_on_something() -> None:
    """阈值配成 0：空串仍然不警告，有一个字就警告。

    这条防的是"顺手把 `>` 改成 `>=`"——那时 06 §八-5 的"超阈值警告"会在用户什么都没选的时候
    先响一声，而 `templates.py` 又保证阈值本身不可能为 0，改错方向在配置里抓不到。
    """
    assert not over("", 0)
    assert over("x", 0)


def test_the_reference_table_lands_in_the_documented_magnitude() -> None:
    """06 §六：短期投资默认组合 ≈1080 根K线 ≈2 万 token。判量级，不判相等。

    这是整条启发式与文档唯一的接点：常数错了（比如 1 字符 1 token）会报出 6 万+，
    而"1 万到 3 万"这个带子正是 06 那句"≈2 万"允许的范围。
    """
    bars = [
        daily_span(
            BASE + timedelta(days=i),
            open_=10.0 + i / 100,
            high=10.5 + i / 100,
            low=9.5 + i / 100,
            close=10.2 + i / 100,
            volume=1_234_567.0,
            amount=12_591_900.0,
        )
        for i in range(1080)
    ]
    text = render(bars, format="markdown", fields=TABLE_FIELDS, adjust="backward")
    assert 10_000 < estimate(text) < 30_000
