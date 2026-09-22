"""日内做T 候选策略（07 §四，Step 7）。

共用的一套姿态先说清，几个策略都是它的变体：

- **状态从持仓推出来，不自己记**。`view.holding − 底仓`（本次运行首根看到的持仓就是底仓）
  在 ±1 手内完全编码了"手里有没有开着一回合、开的哪一腿"——而委托被拒（涨跌停、T+1）时
  holding 不动，从 holding 推状态就**自愈**；自己记一个状态机，拒单之后它就开始说谎。
  底仓记在**实例**身上：B1 之后每次运行都是全新实例，实例字典不跨运行泄漏（模块级字典
  会——那是把上一档的底仓喂给下一档，审计 B1 的镜像错误）。
- **一回合未平不开下一回合**（|delta| ≥ 1 时只做平仓）：堆仓不是策略，是失控。
- **每根最多一笔、成交在下一根**（ADR-0010）：这里只回答"此刻做不做"，不做任何成交假设。
- **确定性**：同一次运行内逐位可复现（03-4.1）；B0 的随机用固定种子 + 逐根推进的 PRNG。

C1（残差均值回归）不在此处：它需要跨票参考序列，引擎的 View 没有这个注入点（07 §四
的缺席说明）。先落地的三个候选 + 随机对照已能开跑整轮竞争。
"""

from __future__ import annotations

import random
from datetime import date

from zhixing_quant.backtest.engine import Signal, View
from zhixing_quant.domain.bar import Bar


def _today(view: View) -> list[Bar]:
    """今天的根。它们在 `view.bars` 的尾部——历史序列跨日，倒着扫到日期变了就停。

    日内回看量级（≤240 根）与全史（几千根）差一个数量级，这个扫法是 O(当日)；
    按日全量过滤是 O(全史)，每根都来一遍就是 O(n²)。
    """
    day: date = view.bar.trade_date
    out = []
    for bar in reversed(view.bars):
        if bar.trade_date != day:
            break
        out.append(bar)
    out.reverse()
    return out


def _zscore(series: list[float]) -> float | None:
    """z 分数。样本 < 2 或 std 为 0（比如一根价的死水）都问不出 z → None，不编一个数。"""
    n = len(series)
    if n < 2:
        return None
    mean = sum(series) / n
    var = sum((x - mean) ** 2 for x in series) / n
    if var <= 0.0:
        return None
    std: float = var**0.5  # float.__pow__ 在 typeshed 里返回 Any，钉住
    return (series[-1] - mean) / std


class _RoundMixin:
    """底仓追踪 + 回合姿态。状态只落在实例字典里（见模块说明第一条）。"""

    def __init__(self) -> None:
        self._bases: dict[str, int] = {}

    def _delta(self, view: View) -> int:
        """持仓相对底仓的手数：0 平、+1 多回合开着、−1 空回合开着。"""
        base = self._bases.setdefault(view.code, view.holding)
        return (view.holding - base) // view.lot_size

    def _act(
        self,
        view: View,
        *,
        enter_sell: bool,
        enter_buy: bool,
        close_short: bool,
        close_long: bool,
        note: str,
    ) -> Signal | None:
        """四条分支各管一种持仓形状。卖出要过可卖额度，买入不用——这是引擎同一条判据的
        预演，策略自己先挡一道，省一单注定被拒的委托。
        """
        delta = self._delta(view)
        if delta == 0:
            if enter_sell and view.lots_sellable >= 1:
                return Signal(side="sell", lots=1, note=f"{note} enter-short")
            if enter_buy:
                return Signal(side="buy", lots=1, note=f"{note} enter-long")
            return None
        if delta == -1 and close_short:
            return Signal(side="buy", lots=1, note=f"{note} exit")
        if delta == 1 and close_long and view.lots_sellable >= 1:
            return Signal(side="sell", lots=1, note=f"{note} exit")
        return None


class VWAPReversion(_RoundMixin):
    """C2：相对当日累计 VWAP 的偏离 z 分数反转（07 §四）。

    偏离 = `close/vwap − 1`，vwap 用累计**金额/累计量**（amount 是元、volume 是股，商就是
    当日真实成交均价），窗口内均值归一后再标准化——早盘偏离天然大，不归一会把开盘半小时
    当成常态极值。默认值取网格中位（`zx-backtest --strategy` 零参构造用的就是它们）。

    止盈与止损共用同一对边界：偏离**缩回** z_exit 内是 thesis 兑现，**冲出** z_enter 之外
    是 thesis 破产——两种都平仓，拖着不处理只会把可控的一笔小亏变成穿仓。
    """

    name = "vwap_reversion"

    def __init__(self, lookback: int = 20, z_enter: float = 2.0, z_exit: float = 0.5) -> None:
        super().__init__()
        if lookback < 2:
            raise ValueError(f"lookback = {lookback}：两根以下算不出 z 分数")
        if z_enter <= z_exit or z_exit < 0:
            raise ValueError(
                f"z_enter = {z_enter} 必须大于 z_exit = {z_exit}，且后者非负——"
                "进场边界缩到出场边界里面，这策略一开仓就平仓"
            )
        self.lookback = lookback
        self.z_enter = z_enter
        self.z_exit = z_exit

    def signals(self, view: View, /) -> Signal | None:
        today = _today(view)
        if len(today) < self.lookback:
            return None
        devs: list[float] = []
        cum_amount = cum_volume = 0.0
        for bar in today:
            if bar.volume <= 0:
                continue
            cum_amount += bar.amount
            cum_volume += bar.volume
            devs.append(bar.close / (cum_amount / cum_volume) - 1.0)
        z = _zscore(devs[-self.lookback :])
        if z is None:
            return None
        up = z >= self.z_enter
        down = z <= -self.z_enter
        return self._act(
            view,
            enter_sell=up,
            enter_buy=down,
            # 多回合（低位买的）：z 缩回 -z_exit 上方 = 止盈；跌破 -z_enter = 止损。
            close_long=down or z >= -self.z_exit,
            # 空回合（高位卖的）：z 缩回 +z_exit 下方 = 止盈；冲破 +z_enter = 止损。
            close_short=up or z <= self.z_exit,
            note=f"z={z:.2f}",
        )


class RangeBreakout(_RoundMixin):
    """C3：开盘 `n_break` 根成区间，突破顺势、跌回边界即离场（07 §四）。

    确认 = 最近 `confirm` 根收盘**全数**站在线外（盘中的影线刺一下不算）；离场看最后一根
    收盘落回区间内——趋势策略的止盈就是止损那一条线，利润回吐是它的既定成本。
    """

    name = "range_breakout"

    def __init__(self, n_break: int = 10, confirm: int = 1) -> None:
        super().__init__()
        if n_break < 2:
            raise ValueError(f"n_break = {n_break}：两根以下不成区间")
        if confirm < 1:
            raise ValueError(f"confirm = {confirm}：确认至少一根")
        self.n_break = n_break
        self.confirm = confirm

    def signals(self, view: View, /) -> Signal | None:
        today = _today(view)
        if len(today) < self.n_break + self.confirm:
            return None
        window = today[: self.n_break]
        high = max(bar.high for bar in window)
        low = min(bar.low for bar in window)
        tail = today[self.n_break :]
        enter_sell = all(bar.close < low for bar in tail[-self.confirm :])
        enter_buy = all(bar.close > high for bar in tail[-self.confirm :])
        last = today[-1].close
        return self._act(
            view,
            enter_sell=enter_sell,
            enter_buy=enter_buy,
            close_long=last < low,
            close_short=last > high,
            note=f"range=[{low:.2f},{high:.2f}]",
        )


class VolumeSpike(_RoundMixin):
    """C4：量比突增 + 价格同向确认，顺势一笔，`exit_bars` 根内时间止损（07 §四）。

    突增判据 = 最近 `confirm` 根每根量 ≥ `k` × 其前 `confirm_window` 根均量（今日内、
    零量根剔除）；方向看确认段最后一根收对确认段前一根收。离场是**纯时间止损**——
    "回到 spike 前价"那种目标价要再引一个参数，网格膨胀，07 §四 定稿时就没给它。
    时间到但持仓已回底仓（上一笔委托被拒、或早被对手分支平掉）→ delta 判据天然沉默，
    不会对着底仓发裸卖。
    """

    name = "volume_spike"

    def __init__(
        self, k: float = 3.0, confirm: int = 1, confirm_window: int = 20, exit_bars: int = 12
    ) -> None:
        super().__init__()
        if k <= 0:
            raise ValueError(f"k = {k}：量比阈值必须为正")
        if confirm < 1 or confirm_window < 1:
            raise ValueError(f"confirm = {confirm} / confirm_window = {confirm_window}：至少一根")
        if exit_bars < 1:
            raise ValueError(f"exit_bars = {exit_bars}：时间止损至少一根")
        self.k = k
        self.confirm = confirm
        self.confirm_window = confirm_window
        self.exit_bars = exit_bars
        self._opened_at: dict[str, int] = {}

    def signals(self, view: View, /) -> Signal | None:
        today = _today(view)
        if len(today) < self.confirm_window + self.confirm + 1:
            return None
        if self._delta(view) != 0:
            opened = self._opened_at.get(view.code)
            if opened is None or view.index - opened < self.exit_bars:
                return None
            return self._act(
                view,
                enter_sell=False,
                enter_buy=False,
                close_short=True,
                close_long=True,
                note="time-stop",
            )
        tail = today[-self.confirm :]
        for i, bar in enumerate(tail):
            head = today[: len(today) - len(tail) + i]
            prev = [b for b in head[-self.confirm_window :] if b.volume > 0]
            if bar.volume <= 0 or not prev:
                return None
            if bar.volume < self.k * sum(b.volume for b in prev) / len(prev):
                return None
        last, before = today[-1], today[-1 - self.confirm]
        up = last.close > before.close
        self._opened_at[view.code] = view.index
        return self._act(
            view,
            enter_sell=not up,
            enter_buy=up,
            close_short=False,
            close_long=False,
            note=f"spike k={self.k}",
        )


class RandomBaseline:
    """B0：与候选同频触发的随机对照（07 §四）。

    固定种子 + 逐根推进的 PRNG：引擎按 (时刻, 代码) 定序问策略，同一份 bars 的问询序列
    唯一，于是逐位可复现（03-4.1）。它的用法不是"调好它"而是"它必须输"——同频随机扣完
    成后期望为负，入选者赢它才算赢（07 §四 B0 行）。
    """

    name = "random_baseline"

    def __init__(self, every_n: int = 24, seed: int = 20260922) -> None:
        if every_n < 1:
            raise ValueError(f"every_n = {every_n}：触发频率至少每根一问")
        self.every_n = every_n
        self._rng = random.Random(seed)
        self._bases: dict[str, int] = {}

    def signals(self, view: View, /) -> Signal | None:
        if view.index % self.every_n != 0:
            return None
        base = self._bases.setdefault(view.code, view.holding)
        delta = (view.holding - base) // view.lot_size
        if delta == 0:
            if self._rng.random() < 0.5:
                if view.lots_sellable < 1:
                    return None
                return Signal(side="sell", lots=1, note="random enter")
            return Signal(side="buy", lots=1, note="random enter")
        if self._rng.random() < 0.5:
            return None
        if delta > 0:
            if view.lots_sellable < 1:
                return None
            return Signal(side="sell", lots=1, note="random exit")
        return Signal(side="buy", lots=1, note="random exit")


#: 竞争名册（07 §四）：C1 缺席，理由与入场条件写在 07，不在这里重复。
CANDIDATES: tuple[type, ...] = (VWAPReversion, RangeBreakout, VolumeSpike)
