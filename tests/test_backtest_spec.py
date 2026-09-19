"""`config/backtest.toml` 的装载与费用算法（ADR-0010 决定 8；03-4.3 的敏感性半边）。

这里钉的是三件事：文件里的数**就是**那些数（改费率必须在 diff 里留下第二处痕迹）、缺东西就
不装载（代码不兜底默认值），以及 `fee` 的手工核算。`attempt` 用的是测试自己编的凑整费率，
所以真配置的数值只有这一处看着——那正是"两个文件各说一遍，改一处漂一处"要防的形状。
"""

from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from zhixing_quant import config
from zhixing_quant.backtest.spec import BacktestConfigError, Cost, load, parse

GOOD: dict[str, Any] = {
    "cost": {
        "commission_pct": 0.025,
        "commission_min": 5.0,
        "stamp_pct": 0.05,
        "transfer_pct": 0.001,
        "slippage_pct": 0.02,
    },
    "execution": {"delay_bars": 1, "max_participation_pct": 5.0},
    "position": {"lot_size": 100, "base_lots": 3},
}


def test_the_shipped_file_says_the_numbers_the_adr_quotes() -> None:
    """这条测试存在的唯一理由：ADR-0010 决定 8 里写了一组"当期标准"，而配置文件是另一处。

    两边不钉一次，就会出现"文档说万分之 2.5、跑起来是万 3"这种没人负责的漂移。
    """
    loaded = load(config.backtest_config_file())
    assert loaded.cost == Cost(
        commission_pct=0.025,
        commission_min=5.0,
        stamp_pct=0.05,
        transfer_pct=0.001,
        slippage_pct=0.02,
    )
    assert loaded.execution.delay_bars == 1
    assert loaded.execution.max_participation_pct == 5.0


def test_a_missing_file_is_not_the_same_as_zero_cost(tmp_path: Path) -> None:
    with pytest.raises(BacktestConfigError, match="不存在"):
        load(tmp_path / "nope.toml")


@pytest.mark.parametrize("section", ["cost", "execution"])
def test_a_missing_section_names_what_it_was_asking_for(section: str) -> None:
    data = {key: dict(value) for key, value in GOOD.items() if key != section}
    with pytest.raises(BacktestConfigError, match=section):
        parse(data)


def test_a_missing_key_names_the_key_and_offers_no_default() -> None:
    data = {key: dict(value) for key, value in GOOD.items()}
    del data["cost"]["stamp_pct"]
    with pytest.raises(BacktestConfigError, match="stamp_pct"):
        parse(data)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("cost", "commission_pct", -0.01),
        ("cost", "slippage_pct", -1.0),
        ("cost", "commission_min", -5.0),
    ],
)
def test_a_negative_rate_is_a_mistake_not_an_extreme_parameter(
    section: str, key: str, value: float
) -> None:
    data = {name: dict(raw) for name, raw in GOOD.items()}
    data[section][key] = value
    with pytest.raises(BacktestConfigError, match="负"):
        parse(data)


def test_zero_bars_of_delay_would_be_trading_at_the_signal_price() -> None:
    """`delay_bars = 0` 就是拿"信号所在那根"的开盘价成交，而那一根还没走完（07 §5.2 禁止项）。"""
    data = {name: dict(raw) for name, raw in GOOD.items()}
    data["execution"]["delay_bars"] = 0
    with pytest.raises(BacktestConfigError, match="delay_bars"):
        parse(data)


@pytest.mark.parametrize("participation", [0.0, -1.0, 100.5])
def test_a_participation_cap_outside_the_possible_range(participation: float) -> None:
    data = {name: dict(raw) for name, raw in GOOD.items()}
    data["execution"]["max_participation_pct"] = participation
    with pytest.raises(BacktestConfigError, match="参与率"):
        parse(data)


@pytest.mark.parametrize("lot_size", [0, -100])
def test_a_lot_smaller_than_one_share_is_not_a_size(lot_size: int) -> None:
    data = {name: dict(raw) for name, raw in GOOD.items()}
    data["position"]["lot_size"] = lot_size
    with pytest.raises(BacktestConfigError, match="一手至少一股"):
        parse(data)


def test_a_negative_base_position_is_an_empty_account_not_a_short() -> None:
    """底仓填负数不是"做空"：A 股散户没有这条路，写负数只可能是把符号记错了。"""
    data = {name: dict(raw) for name, raw in GOOD.items()}
    data["position"]["base_lots"] = -1
    with pytest.raises(BacktestConfigError, match="底仓不可能是负的"):
        parse(data)


def test_zero_base_lots_is_allowed_because_it_is_a_real_account() -> None:
    """空仓起跑是合法配置，只是测不出"先卖后买"的那一半（`[position]` 的注释说的是这件事）。"""
    data = {name: dict(raw) for name, raw in GOOD.items()}
    data["position"]["base_lots"] = 0
    assert parse(data).sizing.base_shares == 0


def test_the_minimum_commission_binds_on_small_orders_only() -> None:
    """一次 300 股的小单，佣金按费率算只有几分钱——不收最低额就是给做T 免票。"""
    cost = Cost(0.025, 5.0, 0.05, 0.001, 0.02)
    assert cost.fee("buy", 30.0) == pytest.approx(5.0 + 0.0003)  # 最低 5 元 + 过户费
    assert cost.fee("buy", 30000.0) == pytest.approx(7.5 + 0.3)  # 万 2.5 已经过了最低额


def test_the_stamp_tax_is_only_on_the_sell_side() -> None:
    cost = Cost(0.0, 0.0, 0.05, 0.0, 0.0)
    assert cost.fee("buy", 10000.0) == pytest.approx(0.0)
    assert cost.fee("sell", 10000.0) == pytest.approx(5.0)


def _as_tuple(cost: Cost) -> tuple[float, ...]:
    """逐项比：`× 1.5` 是浮点乘法，`0.025×1.5` 与字面量 0.0375 差在最末一位。"""
    return tuple(getattr(cost, f.name) for f in fields(Cost))


def test_sensitivity_scaling_multiplies_every_cost_not_just_the_rates() -> None:
    """03-4.3 的"上浮 50%"要连最低佣金与滑点一起乘：只乘费率，小单那一半假设没动过。"""
    base = Cost(0.025, 5.0, 0.05, 0.001, 0.02)
    # 逐项比而不是比整个 Cost：`× 1.5` 是浮点乘法，0.025×1.5 与字面量 0.0375 差在最末一位。
    assert _as_tuple(base.scaled(1.5)) == pytest.approx((0.0375, 7.5, 0.075, 0.0015, 0.03))
    assert _as_tuple(base.scaled(0.5)) == pytest.approx((0.0125, 2.5, 0.025, 0.0005, 0.01))
    assert base.fee("buy", 1000.0) == pytest.approx(5.01)
    assert base.scaled(1.5).fee("buy", 1000.0) == pytest.approx(7.515)


def test_scaling_cost_leaves_the_clock_alone() -> None:
    """敏感性测试乘的是钱。`delay_bars` 跟着变就不是"成本 × 1.5"，那是另一种实验。"""
    loaded = load(config.backtest_config_file())
    assert loaded.with_cost_scaled(1.5).execution == loaded.execution
    assert loaded.with_cost_scaled(1.5).cost.slippage_pct == pytest.approx(0.03)
