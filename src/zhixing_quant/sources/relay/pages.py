"""转接源的分页拉取与截断硬响（ADR-0015 决定 5"拉全"的那一半，pull 与 backfill 共用）。

**截断必须硬响**（2026-09-22 实测）：rds 在 offset≥5000 处一律返回空页，而响应里
`count=5644, has_more=True` 照给——"下一页空"在该源有两种含义（拉完了 / 源截断了），只有
`has_more` 能分辨。把它当"拉完了"就是无声丢 11% 的行，那种丢失在任何报告里都不会自己出现。

分页元数据按源分两派（2026-09-22 / 2026-09-25 实测），**两种形状并存、两套守卫，不许混判**：

1. **has_more 族**（daily/daily_basic/stk_limit/forecast…）：rds 给 has_more/count、单次查询
   5000 行静默截断；promax **不认 limit/offset**，一次全吐（5644 行照回，has_more/count 均无）。
   没有元数据的响应没有"下一页"可言——继续翻页只会把同一批行拉两遍撞"页内重复"，所以
   has_more 缺席就地返回。守卫：`has_more=True` + 空页 = 截断，硬响。
   **另一条实测（2026-09-25）**：高基数接口的单请求 limit 上限是 **1000**——`forecast` 传
   5000 直接 HTTP 400 `query limit is too large for this high-cardinality interface,
   max_limit:1000`。这不是数据问题，是参数问题：发请求前就拒（fail-closed，transport 零调用），
   上限按表登记在 `MAX_PAGE_SIZE`，没登记的按 5000（日表族实测在 5000 处静默截断而非 400）。
2. **`report_rc` 族**（2026-09-25 实测，600519.SH 五次请求）：`has_more` **恒 False**、
   `count` 是 limit 回显不是总数；`limit=5001` 静默回 5000 行；`offset=5000&limit=100` 回
   HTTP 200 **0 行**；带窗口的查询各自顶 5000（`20150101..20240630` 同样回满 5000，且比不带
   窗口的首行更早——单查询静默只留**最新** 5000 行，更早的行被丢）。has_more 守卫对它完全
   失效（它从不举手）。这一族走 `fetch_pages_short`：**页 < limit 即到底 + 撞 5000 顶按
   start_date/end_date 二分缩窗**——只有"没顶满"的查询才能把短页读成拉完；顶满说明可能还有
   被静默丢掉的更早行，切窗查到每窗 <5000 为止，单日窗仍顶满则硬响。

守卫住在本文件的两个函数里而不是各调用点各写一份：pull 的"按日全市场"与 backfill 的
"逐票区间"撞的是同一个 5000 行上限，两份守卫迟早漂成"一边响一边静默丢"。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

#: 抓取边界：一张表 + 一组参数 → (供数源, 响应体)。生产上就是 `relay.client.fetch`。
FetchFn = Callable[..., tuple[str, dict[str, Any]]]

#: 单请求 limit 的默认上限。日表族 2026-09-22 实测在 5000 处静默截断（守卫按 has_more 响），
#: 不是 400——所以没单独登记的表按 5000 放行。
DEFAULT_MAX_PAGE_SIZE = 5000

#: 表 → 单请求 limit 上限（rds 网关按接口给 max_limit，超了是 400 参数错不是数据错）。
#: 只登记**实测过**的表：`forecast` 2026-09-25 全市场回填实测 limit=5000 → HTTP 400
#: `query limit is too large for this high-cardinality interface, detail.max_limit=1000`。
#: fina_audit/stk_holdernumber 未实测，不猜——它们要接时先单接口最小验证再登记。
MAX_PAGE_SIZE: Mapping[str, int] = {"forecast": 1000}

#: `report_rc` 族（has_more 恒 False）：按源的 5000 行顶对齐分页的表名单。
SHORT_PAGE_TABLES: frozenset[str] = frozenset({"report_rc"})

#: `report_rc` 单查询的静默行顶（limit=5001 实测回 5000）。这一族只有对齐到顶的整页翻法
#: 才能靠"页 < limit 即到底"判定到底——页小了会在第 5000 行处撞源的顶，把截断读成拉完。
SOURCE_ROW_CAP = 5000

#: 二分缩窗的默认左界：params 没带 start_date 时的第一刀左端。1990 早于任何 A 股研报史
#: （实测最早 2019-03-29 起），空窗一页 0 行即底，不值当一个专门参数。
DEFAULT_WINDOW_START = date(1990, 1, 1)


def page_limit_of(table: str) -> int:
    """这张表单请求允许的 limit 上限（按表登记，未登记按 5000）。"""
    return MAX_PAGE_SIZE.get(table, DEFAULT_MAX_PAGE_SIZE)


def fetch_pages(
    table: str,
    params: Mapping[str, object],
    *,
    fetch: FetchFn,
    page_size: int = 5000,
) -> tuple[str, list[list[str]], list[str], int | None]:
    """has_more 族：按 limit/offset 翻完整批。返回 (供数源, items, fields, 源声称总行数或 None)。

    `params` 是调用方的查询条件（trade_date / ts_code / start_date / end_date…），这里只叠
    limit/offset 并执行两道守卫：
    - **发前拒**：`page_size` 超过该表上限（如 forecast 的 1000）→ 当场 ValueError，一个请求
      都不发——超上限的 400 是参数错，重发一万次也是 400，不许烧 transport。
    - **截断硬响**：翻到 has_more 还在说"有下一页"而下一页为空，就地抛 `ValueError`——那是源的
      单查询行数上限，半份数据比没有数据更危险。
    """
    cap = page_limit_of(table)
    if page_size > cap:
        raise ValueError(
            f"{table} 单请求 limit 上限 {cap}"
            + (
                "（rds 高基数接口实测 max_limit=1000，超了直接 HTTP 400）"
                if table in MAX_PAGE_SIZE
                else "（本源单查询行数上限）"
            )
            + f"，收到 page_size={page_size}——超上限的请求发前就拒，不发必错的那一发"
        )
    items: list[list[str]] = []
    fields: list[str] = []
    source = ""
    offset = 0
    total: int | None = None
    while True:
        source, body = fetch(table, {**params, "limit": page_size, "offset": offset})
        data = body.get("data") or {}
        page_fields = [str(f) for f in data.get("fields") or []]
        page_items = [[str(v) for v in row] for row in data.get("items") or []]
        if not fields:
            fields = page_fields
        elif page_fields and page_fields != fields:
            raise ValueError(f"offset={offset} 页的 fields 变了：{page_fields} != {fields}")
        raw_count = data.get("count")
        if isinstance(raw_count, int):
            total = raw_count
        items.extend(page_items)
        if data.get("has_more") is None:
            return source, items, fields, total
        if len(page_items) < page_size:
            if data.get("has_more") and total is not None and len(items) < total:
                described = "、".join(f"{key}={value}" for key, value in params.items())
                raise ValueError(
                    f"{source} {table}（{described}）在 offset={offset} 处截断："
                    f"已取 {len(items)} 行、源声称共 {total} 行、下一页为空——"
                    "本源单次查询有行数上限，全市场拉取不可用，改用 --symbols 逐票拉取"
                )
            return source, items, fields, total
        offset += page_size


def fetch_pages_short(
    table: str,
    params: Mapping[str, object],
    *,
    fetch: FetchFn,
    page_size: int = SOURCE_ROW_CAP,
    today: date | None = None,
) -> tuple[str, list[list[str]], list[str], int | None]:
    """`report_rc` 族：**页 < limit 即到底** + 撞 5000 行顶按日期二分缩窗。

    返回形状与 `fetch_pages` 一致；`total` 恒为 None——这一族的 `count` 是 limit 回显不是
    总数（2026-09-25 实测：limit=100 回 count=100 而实际行数 ≥6870），拿它当总数会把
    "回显"读成"拉完"。

    两条硬规矩（都是实测逼出来的）：

    1. **`page_size` 必须恰为 5000**（发前拒，transport 零调用）。`limit=5001` 会被静默顶成
       5000——比顶大的请求会让"页 < limit"恒真，把截断读成拉完；比顶小的页翻到第 5000 行
       会撞同一堵墙（offset+limit 跨顶实测回空页），同样把截断读成拉完。只有对齐到顶的整页
       才能无歧义地判"这一页满没满"。
    2. **满页 ≠ 到底**。单查询静默只留最新 5000 行（实测：不带窗口的全史首行 20210826，
       而 `20150101..20240630` 窗口内能取到 20190329——更早的行在全史查询里被丢了）。所以
       页满时按 `start_date/end_date` 对半切窗重查（左窗到 mid、右窗 mid+1 起，单调不重叠），
       直到每窗 <5000；切到单日仍满 → 硬响（一天 5000 行研报不是数据是事故）。
    """
    if page_size != SOURCE_ROW_CAP:
        raise ValueError(
            f"{table} 只认 page_size={SOURCE_ROW_CAP}（源的单查询静默行顶），收到 {page_size}："
            f"limit={SOURCE_ROW_CAP + 1} 实测被静默顶成 {SOURCE_ROW_CAP} 行、"
            "跨顶/≥顶的 offset 实测回空页——不对齐到顶的分页会把截断读成拉完，发前就拒"
        )
    moment = today if today is not None else date.today()
    items: list[list[str]] = []
    fields_pages: list[list[str]] = []
    last_source = ""

    def window(bound: Mapping[str, object]) -> None:
        nonlocal last_source
        source, body = fetch(table, {**bound, "limit": page_size, "offset": 0})
        last_source = source
        data = body.get("data") or {}
        fields_pages.append([str(f) for f in data.get("fields") or []])
        page = [[str(v) for v in row] for row in data.get("items") or []]
        if len(page) > page_size:
            # 源忽略 limit 一次全吐（promax 形）：行比问的还多，就是它能给的全部。
            items.extend(page)
            return
        if len(page) < page_size:
            # 页 < limit 即到底——本族唯一的"拉完"信号（has_more 恒 False 顶不上来）。
            items.extend(page)
            return
        start, end = _window_bounds(bound, today=moment)
        if start >= end:
            described = start.isoformat() if start == end else f"{start}..{end}"
            raise ValueError(
                f"{source} {table} 窗口 {described} 单窗仍返回满页 {page_size} 行——"
                "已切到单日仍撞源的 5000 行静默顶，这一页可能被截断；该窗不是数据是事故，硬响"
            )
        midpoint = start + (end - start) / 2
        window({**bound, "start_date": _fmt(start), "end_date": _fmt(midpoint)})
        window(
            {
                **bound,
                "start_date": _fmt(date.fromordinal(midpoint.toordinal() + 1)),
                "end_date": _fmt(end),
            }
        )

    window(params)
    fields: list[str] = []
    for page_fields in fields_pages:
        if not page_fields:
            continue
        if not fields:
            fields = page_fields
        elif page_fields != fields:
            raise ValueError(f"缩窗前后各页的 fields 变了：{page_fields} != {fields}")
    return last_source, items, fields, None


def _fmt(day: date) -> str:
    return day.strftime("%Y%m%d")


def _window_bounds(params: Mapping[str, object], *, today: date) -> tuple[date, date]:
    """本次查询的日期二分界：params 自带 start_date/end_date 就用它，没带用默认全史界。"""
    return (
        _parse_ymd(params.get("start_date"), DEFAULT_WINDOW_START, field="start_date"),
        _parse_ymd(params.get("end_date"), today, field="end_date"),
    )


def _parse_ymd(value: object, default: date, *, field: str) -> date:
    if value is None or value == "":
        return default
    text = str(value)
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except (TypeError, ValueError):
        raise ValueError(
            f"{field}={value!r} 不是 YYYYMMDD：切窗要拿它当对半的界，解不出不切"
        ) from None
