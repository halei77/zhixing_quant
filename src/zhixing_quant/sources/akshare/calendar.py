"""akshare 交易日历：源行 → `TradingCalendar`（04 §五 接入清单第 2 项）。

和日线适配器一样不碰网络——`tool_trade_date_hist_sina()` 的调用留在 `tools/`，本层只把
行变成日历，黄金样本才能离线重放（03 §二 L2）。

这一层是全套适配器里唯一**不许把坏值留成 None** 的：日线少一个字段只是那一行进隔离区，
日历少一天改的是判据本身。所以这里抛 `SourceSchemaError`，不静默丢行。
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from zhixing_quant import config
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.sources.rows import SourceSchemaError, pick, to_date

#: `tool_trade_date_hist_sina()` 的列名；后两个别名给别的源复用同一个入口。
DATE_ALIASES = ("trade_date", "日期", "calendar_date")
#: 日历快照的文件名，与 `tools/capture_golden.py` 的 key 一致——两处不同名就等于两份日历。
SNAPSHOT_NAME = "tool_trade_date_hist_sina.csv"


def snapshot_path(directory: Path | None = None) -> Path:
    """日历快照的位置（数据根 `golden/`，见 `config.golden_dir`）。"""
    return (directory if directory is not None else config.golden_dir()) / SNAPSHOT_NAME


def load_calendar(csv_path: Path | None = None, *, until: date | None = None) -> TradingCalendar:
    """读快照文件成日历；`until` 用来钉住"日历得覆盖到这天"。

    `until` 不是可选的保险：日历不覆盖目标日时 R006 永远不会报"缺交易日"，门禁表现为
    "全部通过"——最坏的一种失效。所以覆盖不到就抛，让人去重抓快照，而不是静默判空。
    """
    path = csv_path if csv_path is not None else snapshot_path()
    if not path.is_file():
        raise SourceSchemaError(
            f"没有日历快照 {path}：先跑 tools/capture_golden.py（日历缺一天等于改 R006 判据）"
        )
    with path.open(encoding="utf-8", newline="") as fh:
        calendar = calendar_from_rows(list(csv.DictReader(fh)))
    if until is not None and until > calendar.days[-1]:
        raise SourceSchemaError(
            f"日历快照 {path} 只到 {calendar.days[-1]}，判不了 {until} 是不是交易日"
        )
    return calendar


def calendar_from_rows(rows: Sequence[Mapping[str, object]]) -> TradingCalendar:
    """源行 → 交易日历。任一行解析不出来就整批拒收。

    两件事足以说明"丢一行"不是小错：

    - R006 判"交易日缺失"靠的就是这份日历。日历里没那天，真实数据里缺那天就永远不会被
      报出来——门禁最坏的一种失效，因为它表现为"全部通过"。
    - 空日历更糟：`is_trading_day` 对任何日期都答 False，于是整批数据看起来都不该有K线。

    重复行不去重也不报错：`TradingCalendar` 自己按集合装，源给两次同一天不构成事实冲突。
    """
    days: list[date] = []
    for position, row in enumerate(rows):
        raw = pick(row, *DATE_ALIASES)
        day = to_date(raw)
        if day is None:
            raise SourceSchemaError(
                f"交易日历第 {position} 行的日期 {raw!r} 解析不出来："
                "日历缺一天等于改 R006 的判据，不在这里静默丢行"
            )
        days.append(day)
    if not days:
        raise SourceSchemaError("收到 0 行交易日历：空日历会让每个交易日判定都变成 False")
    return TradingCalendar(days)
