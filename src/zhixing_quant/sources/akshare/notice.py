"""消息组件的原料采集：东财公告（列表 + 正文）→ `news` 参考表。

**为什么是东财公告而不是"新闻接口"**（2026-09-25 探测报告
`${ZX_DATA_ROOT}/reports/source-probe/2026-09-25-news-sources.md` 的判定，本模块是它的落地）：

- 06 §四 点名要评估的 akshare 新闻接口（`stock_news_em`）**不合格**——正文是 62–149 字
  摘要不是原文，关键词绑定串票（000001 title 命中 0/30），同事件多 outlet 重复；
- akshare 的公告封装（`stock_individual_notice_report`）只有标题+日期+网址——丢了
  `display_time`、没有正文；
- **合格的是同一上游的两个公开 JSON 端点**：`fetch.notice_list`（列表，带毫秒挂网时刻）+
  `fetch.notice_content`（正文原文，分页 5000 字/页）——原文、点时、单票绑定三样全齐；
- promax 新闻族无个股参数且 >30 天窗口静默返回窗外数据（stale 缓存），rds 新闻族 403，
  ngw 无新闻接口——全部排除（报告 §四 逐条留证）。

**层分工**（对齐仓库纪律）：网络在 `fetch.notice_list/notice_content`（薄到只发请求）；
本模块 = 解析（纯，黄金样本重放测）+ 行级锚 + 可续跑回填 + `python -m` 入口。

**为什么自带回填而不是进 zx-relay**（形态选择，别另立一套的边界说明）：东财不是
rds/promax 转接谱系——协议（无 X-API-Key、非 Tushare fields/items 分页）、端点（两个
非同构 URL）、鉴权（无）全不同，塞进 `client.fetch` 是把转接源抽象撕开；`jobs/backfill`
的 Tushare 分页契约同样不匹配。故本模块自带续跑循环，但**语义照抄既有形态**：
判据读盘（窗口内已有 `art_code` 差集）、`--attempts`/`--breaker` 同名同义、退出码
0 跑完 / 1 有失败票 / 2 熔断、报告落 `${ZX_DATA_ROOT}/reports/news/`。

**限速**（探测报告 §2.1）：25 连发全 200、无 rate 头、0.05–0.25s/发——仍按 Qoute §7 纪律
自限 `--pace`（默认 0.3s/发）；失败重试 1→2→4s 阶梯，`--attempts` 次数耗尽即该票记败。

入口：`uv run --frozen python -m zhixing_quant.sources.akshare.notice --symbols 600519,…`
（本刀文件集不含 pyproject，故无 console script——`python -m` 即入口）。
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

from zhixing_quant import config
from zhixing_quant.sources.akshare import fetch
from zhixing_quant.storage.tables import read_table, write_table

#: 入库的 source 标记：东财数据中心（与探测报告同词）。
SOURCE = "eastmoney"

#: 列表页上限（akshare 同款 page_size=100）与翻页守卫：total_hits 说谎也不许无限翻。
PAGE_SIZE = 100
MAX_PAGES = 500

#: 回填窗口向列表端多探的自然日：begin/end 过滤字段（notice_date vs display_time）在
#: 边界上可能差一天（盘后发布归次日），多探 3 天再按 ann_date 收窗，边界行一条不丢。
WINDOW_PAD_DAYS = 3


class NoticeItem(NamedTuple):
    """列表解析出的一条公告（正文未取）：行身份三件套 + 标题。"""

    symbol: str
    ann_date: date
    publish_time: str  # display_time 原样（毫秒挂网时刻）
    art_code: str
    title: str


def parse_notice_list(payload: Any, *, symbol: str) -> list[NoticeItem]:
    """东财列表原始响应 → 条目。**形状错整批 ValueError**（源改了形状，一页都不可信）。

    行级判定不在这里：空标题/日期违例留给 `anchor_news`（一处管行级，解析只管形状）——
    与 relay 侧"parse 拒形状、anchor 拒时序"的分工同构。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError(f"{symbol} 的东财公告列表响应没有 data 对象：形状变了，整批拒")
    raw_list = payload["data"].get("list")
    if not isinstance(raw_list, list):
        raise ValueError(f"{symbol} 的东财公告列表响应没有 data.list：形状变了，整批拒")
    items: list[NoticeItem] = []
    for raw in raw_list:
        if not isinstance(raw, dict):
            raise ValueError(f"{symbol} 的列表行不是对象：形状变了，整批拒")
        art_code = str(raw.get("art_code") or "")
        if not (art_code.startswith("AN") and art_code[2:].isdigit()):
            raise ValueError(
                f"{symbol} 的 art_code {art_code!r} 不是「AN+数字」：源改了主键形状，整批拒"
            )
        display_time = str(raw.get("display_time") or "")
        notice_date = str(raw.get("notice_date") or "")
        try:
            date.fromisoformat(display_time[:10])
            date.fromisoformat(notice_date[:10])
        except ValueError as exc:
            raise ValueError(
                f"{symbol} {art_code} 时间戳不可解析"
                f"（display_time={display_time!r} notice_date={notice_date!r}）：整批拒"
            ) from exc
        items.append(
            NoticeItem(
                symbol=symbol,
                ann_date=date.fromisoformat(notice_date[:10]),
                publish_time=display_time,
                art_code=art_code,
                title=str(raw.get("title") or "").strip(),
            )
        )
    return items


def parse_notice_content(payload: Any) -> str:
    """东财正文原始响应 → 该页原文。**形状错整批 ValueError**；`notice_content: null`
    （老公告扫描件无文本层，探测实测 2001 招股书 len=0）→ 返回 `""`，由锚按"空正文"剔行。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("东财公告正文响应没有 data 对象：形状变了，整批拒")
    if "notice_content" not in payload["data"]:
        raise ValueError("东财公告正文响应没有 notice_content 键：形状变了，整批拒")
    body = payload["data"]["notice_content"]
    if body is None:
        return ""
    if not isinstance(body, str):
        raise ValueError(f"notice_content 不是文本（{type(body).__name__}）：形状变了，整批拒")
    return body


class NewsRow(NamedTuple):
    """与 `storage.tables.NewsRow` 同形同序的**孪生**行模型（storage 不 import sources，
    两边各定义一个类、列名单逐一相等——`tests/test_notice_data.py` 钉互认，写盘时
    `write_table` 还会按列名单结构检查一遍）。

    字段语义与 storage 侧 docstring 同一份：ann_date 是点时时钟（东财公告日期，
    盘后归次日恒 ≥ 挂网日），publish_time 是毫秒挂网时刻的源原样。
    """

    source: str
    symbol: str
    ann_date: date
    publish_time: str
    art_code: str
    title: str
    content: str


def row_of(item: NoticeItem, content: str) -> NewsRow:
    """条目 + 正文 → `news` 行（与 `storage.tables.NewsRow` 同形同序）。"""
    return NewsRow(
        SOURCE,
        item.symbol,
        item.ann_date,
        item.publish_time,
        item.art_code,
        item.title,
        content,
    )


def anchor_news(
    rows: Sequence[NewsRow], *, today: date | None = None
) -> tuple[list[str], int, list[NewsRow]]:
    """行级锚（ADR-0015 决定 3 的消息版）：时序违例**整批记 problems**，缺内容**跳行计数**。

    三条判据全部由 2026-09-25 三票实测立的（探测报告 §2.2/§2.3，违例均 0）：

    1. `ann_date ≤ 今天`——公告日期不在未来（与 forecast/report_rc 同款）；
    2. `ann_date ≥ publish_time 的日期`——公告日期不早于东财挂网时刻。实测
       `notice_date − date(display_time) ∈ {0: 35, 1: 37}`（盘后归次日），恒 ≥ 0；
       违了就是源把时序给反了，不裁决；
    3. 标题与正文都非空——**消息原文**的最低条件：空正文（扫描件无文本层）跳行计数，
       渲染层宁可少一条也不给"有标题没原文"的半截消息（06 §四 的 output 契约）。

    problems 非空 → 调用方整批拒（一行不写）；unanchored 只记数（源没给，不是对不上）。
    """
    now = today if today is not None else date.today()
    problems: list[str] = []
    unanchored = 0
    kept: list[NewsRow] = []
    for row in rows:
        try:
            publish_day = date.fromisoformat(row.publish_time[:10])
        except ValueError:
            problems.append(
                f"{row.symbol}@{row.ann_date} publish_time={row.publish_time!r} 不可解析"
            )
            continue
        if row.ann_date > now:
            problems.append(f"{row.symbol}@{row.ann_date} 公告日期在未来（今天 {now}）")
            continue
        if row.ann_date < publish_day:
            problems.append(
                f"{row.symbol}@{row.ann_date} 公告日期早于挂网日 {publish_day}"
                f"（挂网 {row.publish_time}）：时序不成立"
            )
            continue
        if not row.title or not row.content.strip():
            unanchored += 1  # 空标题/空正文：源没给原文，跳行不崩
            continue
        kept.append(row)
    return problems, unanchored, kept


#: 正文抓取失败不烧整票：单条记数，票的成败由"列表取到没/锚拒没"定。
LISTER = Callable[..., dict[str, Any]]
READER = Callable[..., dict[str, Any]]


def _fetch(
    call: Callable[[], dict[str, Any]],
    *,
    attempts: int,
    pace: float,
    sleep: Callable[[float], None],
) -> dict[str, Any]:
    """一次带限速与重试的取数：`pace` 秒限速（每发都睡），失败按 1→2→4s 阶梯重试。

    `ValueError` 不在这里拦——解析层的形状拒由调用方按"不烧 attempts"处理
    （同 backfill 的先例：同形状问题重发一万次还是同一个答案）。
    """
    last = ""
    for attempt in range(attempts):
        if pace > 0:
            sleep(pace)
        try:
            return call()
        except Exception as exc:  # 网络层异常族（超时/5xx/JSON）宽捕，重试后耗尽原样转败
            last = f"{type(exc).__name__}: {exc}"
            if attempt + 1 < attempts:
                sleep(min(2.0**attempt, 4.0))
    raise RuntimeError(f"重试 {attempts} 次仍失败：{last}")


@dataclass(frozen=True)
class SymbolOutcome:
    """一票的回填结局（形态对齐 `jobs.backfill.SymbolOutcome` 的 status/reason 语义）。"""

    symbol: str
    status: str  # "landed" | "skipped" | "failed" | "breaker"
    listed: int = 0
    existing: int = 0
    added: int = 0
    empty: int = 0
    failed: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("landed", "skipped")


def _list_window(
    symbol: str,
    begin: date,
    end: date,
    *,
    lister: LISTER,
    attempts: int,
    pace: float,
    sleep: Callable[[float], None],
) -> list[NoticeItem]:
    """一票一个窗口的全部列表页（分页到 `total_hits` 或空页，带守卫）。"""
    items: list[NoticeItem] = []
    page = 1
    total: int | None = None
    while True:
        payload = _fetch(
            partial(lister, symbol, begin, end, page=page, page_size=PAGE_SIZE),
            attempts=attempts,
            pace=pace,
            sleep=sleep,
        )
        batch = parse_notice_list(payload, symbol=symbol)  # 形状错：原样抛，不烧剩余页
        if total is None:
            raw_total = (payload.get("data") or {}).get("total_hits")
            total = int(raw_total) if isinstance(raw_total, (int, float)) else 0
        if not batch:
            break
        items.extend(batch)
        if len(items) >= total:
            break
        page += 1
        if page > MAX_PAGES:
            break
    return items


def backfill(
    symbols: Sequence[str],
    *,
    days: int,
    root: Path,
    directory: Path,
    attempts: int = 3,
    breaker: int = 5,
    pace: float = 0.3,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] | None = None,
    now: datetime | None = None,
    lister: LISTER | None = None,
    reader: READER | None = None,
) -> tuple[str, int]:
    """跑一池票：列表拉窗口 → 判据读盘算缺口 → 逐条正文 → 锚 → 幂等落盘 → 报告归档。

    返回 `(markdown 报告, 退出码)`；报告写进 `directory/news-backfill-<日期>.md`。
    退出码与 `jobs.backfill` 同义：0 跑完无失败票、1 有失败票、2 熔断收工（没跑完）。

    `root`/`directory` 无默认值（坑 #24：读盘根必须是干净区那层）；`now`/`lister`/`reader`
    是测试注入点，真跑用默认。
    """
    if attempts < 1 or breaker < 1:
        raise ValueError(f"attempts/breaker 至少为 1（收到 {attempts}/{breaker}）")
    if not symbols:
        raise ValueError("票池是空的：--symbols 没解析出代码")
    do_list = lister if lister is not None else fetch.notice_list
    do_read = reader if reader is not None else fetch.notice_content
    moment = now if now is not None else datetime.now()
    end = moment.date()
    start = end - timedelta(days=days)

    outcomes: list[SymbolOutcome] = []
    consecutive = 0
    breaker_tripped = False

    for position, symbol in enumerate(symbols):
        if consecutive >= breaker:
            outcomes.extend(
                SymbolOutcome(left, "breaker", reason="连续失败达 --breaker，熔断收工")
                for left in symbols[position:]
            )
            breaker_tripped = True
            break
        outcome = _process_symbol(
            symbol,
            start=start,
            end=end,
            lister=do_list,
            reader=do_read,
            root=root,
            attempts=attempts,
            pace=pace,
            sleep=sleep,
        )
        outcomes.append(outcome)
        # 熔断数"连续没拿到可用的东西"：判据读盘的 skipped 是读盘不是联网，中立不计
        # （与 jobs/backfill 同款语义：没联网不证明源活着，也不该攒败绩）。
        if outcome.status == "failed":
            consecutive += 1
        elif outcome.status in ("landed", "skipped"):
            consecutive = 0
        if progress is not None:
            progress(
                f"[{position + 1}/{len(symbols)}] {symbol} {outcome.status}"
                f" 列表 {outcome.listed} / 已有 {outcome.existing} / 新增 {outcome.added}"
                + (f" 失败 {outcome.failed}" if outcome.failed else "")
                + (f"：{outcome.reason}" if outcome.reason else "")
            )

    markdown = _report(outcomes, start=start, end=end, breaker_tripped=breaker_tripped)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"news-backfill-{end.isoformat()}.md").write_text(markdown, encoding="utf-8")
    if breaker_tripped:
        return markdown, 2
    return markdown, (1 if any(not outcome.ok for outcome in outcomes) else 0)


def _process_symbol(
    symbol: str,
    *,
    start: date,
    end: date,
    lister: LISTER,
    reader: READER,
    root: Path,
    attempts: int,
    pace: float,
    sleep: Callable[[float], None],
) -> SymbolOutcome:
    """一票：列表（带重试）→ 判据读盘 → 缺口抓正文 → 锚 → 幂等落盘。"""
    begin = start - timedelta(days=WINDOW_PAD_DAYS)
    try:
        items = _list_window(
            symbol,
            begin,
            end,
            lister=lister,
            attempts=attempts,
            pace=pace,
            sleep=sleep,
        )
    except Exception as exc:
        return SymbolOutcome(symbol, "failed", reason=f"列表取数失败：{exc}")

    # 收窗：列表多探的 WINDOW_PAD_DAYS 在这里滤掉——ann_date 才是本表的窗口语义。
    # 顺带滤掉 notice_date 归到明天的盘后公告（今天不入库，明天下一轮判据读盘补）。
    items = [item for item in items if start <= item.ann_date <= end]

    existing = {row.art_code for row in read_table("news", symbol, start, end, root=root)}
    # 同 art_code 去重（窗口多探与分页理论不重叠，防御一次）
    missing: list[NoticeItem] = []
    seen = set(existing)
    for item in items:
        if item.art_code in seen:
            continue
        seen.add(item.art_code)
        missing.append(item)

    if not missing:
        return SymbolOutcome(
            symbol,
            "skipped",
            listed=len(items),
            existing=len(existing),
            reason="窗口内公告全部已在盘（判据读盘零缺口）",
        )

    rows: list[NewsRow] = []
    failed = 0
    for item in missing:
        try:
            payload = _fetch(
                partial(reader, item.art_code),
                attempts=attempts,
                pace=pace,
                sleep=sleep,
            )
        except Exception:
            failed += 1  # 单条正文失败：记数跳过，续跑判据读盘下一轮补
            continue
        rows.append(row_of(item, parse_notice_content(payload)))

    problems, unanchored, kept = anchor_news(rows, today=end)
    if problems:
        tail = "".join(f"\n- {problem}" for problem in problems[:5])
        return SymbolOutcome(
            symbol,
            "failed",
            listed=len(items),
            existing=len(existing),
            failed=failed,
            reason="锚点对账整批拒（ADR-0015 决定 3，一行不写）：" + problems[0] + "".join(tail),
        )
    if not kept and failed >= len(missing):
        return SymbolOutcome(
            symbol,
            "failed",
            listed=len(items),
            existing=len(existing),
            failed=failed,
            reason=f"正文 {failed} 条全部抓取失败",
        )

    report = write_table(kept, table="news", root=root)
    return SymbolOutcome(
        symbol,
        "landed",
        listed=len(items),
        existing=len(existing),
        added=report.added,
        empty=unanchored,
        failed=failed,
        reason=f"空正文跳 {unanchored}、正文失败 {failed}" if (unanchored or failed) else "",
    )


def _report(
    outcomes: Sequence[SymbolOutcome], *, start: date, end: date, breaker_tripped: bool
) -> str:
    """markdown 报告（形态对齐 zx-relay backfill 的头/票/尾三段）。"""
    failed = [outcome for outcome in outcomes if not outcome.ok]
    head = [
        "# 消息组件回填 · news · 东财公告",
        "",
        f"- 窗口：{start.isoformat()} .. {end.isoformat()}（--days，按 ann_date 收窗）",
        f"- 票池：{len(outcomes)} 票" + ("；**熔断收工**（--breaker）" if breaker_tripped else ""),
        "- 供数源：`eastmoney`（np-anotice 列表 + np-cnotice 正文，探测报告 §2.1）",
        "",
    ]
    lines = list(head)
    for outcome in outcomes:
        mark = "✓" if outcome.ok else "✗"
        line = (
            f"- {mark} `{outcome.symbol}` {outcome.status}：列表 {outcome.listed} 条、"
            f"盘上已有 {outcome.existing}、新增 {outcome.added}"
        )
        if outcome.reason:
            line += f"——{outcome.reason}"
        lines.append(line)
    covered = sum(outcome.added for outcome in outcomes)
    breaker_skipped = sum(1 for outcome in outcomes if outcome.status == "breaker")
    lines += [
        "",
        f"- 落盘：本 run 新增 {covered} 行（判据读盘 = 窗口内已有 art_code 差集；"
        "复跑同票应为 0 新增——幂等由 write_table 主键保证）",
        f"- 失败 {len(failed) - breaker_skipped} 票、熔断跳过 {breaker_skipped} 票",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """`python -m zhixing_quant.sources.akshare.notice`：消息组件回填入口。

    退出码 0 跑完无失败 / 1 有失败票 / 2 熔断或参数错（与 zx-relay 同义）。
    """
    parser = argparse.ArgumentParser(
        prog="notice",
        description="东财公告（列表+正文）→ news 参考表：判据读盘可续跑（探测报告 2026-09-25）",
    )
    parser.add_argument("--symbols", required=True, help="逗号分隔的 6 位代码，如 600519,300308")
    parser.add_argument(
        "--days", type=int, default=90, help="回填窗口自然日（模板 30 交易日的余量）"
    )
    parser.add_argument("--attempts", type=int, default=3, help="每发请求的重试次数")
    parser.add_argument("--breaker", type=int, default=5, help="连续失败几票后熔断")
    parser.add_argument("--pace", type=float, default=0.3, help="请求间隔秒（限速纪律，默认 0.3）")
    parser.add_argument(
        "--out", type=Path, default=None, help="报告目录（默认 ${ZX_DATA_ROOT}/reports/news）"
    )
    args = parser.parse_args(argv)
    symbols = [code.strip() for code in args.symbols.split(",") if code.strip()]
    if not symbols:
        parser.error("--symbols 至少要一个 6 位代码")
    if args.days < 1:
        parser.error("--days 得是正数")
    try:
        markdown, code = backfill(
            symbols,
            days=args.days,
            root=config.parquet_dir(),
            directory=args.out if args.out is not None else config.reports_dir() / "news",
            attempts=args.attempts,
            breaker=args.breaker,
            pace=args.pace,
            progress=lambda line: print(line, flush=True),
        )
    except ValueError as exc:
        print(f"参数/输入错（退出码 2）：{exc}")
        return 2
    print()
    print(markdown)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
