"""源行的取值归一：与具体源无关的那一小层。

放在 `sources/` 而不是 `domain/`：这些函数判的是"外部给的这个值能不能变成一个字段"，
是源边界上的事。`domain/` 只管已经归一好了的数据。

统一取向：**归一不出来就留 None，交给门禁判**（04 §二 R010）。适配器不替源"猜"一个值，
也不抛 KeyError 把采集炸成半批入库——除非少一个值就等于判据本身变了，见
`calendar_from_rows`（日历少一天不是"数据差一点"，是 R006 的口径变了）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime


class SourceSchemaError(ValueError):
    """源给了没见过的形状：列名全变、一行都没解析出来。"""


#: 一组源行。**抓取层与适配层的共同词汇**，所以住在这里而不是某个源的子包里：
#: `jobs.daily.Fetcher` 的返回值与 `akshare.daily.daily_drafts` 的入参必须是同一个形状，
#: 各写一遍迟早漂成两份契约，而漂了的那份只会在真网络上出错——CI 里两边各自都"对"。
Rows = Sequence[Mapping[str, object]]
#: (不复权, 后复权) 两帧。R002 判涨跌停要看不复权价，干净区存的也是不复权价，后复权帧只
#: 用来算 `adj_factor`（`akshare.daily._factor`），所以两帧只能一起交进来。
Pair = tuple[Rows, Rows]


def to_float(value: object) -> float | None:
    """源字段 → float。缺列/空值给 None（交 R010），NaN 原样留着（交 R001/R002）。

    把 NaN 归成 None 是最坏的一种"清洗"：门禁看到的是"字段没给"，而真相是"源给了一个
    不成立的数"。两者的处置不同——前者要去找源，后者要拒收这批。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        return None  # True 不是 1：布尔出现在数值列里说明列映射错了，让它缺着被门禁抓到
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def to_date(value: object) -> date | None:
    """源日期 → `date`。认 `date`/`datetime`/ISO 串；认不出给 None，不猜。

    三处取舍都不是随手定的：

    - `datetime` 必须显式降回 `date`。留着时分秒，`(symbol, trade_date)` 这个主键就成了
      两个字段，日线的重复判定（R008）再也对不上。
    - 紧凑形 `20240102` 是 ISO 8601 basic 格式，Python 3.11+ 认它——腾讯与东财的历史接口
      原样返回这个形状，拒它等于把一整批数据判成"缺交易日"。
    - 带空格的串先截到 10 位再解析（`fromisoformat` 不认空格式）；epoch 秒不认，猜时区
      就是造日期。
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()[:10].replace("/", "-")
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    return None


def to_datetime(value: object) -> datetime | None:
    """源时间戳 → naive `datetime`（分钟K的收盘时刻，ADR-0009 决定 2）。认不出给 None。

    与 `to_date` 同一条取向：不猜。两处刻意的严格是要的：

    - **只有日期不算时刻**。`2026-09-18` 补成 00:00 会造出一根"当天第一根K线"，而源其实
      没给分钟——那根不存在的K线在回测里会被当成真实事件。
    - **带时区的拒收**。A 股分钟K的收盘时刻是交易所本地时间，源给个 `+08:00` 出来说明
      口径不明；替它换算一次就是一条时间轴整体偏移，而 R009（乱序）判不出来。
    """
    if isinstance(value, datetime):
        return None if value.tzinfo is not None else value
    if isinstance(value, str):
        text = value.strip().replace("/", "-")
        # `fromisoformat("2024-01-02")` 与 `("20240102")` 都成功，给的是午夜（实测）。所以
        # "只有日期"这件事不能靠解析失败来表达，得自己问：日期与时刻之间那个分隔符在不在。
        if " " not in text and "T" not in text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return None if parsed.tzinfo is not None else parsed
    return None


def pick(row: Mapping[str, object], *aliases: str) -> object:
    """按别名顺序取第一个存在的列。各交易所的同一列有三种叫法（证券代码/A股代码/code）。

    只认"列在不在"，不认"值真不真"：`{"code": None}` 也算取到了，交给 `to_*` 判 None——
    列在而值为空是数据问题，列不在是协议问题，两者都到这里为止分开。
    """
    for key in aliases:
        if key in row:
            return row[key]
    return None
