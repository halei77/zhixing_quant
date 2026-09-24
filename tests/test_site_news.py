"""`news` 组件（消息原文，06 §四 新闻/公告类 + 03-L4 未来函数禁令）的对抗性测试。

怀疑点清单（这一族错了照样能生成、生成出来却在骗模型）：

1. **用了 as_of 之后的新闻**（03-L4 的头号错法）：读盘读到**生成日**而不是 as_of——
   回溯生成（as_of 早于今天）时把 as_of 之后才挂网的公告递进提示词。测试**植入这个
   错误实现**，证明本 fixture 能把它测红（正确集 ≠ 读到生成日的集，且差的正是那条
   未来公告）。
2. **点时可见性**：`ann_date ≤ as_of` 是真判据（公告日期=交易所披露日，盘后归次日、
   恒不早于挂网时刻——探测报告 §2.2 实测 72+150 行违例 0）；区间放宽到 as_of 只是让
   非交易日/休市日的公告有机会被它看到，不是放宽点时。
3. **原文契约**：渲染必须带口径行、正文超 600 字截断标 …；空行不渲染（NO_DATA_NOTE，
   ADR-0020）——半截标题冒充原文是最直接的放水。
4. **表格完整性**：公告正文自带段落与表格（换行、竖线）——一个不转义就把整张表切碎。

组合根的接线（读哪份盘、区间右端用 as_of、与日K同窗）由 build 级测试判；纯渲染不碰盘。
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from tests.fakes import bar, snapshot_root
from zhixing_quant import config
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.site import tokens
from zhixing_quant.site.prompt import NO_DATA_NOTE, build, heading, title_of
from zhixing_quant.site.table import NEWS_CONTENT_CHARS, render_news
from zhixing_quant.site.templates import NEWS_FIELDS, Selection, Template
from zhixing_quant.storage import layout
from zhixing_quant.storage.tables import NewsRow, read_table, write_table
from zhixing_quant.storage.write import store_bars

#: 2026-09-14（周一）.. 2026-09-18（周五）：与 fina_trend 同一个合成交易周。
D1, D2, D3, D4, D5 = (date(2026, 9, day) for day in range(14, 19))
WEEK = (D1, D2, D3, D4, D5)
CLOSE = 100.0


def notice(
    ann_date: date,
    *,
    art_code: str,
    title: str = "某某股份:关于变更回购方案的公告",
    content: str = "证券代码：600519 证券简称：贵州茅台 公告编号：临2026-0XX 以下是公告正文。",
    publish_time: str = "2026-09-14 20:41:29:380",
    symbol: str = "600519",
) -> NewsRow:
    """一行 news。默认值是 600519 真公告的实测量纲（毫秒时间戳、公告正文起手式）。"""
    return NewsRow("eastmoney", symbol, ann_date, publish_time, art_code, title, content)


def _tmpl(days: int) -> Template:
    return Template(
        name="最新消息解读",
        role="你是资深 A 股事件驱动分析师",
        task="判断消息面对走势的影响与持续时间",
        data=(
            Selection(dataset=layout.DAILY, days=days),
            Selection(dataset="news", days=days, fields=NEWS_FIELDS),
        ),
        output="只允许解读提示词内附的消息原文。",
        format="markdown",
        fields=("open", "close"),
        adjust="raw",
        status="ready",
        waiting_on="",
    )


@pytest.fixture
def disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """三天不复权日线的干净区，落在 tmp 数据根里（news 行由各测试自己写）。"""
    base = snapshot_root(tmp_path, monkeypatch)
    store_bars([bar(day, CLOSE) for day in WEEK[:3]], dataset=layout.DAILY, root=base / "data")
    return base / "data"


def _write_news(root: Path, rows: list[NewsRow]) -> None:
    write_table(rows, table="news", root=root)


# ── 对抗：as_of 之后的新闻不许进来 ─────────────────────────────────────────────


def _wrong_read_to_generation_day(code: str, start: date, as_of: date) -> list[NewsRow]:
    """**错误实现（对抗靶）**：读盘读到**生成日**（今天）而不是 as_of。

    这是回溯生成提示词时最自然的 bug——`as_of` 参数被传进了窗口起点，右端却拿了
    `date.today()`。今天才挂网的公告于是出现在"截至 as_of"的提示词里：03-L4 点名的
    "用了 as_of 之后的新闻"。`as_of` 形参在这里根本没被用到，这正是它错的证据。
    """
    _ = as_of
    return read_table("news", code, start, date.today(), root=config.parquet_dir())


def test_the_wrong_read_that_ignores_as_of_is_caught_by_this_fixture(
    disk: Path,
) -> None:
    """对抗测试（03-L4）：植入"读到生成日"，本 fixture 必须把它测红。"""
    assert date.today() > D3, "测试前提：生成日必须晚于 as_of（合成周钉在 2026-09）"
    # 未来行的 ann_date = 生成日当天：> as_of（正确读法滤掉它）、≤ today（错误读法带上它）
    future = date.today()
    _write_news(
        disk,
        [
            notice(D1, art_code="AN202609141827994401", title="截至as_of那天已挂网的公告"),
            notice(future, art_code="AN202609161827994402", title="as_of之后才挂网的公告"),
        ],
    )
    as_of = D3
    correct = [
        row
        for row in read_table("news", "600519", D1, as_of, root=config.parquet_dir())
        if row.ann_date <= as_of
    ]
    wrong = _wrong_read_to_generation_day("600519", D1, as_of)

    # 红路一（集合）：正确集不含未来公告，错误集含——fixture 辨别力就是这一步
    assert [row.title for row in correct] == ["截至as_of那天已挂网的公告"]
    assert "as_of之后才挂网的公告" in [row.title for row in wrong]
    assert correct != wrong, "fixture 辨别力：两种读法必须给出不同集合，否则测了等于没测"
    # 红路二（不变量）：正确集每行 ann_date ≤ as_of；错误集破了这条
    assert all(row.ann_date <= as_of for row in correct)
    assert any(row.ann_date > as_of for row in wrong), "读到生成日把 as_of 之后的行带进来了"


def test_build_never_leaks_a_news_row_published_after_as_of(disk: Path) -> None:
    """组合根整条链：as_of=D3 生成提示词，D3 之后挂网的公告一个字都不许出现。"""
    future = date.today()
    _write_news(
        disk,
        [
            notice(D1, art_code="AN202609141827994401", title="窗口内已挂网的公告标题"),
            notice(future, art_code="AN202609161827994402", title="生成日之后才挂网的公告标题"),
        ],
    )
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    text = build(_tmpl(3), "600519", D3, calendar=calendar, token_warn_above=100_000).text

    assert "### 最新消息（公告原文）（1 条公告，窗口 3 个交易日）" in text
    assert "窗口内已挂网的公告标题" in text
    assert "生成日之后才挂网的公告标题" not in text, "as_of 之后的公告泄进了提示词（03-L4）"
    assert "消息原文口径" in text, "口径行是表的一部分：模型要知道这列原文的点时契约"


# ── 空窗口与标题计数 ───────────────────────────────────────────────────────────


def test_a_window_without_any_news_gives_the_no_data_note(disk: Path) -> None:
    """窗口里一条公告都没有（没回填 / 这票真的没发）→ `NO_DATA_NOTE` 整节交代
    （ADR-0020）——不给只有表头的空表，更不许模型拿标题编正文。"""
    assert disk.is_dir()  # 夹具只钉数据根；本例断言"没有 news 行"那一侧
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    text = build(_tmpl(3), "600519", D3, calendar=calendar, token_warn_above=100_000).text

    section = text.split("### 最新消息（公告原文）")[1]
    assert NO_DATA_NOTE.splitlines()[0] in section
    assert "公告日" not in section, "空段不许给表骨架——空表会被读成那段时间没消息"


def test_the_heading_counts_announcements_not_trading_days() -> None:
    """事件表的标题数的是**条数**：写成"4 个交易日"会把 4 条公告说成 4 天（决定 3）。"""
    selection = Selection(dataset="news", days=30, fields=NEWS_FIELDS)
    heading_text = heading(selection, 4)
    assert "4 条公告" in heading_text
    assert "窗口 30 个交易日" in heading_text
    assert "4 个交易日" not in heading_text


def test_the_title_is_registered_not_invented() -> None:
    assert title_of("news") == "最新消息（公告原文）"


# ── 渲染：口径行、截断、表格完整性 ─────────────────────────────────────────────


def test_render_carries_the_caliber_line_and_truncates_the_body() -> None:
    """口径行 + 正文截断标 …：6000 字的正文进提示词只附前 600，且**声明**附的是节选
    ——只给标题是放水，全贴会把 10 万 token 阈值顶破，声明的截断才是诚实的原文。"""
    long_body = "证券代码：600519 证券简称：贵州茅台 " + "正文内容" * 1000
    assert len(long_body) > NEWS_CONTENT_CHARS
    text = render_news([notice(D1, art_code="AN202609141827994401", content=long_body)])

    assert text.startswith("> 消息原文口径")
    assert "| 公告日 | 挂网时刻 | 标题 | 正文 |" in text
    body_cell = text.splitlines()[-1].split("|")[4].strip()
    assert body_cell.endswith("…"), "超长正文必须截断并标记，不许静默截"
    assert len(body_cell) == NEWS_CONTENT_CHARS + 1
    assert "2026-09-14 20:41:29" in text, "挂网时刻进提示词（盘后/盘中是解读素材）"


def test_render_keeps_newlines_and_pipes_from_shattering_the_table() -> None:
    """公告正文自带段落与表格：换行会把一行切成两行、竖线会切列——单元格层必须压平/转义。"""
    messy = "第一段\n有换行 | 有竖线\n第二段"
    text = render_news([notice(D1, art_code="AN202609141827994401", content=messy)])
    data_line = text.splitlines()[-1]
    assert "\n" not in data_line
    assert "\\| 有竖线" in data_line, "markdown 表里的竖线要转义，否则列被切碎"
    assert "换行 | 有竖线" not in data_line, "裸竖线还在——转义没生效"
    cells = re.split(r"(?<!\\)\|", data_line)
    assert len(cells) == 6, f"四列表按未转义竖线切开应是 6 段（首尾空 + 4 格），实得 {len(cells)}"

    csv_text = render_news(
        [notice(D1, art_code="AN202609141827994401", content=messy)], format="csv"
    )
    assert csv_text.startswith("# 消息原文口径")
    assert "第一段 有换行 | 有竖线 第二段" in csv_text, "CSV 只压换行，竖线是普通字符"


def test_build_token_estimate_covers_the_news_prompt(disk: Path) -> None:
    """真实（合成）数据上 token 估算照跑：估算器认得出这张表，不为 0 也不炸。"""
    _write_news(disk, [notice(D1, art_code="AN202609141827994401", content="正文" * 300)])
    calendar = TradingCalendar([*WEEK, date(2026, 9, 21)])
    prompt = build(_tmpl(3), "600519", D3, calendar=calendar, token_warn_above=100_000)
    assert tokens.estimate(prompt.text) > 0
