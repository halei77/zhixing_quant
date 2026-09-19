"""绩效指标：把 `Result` 那几堆事实折成 07 §七 验收 5 要的那五个数（ADR-0010 决定 6）。

纯函数——不读盘、不写盘、不 import `storage`/`sources`。每个数的分母都在决定 6 里写死了，
这一层不许改口径，只做折算。

三条与"别把自己骗了"有关的设计：

- **分母为 0 给 `None`，不给 0.0**。回合数为 0 时印 0.0% 的胜率，读的人以为测过了；`None`
  逼报告写一句"无回合"。这跟 04 §四 那条老规矩是同一件事：没做和做完不能长一样。
- **盈亏只算在回合上**（决定 6 + 决定 5 的底仓不进配对）。底仓那几天的涨跌是持有这只票的
  beta，不是做T 的成绩，所以它进 `final_pnl`、不进胜率分子。两个数分开报，读者才看得出
  "这套信号赚的到底是手的钱还是扛的钱"。
- **打平的回合既不算赢也不算输，但留在胜率的分母里**。把它并进"赢"是送分，并进"输"是冤枉，
  而把它从分母剔掉会让胜率凭空变高——三件事里只有第三种是偷偷改口径。
"""

from __future__ import annotations

from dataclasses import dataclass

from zhixing_quant.backtest.engine import Result
from zhixing_quant.backtest.position import RoundTrip


@dataclass(frozen=True)
class Metrics:
    """一次回测的成绩单。`None` 一律读作"这个数算不出来"，不是"算出来是零"。"""

    trips: int
    wins: int
    losses: int
    even: int
    win_sum: float
    loss_sum: float  # ≤ 0：亏掉的金额按负数存，`final_pnl` 与它对得上才叫核算过
    trip_pnl: float
    t_out_fills: int
    t_in_fills: int
    flew: int
    added: int
    max_loss_streak: int
    #: 账户总盈亏 = 下面三个数之和（`test_the_money_decomposes_exactly` 钉的就是这条恒等式）：
    #: 回合已实现的部分、还没还原的那些腿的浮盈亏、底仓自身的 beta。
    final_pnl: float
    unrealized: float
    base_pnl: float

    @property
    def win_rate(self) -> float | None:
        """胜率 = 盈利回合 / 回合数（决定 6 写死的分母：含打平的那些）。"""
        return _ratio(self.wins, self.trips)

    @property
    def payoff(self) -> float | None:
        """盈亏比 = 平均一笔盈利 / 平均一笔亏损的绝对值。

        取"平均"而不是"总额之比"：总额之比会把胜率偷渡进盈亏比里，一个数回答两件事，
        读的人分不清自己是赢在手熟还是赢在赔率。没有亏损回合时给 `None` 而不是无穷大——
        一个回测里"零亏损"通常是样本太少，不是圣杯。
        """
        if not self.wins or not self.losses:
            return None
        return (self.win_sum / self.wins) / abs(self.loss_sum / self.losses)

    @property
    def expectancy(self) -> float | None:
        """单笔期望 = 回合盈亏合计 / 回合数。已含两笔费用，所以它是"扣完成本划不划算"。"""
        return _ratio(self.trip_pnl, self.trips)

    @property
    def fly_rate(self) -> float | None:
        """T飞率 = T飞 笔数 / T出成交笔数（决定 6）。分母是**成交**的卖出，不含被拒的委托。"""
        return _ratio(self.flew, self.t_out_fills)

    @property
    def add_rate(self) -> float | None:
        """加仓率 = 加仓 笔数 / T入成交笔数。07 §二 把"不得已加仓"写成正常成本，那就得报数。"""
        return _ratio(self.added, self.t_in_fills)


def _ratio(numerator: int | float, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def measure(result: Result) -> Metrics:
    """从一次运行的全部事实折出成绩单。这里没有一个数是重新算出来的——价与费都在 `Result` 里。"""
    trips = result.round_trips
    wins = tuple(t for t in trips if t.pnl > 0)
    losses = tuple(t for t in trips if t.pnl < 0)
    return Metrics(
        trips=len(trips),
        wins=len(wins),
        losses=len(losses),
        even=len(trips) - len(wins) - len(losses),
        win_sum=sum(t.pnl for t in wins),
        loss_sum=sum(t.pnl for t in losses),
        trip_pnl=sum(t.pnl for t in trips),
        t_out_fills=sum(1 for f in result.fills if f.order.side == "sell"),
        t_in_fills=sum(1 for f in result.fills if f.order.side == "buy"),
        flew=sum(e.flew for e in result.day_events),
        added=sum(e.added for e in result.day_events),
        max_loss_streak=_longest_loss_run(trips),
        final_pnl=sum(c.cash + c.held * c.last_price - c.base_cost for c in result.closings),
        unrealized=sum(c.unrealized for c in result.closings),
        base_pnl=sum(c.base_pnl for c in result.closings),
    )


def _longest_loss_run(trips: tuple[RoundTrip, ...]) -> int:
    """最长连败。顺序是**配上的先后**（一个回合在它第二腿成交那天才算完成），不是买入日。

    连败是账户体验的量尺，所以跨票一起数：两只票各连亏三笔，用户感受到的是一串六笔。
    打平的那笔把链子剪断——它不是亏损。
    """
    run = longest = 0
    for trip in trips:
        run = run + 1 if trip.pnl < 0 else 0
        longest = max(longest, run)
    return longest
