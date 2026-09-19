"""分钟K线 → 合成日K（01 Step 4 验收 2；ADR-0009 决定 6 的实测判据）。

只做"一天一根"的装配，不做判定：判据（价必须相等、量额留容差）住在 `quality.reconcile`，
合成住在这里——做T 的回测迟早也要同一张日K表，两处各写一份迟早长成两种"这一天的开盘价"。

`ts` 是收盘时刻（右端点，决定 2），所以"开盘那根"是 `ts` 最小的那根而不是 `ts` 最早的减一格：
09:35 那根覆盖 09:30–09:35，它的 `open` 就是当天的开盘价。这条在源侧实测过（三种周期的标签
都是右端点），不是这里的假设。
"""

from collections.abc import Sequence
from datetime import datetime

from zhixing_quant.domain.bar import Bar


def daily_from_minutes(bars: Sequence[Bar]) -> Bar | None:
    """同一只票、同一天、同一周期的一批分钟K线 → 一根日K。空批给 None。

    None 与抛错分得很清：**"这天一根都没有"** 是对账要的那句话（覆盖率的一半就问这个），
    而"有一根不知道自己属于几点"是数据坏了，两者在日报上不是同一条结论。

    周期不混：调用方按 dataset 分组交进来，这里再兜一道——5min 与 30min 混成一批时，成交量
    会被同一笔真实成交数两遍，而合成出来的 OHLC 看起来完全正常。那种错比抛错贵得多。
    """
    if not bars:
        return None
    days = {(b.symbol, b.trade_date) for b in bars}
    if len(days) > 1:
        earliest, other = sorted(days)[:2]
        raise ValueError(
            f"一次只合成一只票的一天，这批给了 {len(days)} 组（如 {earliest} 与 {other}）："
            "跨票跨天合成出来的那根日K 谁都不是"
        )
    sources = {b.source for b in bars}
    if len(sources) > 1:
        raise ValueError(
            f"{bars[0].symbol}@{bars[0].trade_date} 把 {sorted(sources)} 混成一批来合成："
            "两种周期数的是同一笔成交，量额会翻倍，而 OHLC 看不出任何异常"
        )
    timed: list[tuple[datetime, Bar]] = []
    for bar in bars:
        if bar.ts is None:
            raise ValueError(
                f"{bar.symbol}@{bar.trade_date} 有一根分钟K线没有收盘时刻："
                "合成日K 要的是「最早那根的开盘」与「最晚那根的收盘」，不替它猜一个时刻"
            )
        timed.append((bar.ts, bar))
    timed.sort(key=lambda item: item[0])
    ordered = [bar for _, bar in timed]
    first, last = ordered[0], ordered[-1]
    return Bar(
        source=first.source,
        symbol=first.symbol,
        trade_date=first.trade_date,
        open=first.open,
        high=max(b.high for b in ordered),
        low=min(b.low for b in ordered),
        close=last.close,
        volume=sum(b.volume for b in ordered),
        amount=sum(b.amount for b in ordered),
        adj_factor=None,  # 分钟线不带因子（决定 4）：合成的这根也不要有
        is_suspended=False,
    )
