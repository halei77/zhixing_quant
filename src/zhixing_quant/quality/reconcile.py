"""R011：分钟合成日K 与日线的交叉对账（01 Step 4 验收 2；ADR-0009 决定 6）。

它不在 `config/gate.toml` 的 `[[rule]]` 表里，理由不是"还没来得及加"：门禁引擎一次只看一批、
一个粒度，且按设计不认识存储层（它的注入点只有主数据与日历）。R011 要的是**另一个 dataset 的
同一天**——那是跨盘的事实，为它给引擎开第三个口子，换来的是一条本来也进不了逐行判定循环的规则
（一天 48 根分钟K线会把同一条偏差报 48 次）。所以判据住在这里，容差与 R004/R007 同源：
`tolerance_pct` 由调用方从 `gate_config` 的 `[defaults]` 给进来，"多少算噪声"全项目只有一个数。

五种坏法是分开的五句话，各自一个 `kind`（04 §二 末尾把这一条列成表外规则）：

- `no_minutes`：当天有日线、分钟一根都没有。**这是防断档的那一半**。尾巴每天随抓取整段重落盘，
  所以漏一天通常明天就补回来了；这条报的是"补不回来"的那个征兆——连着几天没跑，等回过神来那天
  已经滑出 1970 根的窗口，而分钟线没有更早的回填手段（ADR-0009 代价三）。
- `no_daily`：分钟有一堆、日线那天没有。多半是日线任务今天没抓到这只票，两本账对不上就是事故。
- `price`：开盘/收盘不相等。**历史上不等过**：2024-09-06..2026-09-18 全量对账（池内五只 ×
  三个周期，3,895 个 (票,天,周期) 组合）里 5 条不等，摊到 3 个 (票,天) 上，最大 0.51 元
  （600519 2026-06-25 开盘：分钟合成 1207.51 vs 日线 1207）。等号照写：两边读的都是不复权价，
  同一天的开盘价不可能两个都对，所以"不等"这句话永远不需要容差。要重议的是"报了算不算事故"，
  不是判据本身。
- `extremes`：分钟极值**越过**日线极值。反方向（合成值落在日线区间内）是常态——日内瞬时最高价
  可以发生在某一根 5 分钟K线内部，日线看得见、分钟聚合看不见。那 3,895 个组合里 0 条。
- `volume`：量或额相对偏差超过容差。同一批全量实测：330 条、165 个 (票,天,周期) 组合、
  105 个 (票,天)、84 个独立交易日（按周期分 8/55/102）。偏差**恒为一个方向**（合成比日线少），
  区间 -0.50%~-8.64%（最差 600519 2025-05-27 的 60 分钟）。留容差不写等号的理由不变，
  但**尾差不是收盘集合竞价**：15:00 那根每个周期都在盘上且量不为零（5min 48 根、30min 8 根、
  60min 4 根，一根不缺），而同一天的缺口在三个周期间互不相等（2026-09-14 的 000001：5min 少
  2,468,100 股、30min 少 5,851,400 股、60min 一分不少）。能在盘中定位差额落在哪根的那一天
  （2026-09-14，只有 5min 覆盖满的日子才定位得了），差额在上午 11:00/11:30 那根粗K上——
  同一段时间 5 分钟侧合计 3,385,300 股，30 分钟侧那根只有 2,000 股。
  所以这条抓到的是**源自己的几本账不一致**，不是我们的聚合少加了。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

from zhixing_quant.domain.aggregate import daily_from_minutes
from zhixing_quant.domain.bar import Bar
from zhixing_quant.storage.query import Cover

#: 五种坏法的名字。写在这里而不是散在渲染里：日报的计数与告警的分组都按它来。
KINDS = ("no_minutes", "no_daily", "price", "extremes", "volume")


@dataclass(frozen=True)
class Finding:
    """一条对不上的账。`dataset` 是分钟侧的名字（`minute_5`），日线侧不用说：只有一个。"""

    kind: str
    symbol: str
    day: date
    dataset: str
    detail: str


@dataclass(frozen=True)
class Recon:
    """一次对账的账：查了几组（票 × 周期）、对不上的那几条、每个周期在盘上覆盖了多久。

    `groups` 与 `findings` 是两个数，分开记是因为它们各自的 0 意思相反：0 条 findings 配上 0 组
    是"今天根本没查"，配上 900 组才是"两本账都对上了"。日报只能有一种写法把它们区分开，而写日报
    的人手里必须同时握着这两个数。

    `covers` 是顺带报出的另一半（07 §5.1 的"可回测区间"）：断档说的是"今天少没少"，它说的是
    "盘上一共攒了多少"。两个数来自同一次读盘，分开跑就会有两个时刻、两份答案。它的值是存储层的
    记录——判据层因此认识了一个存储类型，与 `daily_report` 接 `WriteReport` 是同一条取舍。
    """

    groups: int
    findings: tuple[Finding, ...]
    covers: Mapping[str, Cover | None] = field(default_factory=dict)


def reconcile_day(
    symbol: str,
    day: date,
    dataset: str,
    minute_bars: Sequence[Bar],
    daily: Bar | None,
    *,
    tolerance_pct: float,
) -> tuple[Finding, ...]:
    """一只票、一天、一个周期：合成日K 与日线对一遍，返回对不上的那几条。

    `tolerance_pct` 是百分数（0.5 = 0.5%），与 R004/R007 用的是 `[defaults]` 里同一个数：
    "多少算噪声"全项目只该有一个口径，两处各写一遍迟早漂成两种严格程度。

    两边都没有时给空而不是报"断档"：那只票那天可能根本没上市、可能整池里就没有它——
    "该不该有数据"是股票池与交易日历的事，这一层只回答"两本账对得上吗"。
    """
    if daily is None:
        if not minute_bars:
            return ()
        return (
            Finding(
                "no_daily",
                symbol,
                day,
                dataset,
                f"分钟线有 {len(minute_bars)} 根，日线那天一根都没有：两本账对不上，"
                "先查日线任务那天有没有抓这只票",
            ),
        )
    if not minute_bars:
        # 全天停牌那天真的没有分钟行，而源不给停牌标记——"量=0"是这里唯一可用的判据。
        # 把它报成断档等于每天早上一堆假警报，代价是把真断档淹掉。
        if daily.is_suspended or daily.volume == 0:
            return ()
        return (
            Finding(
                "no_minutes",
                symbol,
                day,
                dataset,
                f"日线有成交（{daily.volume:g} 股），分钟线一行都没有：这一天的尾巴缺了。"
                "明天重跑还补得回来（尾巴整段重落盘），滑出 1970 根的窗口就永久没了",
            ),
        )
    synthetic = daily_from_minutes(minute_bars)
    assert synthetic is not None  # 上面已保证 `minute_bars` 非空，这一行只是把这件事交给类型
    out: list[Finding] = []
    for name, got, want in (
        ("开盘", synthetic.open, daily.open),
        ("收盘", synthetic.close, daily.close),
    ):
        if got != want:
            out.append(
                Finding(
                    "price",
                    symbol,
                    day,
                    dataset,
                    f"{name}不相等：分钟合成 {got:g} vs 日线 {want:g}。两边都是不复权价，"
                    "同一天的这个价不可能两个都对，不等就是要人去看哪一边错",
                )
            )
    if synthetic.high > daily.high:
        out.append(
            Finding(
                "extremes",
                symbol,
                day,
                dataset,
                f"分钟最高价 {synthetic.high:g} 越过日线最高价 {daily.high:g}："
                "日线看不到比它更高的价，说明其中一边不是同一天或有一边错",
            )
        )
    if synthetic.low < daily.low:
        out.append(
            Finding(
                "extremes",
                symbol,
                day,
                dataset,
                f"分钟最低价 {synthetic.low:g} 跌破日线最低价 {daily.low:g}：理由同最高价那一条",
            )
        )
    for name, got, want in (
        ("成交量", synthetic.volume, daily.volume),
        ("成交额", synthetic.amount, daily.amount),
    ):
        gap = _relative(got, want) * 100.0  # 百分数：与 `gate.toml` 的 `tolerance_pct` 同单位
        if gap > tolerance_pct:
            out.append(
                Finding(
                    "volume",
                    symbol,
                    day,
                    dataset,
                    f"{name}相对偏差 {gap:.2f}% 超过容差 {tolerance_pct:.2f}%："
                    f"分钟合成 {got:g} vs 日线 {want:g}（全量实测此类偏差恒为分钟侧偏少，"
                    "最差 -8.64%；是源的几个端点彼此对不上，不是聚合少加了）",
                )
            )
    return tuple(out)


def _relative(got: float, want: float) -> float:
    """相对偏差（比例，0.0864 = 8.64%，全量实测的最大一档）。

    `want` 为 0 时只有 `got` 也为 0 才算 0 偏差。
    """
    if want == 0:
        return 0.0 if got == 0 else float("inf")
    return abs(got - want) / abs(want)


def counts(findings: Sequence[Finding]) -> dict[str, int]:
    """每种坏法几条。五种都列出来（包括 0 条的那几种）：只报非零的会让"今天完全没查"看起来
    像"全部对上了"。
    """
    tally = dict.fromkeys(KINDS, 0)
    for finding in findings:
        tally[finding.kind] += 1
    return tally
