"""回测的成本与执行假设：`config/backtest.toml` 是唯一真源，代码不兜底（ADR-0010 决定 8）。

为什么没有默认值：一套"看起来像"的费率能跑出一份正期望的回测，而读者看不出那份收益里有几分
钱来自假设。缺文件、缺键、数字不成形状，全部在装载这一步响——与 `quality/gate_config.py` 同一条
fail-closed，同一个理由（带着坏配置跑完比跑不完贵得多）。

单位一律是**百分点**（0.025 = 万分之 2.5），与 `gate.toml` 的 `limits_pct` 同一口径：两种单位
混在一个项目里，"佣金填成 0.025 还是 2.5"这个问题迟早有人答错一次。

`Cost` 只管钱，`Execution` 只管时间——分成两个类是因为 03-4.3 的敏感性测试乘的是前者，
把 delay_bars 也乘 1.5 不是"成本上浮"，那是另一种实验。
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

#: 交易方向。整条回测链路上只有这一个词，所以它住在最下游依赖的地方，不住在引擎里。
Side = Literal["buy", "sell"]

#: `[cost]` 与 `[execution]` 各自必须有的键。少一个就是装载失败，不猜、不补零。
REQUIRED_COST = (
    "commission_pct",
    "commission_min",
    "stamp_pct",
    "transfer_pct",
    "slippage_pct",
)
REQUIRED_EXECUTION = ("delay_bars", "max_participation_pct")
#: 底仓与一手股数。放在配置而不是写死在引擎里：`base_lots = 0` 是一次合法的实验设置
#: （空仓起跑，只能测先买后卖的那一半），把它编译进代码就没有那个对照组了。
REQUIRED_POSITION = ("lot_size", "base_lots")


class BacktestConfigError(ValueError):
    """成本或执行假设不成形状：装载即失败，不许带着一份可疑费率出结论。"""


@dataclass(frozen=True)
class Cost:
    """一笔成交要付出的钱（滑点除外——它写在价上，不写在费上，见 ADR-0010 决定 3）。"""

    commission_pct: float
    commission_min: float
    stamp_pct: float
    transfer_pct: float
    slippage_pct: float

    def fee(self, side: Side, notional: float) -> float:
        """按成交额算的费用：佣金双边（不足最低额按最低额收）、印花税只收卖出、过户费双边。

        最低佣金这一条不是装饰：做T 一次 300 股、几块钱的股票，佣金按费率算只有几分钱，
        免掉最低额等于给小单开了免票，而 07 §二 说的测试单位恰恰全是小单。
        """
        commission = max(notional * self.commission_pct / 100.0, self.commission_min)
        transfer = notional * self.transfer_pct / 100.0
        stamp = notional * self.stamp_pct / 100.0 if side == "sell" else 0.0
        return commission + transfer + stamp

    def scaled(self, factor: float) -> Cost:
        """成本整体上浮/下调，03-4.3 的 ±50% 就是它。

        最低佣金与滑点一起乘：只乘费率的话，"成本 × 1.5"测出来的是一个小部分的变化，
        而小单的佣金由最低额决定——那正是做T 的主战场。
        """
        return Cost(
            commission_pct=self.commission_pct * factor,
            commission_min=self.commission_min * factor,
            stamp_pct=self.stamp_pct * factor,
            transfer_pct=self.transfer_pct * factor,
            slippage_pct=self.slippage_pct * factor,
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Cost:
        return cls(
            commission_pct=float(raw["commission_pct"]),
            commission_min=float(raw["commission_min"]),
            stamp_pct=float(raw["stamp_pct"]),
            transfer_pct=float(raw["transfer_pct"]),
            slippage_pct=float(raw["slippage_pct"]),
        )


@dataclass(frozen=True)
class Execution:
    """成交在时间上的形状：隔几根K线、一笔最多吃掉多少成交量。"""

    delay_bars: int
    max_participation_pct: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Execution:
        return cls(
            delay_bars=int(raw["delay_bars"]),
            max_participation_pct=float(raw["max_participation_pct"]),
        )


@dataclass(frozen=True)
class Sizing:
    """一手多少股、期初底仓几手（07 §二）。台账用它把"几手"换成"几股"。"""

    lot_size: int
    base_lots: int

    @property
    def base_shares(self) -> int:
        return self.lot_size * self.base_lots

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Sizing:
        return cls(lot_size=int(raw["lot_size"]), base_lots=int(raw["base_lots"]))


@dataclass(frozen=True)
class Assumptions:
    """一次回测的全部外部假设。报告要把它整个印出来：结论依赖哪个假设，得一眼看得见。"""

    cost: Cost
    execution: Execution
    sizing: Sizing

    def with_cost_scaled(self, factor: float) -> Assumptions:
        """03-4.3 的成本敏感性：**只**乘成本。

        `execution` 与 `sizing` 原样带过去是有意的——把 delay_bars 也乘 1.5 测出来的是另一种
        实验（延迟敏感性），混在一行里读就成了"成本翻倍的策略依然赚钱"，而真正翻倍的是延迟。
        """
        return Assumptions(
            cost=self.cost.scaled(factor), execution=self.execution, sizing=self.sizing
        )


def _section(data: Mapping[str, Any], name: str, required: tuple[str, ...]) -> Mapping[str, Any]:
    raw = data.get(name)
    if not isinstance(raw, Mapping):
        raise BacktestConfigError(f"缺 [{name}] 一节：{'/'.join(required)} 都要有人回答")
    missing = [key for key in required if key not in raw]
    if missing:
        raise BacktestConfigError(f"[{name}] 缺 {','.join(missing)}：这里没有默认值可补")
    return raw


def _check(cost: Cost, execution: Execution, sizing: Sizing) -> None:
    """形状检查：负费率与"隔 0 根"都不是"极端参数"，是写错了。"""
    if min(cost.commission_pct, cost.stamp_pct, cost.transfer_pct, cost.slippage_pct) < 0:
        raise BacktestConfigError("费率出现负数：成本为负的回测是在算赚钱的机器")
    if cost.commission_min < 0:
        raise BacktestConfigError("commission_min 为负：最低额不可能是退钱")
    if execution.delay_bars < 1:
        raise BacktestConfigError(
            f"delay_bars = {execution.delay_bars}：隔 0 根就是拿信号所在那根的开盘价成交，"
            "而那一根还没走完（ADR-0010 决定 3）。人工看到信号再下单不可能比信号更早"
        )
    if not 0.0 < execution.max_participation_pct <= 100.0:
        raise BacktestConfigError(
            f"max_participation_pct = {execution.max_participation_pct}：参与率必须在 (0, 100]"
        )
    if sizing.lot_size <= 0:
        raise BacktestConfigError(f"lot_size = {sizing.lot_size}：一手至少一股")
    if sizing.base_lots < 0:
        raise BacktestConfigError(f"base_lots = {sizing.base_lots}：底仓不可能是负的")


def parse(data: Mapping[str, Any]) -> Assumptions:
    cost = Cost.from_mapping(_section(data, "cost", REQUIRED_COST))
    execution = Execution.from_mapping(_section(data, "execution", REQUIRED_EXECUTION))
    sizing = Sizing.from_mapping(_section(data, "position", REQUIRED_POSITION))
    _check(cost, execution, sizing)
    return Assumptions(cost=cost, execution=execution, sizing=sizing)


def load(path: Path) -> Assumptions:
    """从 TOML 装载。文件不存在直接抛——"没有成本假设"不等于"成本为零"。"""
    if not path.is_file():
        raise BacktestConfigError(
            f"回测假设表不存在：{path}。缺它就跑，等于用一份没人认领的费率出结论"
        )
    with path.open("rb") as fh:
        return parse(tomllib.load(fh))
