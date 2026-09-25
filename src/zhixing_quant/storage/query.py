"""单一查询接口：调用方既不感知文件布局，也不自己换算权（ADR-0003 决定 2、Step 3 验收 1/2）。

一个函数管三个口径。拆成三个函数的话，"怎么算复权"就有两份写法，迟早漂，所以换算全部
走 `domain.adjust`；本模块只回答两个问题——因子从哪来（干净区每行的 `adj_factor`）、
基准取哪一天（`domain.adjust` 的口径：区间末日）。

口径的取舍要在使用前说清，不然发现它的场合是回测跑完之后：

- `backward` 只取决于当天及以前的因子，与查询区间无关。回测用它。
- `forward` 的基准是区间末日（前复权的定义如此，见 `domain.adjust` 模块开头），所以同
  一天的前复权价会随窗口末尾变化。这不是 bug，但跨窗口比对价格前先想起这一句。
- 量与额不复权：`volume` 是股、`amount` 是元，源给什么存什么（04 §五 跨源对账按这个口径比）。
  复权只动价格，"复权成交量"没有谁认的口径。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import duckdb

from zhixing_quant.domain.adjust import (
    AdjustmentFactor,
    factor_at,
    ladder_days,
    sorted_factors,
    to_backward,
    to_forward,
)
from zhixing_quant.domain.bar import Bar
from zhixing_quant.domain.symbol import normalize_code
from zhixing_quant.storage import layout
from zhixing_quant.storage.layout import Record

#: 三种口径。`Literal` 而不是 `Enum`：调用方传的是配置里的一个词，不是要拿去比较的对象。
Adjust = Literal["raw", "backward", "forward"]


class Unadjustable(ValueError):
    """要复权价，但这只票一个因子都没有。

    不拿 1.0 兜底：那等于把"源没给复权数据"洗成"这只票从没除权过"，前后两个口径会给出
    一模一样的价格，谁也不会发现复权其实没生效（`sources/akshare/daily.py` 的 `_factor`
    在源头就是同样的取舍）。
    """


@dataclass(frozen=True)
class Cover:
    """一个 dataset 在盘上覆盖了多久（07 §5.1 要的"可回测区间"就是这几个数）。

    `days` 数的是**有行的交易日**而不是首末之间的日历天数：跨一个周末就多半报两天，而 07 §5.3
    的段长要的是"能切出多少天样本"。`symbols` 一起给，是因为"42 天 × 1 只票"与"42 天 × 300 只"
    在回测里根本不是同一件事。
    """

    first: date
    last: date
    days: int
    symbols: int


def read_bars(
    symbol: str,
    start: date,
    end: date,
    *,
    adjust: Adjust = "raw",
    dataset: str = layout.DAILY,
    root: Path | None = None,
) -> list[Bar]:
    """区间内的干净区K线，按时间升序。没落过盘的票返回空列表。

    分钟 dataset 同一条查询：`start`/`end` 筛的是**交易日**（`trade_date` 仍在每行上），
    区间内每天的所有K线都在结果里，顺序是"日、再日内时刻"。复权价要日线因子（`_factors`），
    所以分钟 dataset 配 `adjust != "raw"` 时，日线那天必须也在盘上——这不是可商量的口径，
    分钟价 × 当日日线因子是 ADR-0009 决定 4 给的唯一换算路径。
    """
    if end < start:
        raise ValueError(f"区间颠倒了：{start} 晚于 {end}")
    spec = layout.dataset_spec(dataset)
    code = normalize_code(symbol)
    paths = [
        path
        for path in layout.partitions(code, start, end, dataset=dataset, root=root)
        if path.is_file()
    ]
    if not paths:
        return []
    columns = ", ".join(spec.names)
    # 排序键跟着 dataset 走：日线只有交易日，分钟线再加一个 `ts`
    order = ", ".join(spec.order)
    with duckdb.connect() as con:
        fetched: list[tuple[Any, ...]] = con.execute(
            f"SELECT {columns} FROM read_parquet(?) "
            f"WHERE trade_date BETWEEN ? AND ? ORDER BY {order}",
            [[str(path) for path in paths], start, end],
        ).fetchall()
        rows = [Record(*row) for row in fetched]
        factors: tuple[AdjustmentFactor, ...] = ()
        if adjust != "raw":
            factors = _factors(con, code, root=root, up_to_year=end.year)
    bars = [_to_bar(row) for row in rows]
    return adjusted_bars(bars, adjust=adjust, factors=factors)


def factors_for(
    symbol: str, *, root: Path | None = None, up_to_year: int = 9999
) -> tuple[AdjustmentFactor, ...]:
    """一只票的复权因子阶梯（公开读口）。因子源恒为日线 dataset——`_factors` 那条纪律的
    对外形状，供「Bar 不在盘上」的路径换算口径（站点分钟K实时获取，ADR-0026）：
    实时抓回的原始价分钟 Bar 配这份日线因子，走同一条 `adjusted_bars` 换算。

    `up_to_year` 默认到顶：展示路径不存在"区间之后的除权回头改价"的复现性问题——
    它要的就是**此刻**的后复权口径；回测/读盘路径仍走 `read_bars` 里的截断语义。
    """
    code = normalize_code(symbol)
    with duckdb.connect() as con:
        return _factors(con, code, root=root, up_to_year=up_to_year)


def depth(dataset: str, *, root: Path | None = None) -> Cover | None:
    """整个 dataset 在盘上的覆盖范围；一格都没落过时给 None（不是"0 天"）。

    一次聚合而不是 `read_bars` 再把天数数出来：全市场分钟线一年 6500 万行，为了问"有多少天"
    而把所有行拉进 Python，等于把 Parquet 的 min/max 统计白扔掉。

    查的是**所有**分区而不是某只票：07 §5.1 要的是"这条管道从哪天开始有数据"，那是盘的事实。
    """
    layout.dataset_spec(dataset)  # 认不出的 dataset 名要在这里响，不是 glob 出一个空目录再报 None
    paths = layout.dataset_partitions(dataset, root=root)
    if not paths:
        return None
    with duckdb.connect() as con:
        row: tuple[Any, ...] = con.execute(
            "SELECT min(trade_date), max(trade_date), count(DISTINCT trade_date), "
            "count(DISTINCT symbol) FROM read_parquet(?)",
            [[str(path) for path in paths]],
        ).fetchone()
    if row[0] is None or row[1] is None:
        # 文件在、行是零：DuckDB 给 NULL 而不是报错。别把它包成一个首末日为 None 的 Cover。
        return None
    return Cover(
        first=row[0],
        last=row[1],
        days=int(row[2]),
        symbols=int(row[3]),
    )


def adjusted_bars(
    bars: Sequence[Bar], *, adjust: Adjust, factors: Sequence[AdjustmentFactor]
) -> list[Bar]:
    """把不复权行换成指定口径。**全项目只有这里做口径切换**，别的模块不许自己乘因子。

    拆成独立函数是为了属性测试能直接轰它：恒等式讲的是换算，与 Parquet 读写无关，把 IO
    塞进循环只会让一万例跑一晚上，还测不到别的形状。
    """
    if adjust == "raw" or not bars:
        return list(bars)
    series = sorted_factors(factors)
    if not series:
        raise Unadjustable(
            f"{bars[0].symbol} 没有可用复权因子，给不出 {adjust} 价：先确认 hfq 帧抓到了"
        )
    days = ladder_days(series)
    base = factor_at(series, days, bars[-1].trade_date) if adjust == "forward" else 1.0
    out: list[Bar] = []
    for bar in bars:
        factor = factor_at(series, days, bar.trade_date)
        raw = (bar.open, bar.high, bar.low, bar.close)
        if adjust == "backward":
            scaled = tuple(to_backward(value, factor) for value in raw)
        else:
            scaled = tuple(to_forward(value, factor, base) for value in raw)
        out.append(_rescaled(bar, scaled))
    return out


def _factors(
    con: Any,
    code: str,
    *,
    root: Path | None,
    up_to_year: int,
) -> tuple[AdjustmentFactor, ...]:
    """这只票已知的复权因子阶梯，按变化点给出。**来源恒为日线 dataset**（ADR-0009 决定 4）。

    分钟线自己不带因子（`adj_factor` 为空）：因子是阶梯函数、日内不变，分钟级复权价 = 分钟价 ×
    当日日线因子。所以这里不问"bars 来自哪个 dataset"——问了也只有日线一个答案，而那正是
    "全项目只有一处换算式"要的形状。

    往前读到分区头、往后读到 `up_to_year` 就停：区间首日之前那次除权仍然决定区间内的
    价格，只看区间内的因子会把一只 2015 年除权、此后没动过的票在 2020 年段算回 1.0；
    而区间之后那次除权不该回头改区间内的价格，否则同一天的后复权价会随着"这只票后来又
    除权了"而变化，回测就再也复现不出来。

    留变化点（`!=` 而不是逐日全给）压的是"连续几天同一个因子"那种形状。它压不动真数据：盘上的
    因子是 hfq收盘 ÷ 原始收盘 的商，两个都只到分，商在第 6-7 位小数上每天抖一下，于是阶梯点数
    约等于日线行数（600519：63 行 63 点，坑 #37 就是这么量出来的）。所以查询侧的代价不靠这里
    兜，靠 `adjust` 那一段一次排序 + 每根K线二分（`domain.adjust.factor_at`）。
    """
    paths = [
        path
        for path in layout.existing_partitions(code, dataset=layout.DAILY, root=root)
        if layout.year_of(path) <= up_to_year
    ]
    if not paths:
        return ()
    fetched: list[tuple[date, float]] = con.execute(
        "SELECT trade_date, adj_factor FROM read_parquet(?) "
        "WHERE adj_factor IS NOT NULL ORDER BY trade_date",
        [[str(path) for path in paths]],
    ).fetchall()
    events: list[AdjustmentFactor] = []
    previous: float | None = None
    for day, value in fetched:
        if value != previous:
            events.append(AdjustmentFactor(code=code, effective_on=day, factor=value))
            previous = value
    return tuple(events)


def _to_bar(row: Record) -> Bar:
    """落盘行 → `Bar`。字段名逐个写出来（不 `zip` 列名）：那圈魔法下标是列序错位时唯一
    还能"看起来对"的地方，而这里一旦错位，`model_validate` 会安静地少校验一个字段。
    加一列时 `Record` 与 dataset 的形状先对不上，比在这里靠运气好。
    """
    return Bar.model_validate(
        {
            "source": row.source,
            "symbol": row.symbol,
            "trade_date": row.trade_date,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "amount": row.amount,
            "ts": row.ts,
            "adj_factor": row.adj_factor,
            "is_suspended": row.is_suspended,
        }
    )


def _rescaled(bar: Bar, prices: Sequence[float]) -> Bar:
    """整行照搬，只换四个价格，并重新过一遍 `Bar` 的不变量。

    不用 `model_copy(update=...)`：它跳过校验。"正数缩放保序保正"是推理出来的结论，推理
    错了（因子为负、基准为 0）就该在这里响，而不是让一张坏K线安静地进策略。
    """
    open_, high, low, close = prices
    return Bar(
        source=bar.source,
        symbol=bar.symbol,
        trade_date=bar.trade_date,
        ts=bar.ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=bar.volume,
        amount=bar.amount,
        adj_factor=bar.adj_factor,
        is_suspended=bar.is_suspended,
    )
