"""转接源的分页拉取与截断硬响（ADR-0015 决定 5"拉全"的那一半，pull 与 backfill 共用）。

**截断必须硬响**（2026-09-22 实测）：rds 在 offset≥5000 处一律返回空页，而响应里
`count=5644, has_more=True` 照给——"下一页空"在该源有两种含义（拉完了 / 源截断了），只有
`has_more` 能分辨。把它当"拉完了"就是无声丢 11% 的行，那种丢失在任何报告里都不会自己出现。

分页元数据按源分两派（2026-09-22 实测）：rds 给 has_more/count、单次查询 5000 行静默截断；
promax **不认 limit/offset**，一次全吐（5644 行照回，has_more/count 均无）。没有元数据的响应
没有"下一页"可言——继续翻页只会把同一批行拉两遍撞"页内重复"，所以 has_more 缺席就地返回。

守卫住在 `fetch_pages` 一处而不是各调用点各写一份：pull 的"按日全市场"与 backfill 的
"逐票区间"撞的是同一个 5000 行上限，两份守卫迟早漂成"一边响一边静默丢"。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

#: 抓取边界：一张表 + 一组参数 → (供数源, 响应体)。生产上就是 `relay.client.fetch`。
FetchFn = Callable[..., tuple[str, dict[str, Any]]]


def fetch_pages(
    table: str,
    params: Mapping[str, object],
    *,
    fetch: FetchFn,
    page_size: int = 5000,
) -> tuple[str, list[list[str]], list[str], int | None]:
    """按 limit/offset 翻完整批。返回 (供数源, 原始 items, fields, 源声称的总行数或 None)。

    `params` 是调用方的查询条件（trade_date / ts_code / start_date / end_date…），这里只叠
    limit/offset 并执行守卫：翻到 has_more 还在说"有下一页"而下一页为空，就地抛
    `ValueError`——那是源的单查询行数上限，半份数据比没有数据更危险。
    """
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
