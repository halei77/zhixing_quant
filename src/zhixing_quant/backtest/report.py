"""报告：把一次运行折成一页 Markdown（ADR-0010 决定 10；03-4.5 的留痕要求）。

只产文本，不碰磁盘——登记与落盘在 `backtest/cli.py`，那才是唯一允许 import `storage`/`sources`
的地方。这么分是为了让这一页能被单测逐字断言：报告是用户唯一会读的东西，它说错一句，
前面所有的正确都没人看见。

判定也住在这里：`status()` 把三档成本折成台账那三个码之一。让报告与台账共用同一个 `_shape`，
是为了堵掉一种最难发现的错——页面上写"同号：都是正的"、台账里记 `fail`。结论由数字算出来，
调用方就敲不出一个"通过"来（09 §五）。

三条写法上的硬规矩，都来自"不许把没做读成做了"：

- 算不出来的数印 `—`，并在上面那句话里说清算不出来（0 回合的胜率不是 0.0%）。
- 拒单表**按判序补齐六档**、缺席的印 0。这张表全零是一句有意义的话（一单没拒），
  缺档则是"没查"——所以补零的活在这里干，`Result.rejects_by_reason` 故意不补。
- 成本敏感性只报**期望的符号有没有反转**（03-4.3 问的就是这一句）。反转不判失败、只亮红灯
  并要求人工归因：这是 03 的原话，不是这里可以商量的余地。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from zhixing_quant.backtest.engine import Result, say
from zhixing_quant.backtest.fills import REASONS
from zhixing_quant.backtest.limits import ALLOWANCE_PP
from zhixing_quant.backtest.metrics import Metrics
from zhixing_quant.backtest.position import REASON_T1
from zhixing_quant.backtest.spec import Assumptions

#: 表里"算不出来"的那个格子。空白会被读成"0"或"略过"，破折号至少是个没填的格。
NA = "—"

#: 台账 `backtests.status` 那三个码（`tasks/db.py` 的 CHECK 约束是出处，这里只转述不新造）。
#: 测试把它们喂回 `tasks.models.parse_backtest_status`，撞了就红——防止这里改名而那边没跟。
PASS = "pass"
FAIL = "fail"
VOID = "void"


@dataclass(frozen=True)
class Identity:
    """这一跑是谁。`run_no` 与 `backtest_id` 都来自台账，报告只转述不计算。"""

    strategy: str
    version: str
    params_hash: str
    backtest_id: int
    run_no: int


@dataclass(frozen=True)
class Scope:
    """数据从哪来、覆盖多久。`depth` 是盘上真实深度：报告要报它，而不是让读者猜区间外有没有数。"""

    dataset: str
    start: date
    end: date
    codes: tuple[str, ...]
    bars: int
    depth: str


@dataclass(frozen=True)
class Probe:
    """成本敏感性的一档：乘了多少，跑出来什么成绩。"""

    factor: float
    metrics: Metrics


@dataclass(frozen=True)
class Shape:
    """三档成本的形状：有没有内容、符号一致吗、最贵那一档还赚不赚钱。

    只留判定要的这三问，不留数字本身——`_verdict` 与 `status` 都从同一个 `Shape` 出发，
    于是"报告写同号都是正的、台账记 fail"这种话在这套代码里出不来。
    """

    signs: Mapping[float, bool]

    @property
    def contentless(self) -> bool:
        return not self.signs

    @property
    def flipped(self) -> bool:
        return len(set(self.signs.values())) > 1

    @property
    def dearest_positive(self) -> bool:
        """最贵一档的期望是否为正。07-5.4 第 2 条问的就是这一句。

        三档同号时它同时就是"三档都为正"，所以 `_verdict` 那句"都是正的"也读它——两处
        共用一个判据，才不会一处说通过、另一处记失败。
        """
        return bool(self.signs) and self.signs[max(self.signs)]


@dataclass(frozen=True)
class Report:
    identity: Identity
    scope: Scope
    assumptions: Assumptions
    metrics: Metrics
    result: Result
    output_dir: Path
    sensitivity: tuple[Probe, ...] = ()
    disclosures: tuple[str, ...] = ()


def _pct(value: float | None) -> str:
    return NA if value is None else f"{value:.2%}"


def _num(value: float | None) -> str:
    return NA if value is None else f"{value:.4f}"


def _money(value: float) -> str:
    return f"{value:,.2f}"


def _factor(factor: float) -> str:
    """成本倍数写成一位小数：`:g` 会把 1.0 印成 "1"，那一档就和上面那句 "0.5、1.0、1.5" 对不上。"""
    return f"×{factor:.1f}"


def render(report: Report) -> str:
    """整页 Markdown。段落顺序就是读的顺序：先说这是什么，再说多少钱，最后说哪里不确定。"""
    m = report.metrics
    i = report.identity
    return "\n".join(
        [
            f"# 日内回测报告 · {i.strategy} v{i.version}",
            "",
            f"> **这是 {i.strategy} 的第 {i.run_no} 次回测**（台账编号 #{i.backtest_id}，"
            f"参数哈希 `{i.params_hash}`）。",
            "> 同一份参数每跑一次计数加一：反复调参只报最好那次，在这里会被这个数拆穿（03-4.5）。",
            "",
            *_assumptions_section(report),
            *_score_section(m),
            *_no_trip_section(report, m),
            *_money_section(m),
            *_reject_section(report.result),
            *_open_section(report),
            *_sensitivity_section(report.sensitivity),
            *_disclosure_section(report.disclosures),
            *_artifact_section(report, m),
        ]
    )


def _assumptions_section(report: Report) -> list[str]:
    a = report.assumptions
    s = report.scope
    return [
        "## 一、这一跑用了什么",
        "",
        f"- 数据：`{s.dataset}`，{s.start} → {s.end}，{len(s.codes)} 只票、{s.bars:,} 根K线",
        f"- 盘上深度：{s.depth}",
        f"- 执行：信号之后隔 {a.execution.delay_bars} 根K线成交，单笔不超过该根成交量的 "
        f"{a.execution.max_participation_pct:g}%",
        f"- 成本：佣金 {a.cost.commission_pct:g}%（单笔最低 {a.cost.commission_min:g} 元）"
        f"、印花税 {a.cost.stamp_pct:g}%（卖出单边）、过户费 {a.cost.transfer_pct:g}%、"
        f"滑点 {a.cost.slippage_pct:g}%",
        f"- 仓位：一手 {a.sizing.lot_size} 股 × 期初底仓 {a.sizing.base_lots} 手 = "
        f"{a.sizing.base_shares} 股/票。底仓入账价取该票区间内第一根K线的开盘价——"
        "**那是假设不是事实**（我们不知道当年买在多少，ADR-0010 决定 5）",
        f"- 判板：档位取自 `config/gate.toml` 的 `limits_pct`（与门禁 R004 同一张表），"
        f"另留 {ALLOWANCE_PP:g} 个百分点的取整余量；**不叠加** R004 的 `tolerance_pct`——"
        "那 0.5% 是给测量噪声的，不是一条制度",
        "",
    ]


def _score_section(m: Metrics) -> list[str]:
    return [
        "## 二、成绩",
        "",
        "| 指标 | 值 | 怎么算的（决定 6 写死的分母） |",
        "|---|---|---|",
        f"| 回合数 | {m.trips} | 配上的那几截，跨日也算 |",
        f"| 盈 / 亏 / 平 | {m.wins} / {m.losses} / {m.even} | 按回合盈亏正负 |",
        f"| 胜率 | {_pct(m.win_rate)} | 盈利回合 / 回合数（打平留在分母里） |",
        f"| 盈亏比 | {_num(m.payoff)} | 平均盈利 ÷ 平均亏损的绝对值 |",
        f"| 单笔期望（元） | {_num(m.expectancy)} | 回合盈亏合计 ÷ 回合数，已含两笔费用 |",
        f"| 最大连败 | {m.max_loss_streak} | 按配上的先后、跨票一起数 |",
        f"| T出成交 / T入成交 | {m.t_out_fills} / {m.t_in_fills} | 笔数，不含被拒的委托 |",
        f"| T飞率 | {_pct(m.fly_rate)} | T飞 ÷ T出成交 |",
        f"| 加仓率 | {_pct(m.add_rate)} | 加仓 ÷ T入成交 |",
        "",
    ]


def _no_trip_section(report: Report, m: Metrics) -> list[str]:
    if m.trips:
        return []
    return [
        f"**无回合**：这一跑没有任何一笔买卖配上，所以上面那五个数**一个都不存在**——"
        f"`{_num(None)}` 不是 0，是“没做”。成交 {len(report.result.fills)} 笔、拒单 "
        f"{len(report.result.rejects)} 笔，原因见第四节。",
        "",
    ]


def _money_section(m: Metrics) -> list[str]:
    return [
        "## 三、钱",
        "",
        f"- 账户总盈亏 {_money(m.final_pnl)} 元，拆开是下面三项（精确值相加等于总数；"
        f"每个数各自四舍五入到分，纸上相加最多差二分）：",
        f"  - 回合已实现 {_money(m.trip_pnl)} 元 —— {m.trips} 个回合，做T 本身的成绩",
        f"  - 未还原腿浮盈亏 {_money(m.unrealized)} 元 —— 收盘还没配上的那几笔",
        f"  - 底仓 beta {_money(m.base_pnl)} 元 —— 拿着不动也会有，与手艺无关",
        "",
        "拆开是因为 07 §二 要的答案是“这套信号做T 划不划算”，不是“这只票这个月涨了多少”。"
        "两者混在一个总数里，回测就会把扛仓的运气报成手艺。",
        "",
    ]


def _reject_section(result: Result) -> list[str]:
    tally = result.rejects_by_reason
    rows = [
        f"| `{reason}` | {tally.get(reason, 0)} |"
        for reason in (REASON_T1, *REASONS)  # 这个顺序就是判序：账户先于市场、制度先于流动性
    ]
    return [
        "## 四、没做成的单子",
        "",
        "| 原因 | 笔数 |",
        "|---|---|",
        *rows,
        f"| 合计 | {len(result.rejects)} |",
        "",
        "六档全列、没有的印 0：这张表全零是一句有内容的话（一单没拒），缺档却等于“这一类没查过”。"
        "`t_plus_1` 归账户（今天没额度），其余五条归市场（那根K线不给成交）。",
        "",
    ]


def _open_section(report: Report) -> list[str]:
    closings = report.result.closings
    if not closings:
        return ["## 五、还开着的仓位", "", "这一跑没有一只票进过账，也就没有未平仓。", ""]
    lot = report.assumptions.sizing.lot_size
    rows = [
        f"| {c.code} | {c.held:,} | {c.open_shares // lot} 手 | {_money(c.unrealized)} |"
        for c in closings
    ]
    return [
        "## 五、还开着的仓位",
        "",
        "| 票 | 期末持仓（股） | 未还原 | 浮盈亏（元） |",
        "|---|---|---|---|",
        *rows,
        "",
        "未平仓的部分**不在胜率的分母里**（决定 6）：胜率只算配完的回合，浮盈亏按这一行如实挂着。",
        "",
    ]


def _sensitivity_section(probes: tuple[Probe, ...]) -> list[str]:
    if not probes:
        return []
    rows = [
        f"| {_factor(p.factor)} | {_num(p.metrics.expectancy)} | {p.metrics.trips} | "
        f"{_pct(p.metrics.win_rate)} |"
        for p in probes
    ]
    return [
        "## 六、成本敏感性（03-4.3）",
        "",
        "同一份假设，把 `[cost]` 五项一起乘 {0.5, 1.0, 1.5} 各跑一遍：",
        "",
        "| 成本倍数 | 单笔期望（元） | 回合数 | 胜率 |",
        "|---|---|---|---|",
        *rows,
        "",
        f"判定：{_verdict(probes)}",
        "",
    ]


def _verdict(probes: tuple[Probe, ...]) -> str:
    """只问一件事：成本上下浮动 50% 之后，正期望会不会翻成负期望（或反过来）。"""
    shape = _shape(probes)
    if shape.contentless:
        return (
            "**无从判断**：每一档都没有回合，“正期望会不会反转”在这里没有期望可问。"
            "这不是“通过”，是没有内容可判——策略一单没做成，或者票池与区间不对。"
        )
    every = "、".join(_factor(factor) for factor in sorted(shape.signs))
    if not shape.flipped:
        # 说"都不为正"而不是"都是负的"：期望恰好为 0（两笔回合互相抵平）不是负，
        # 而这一句要把零那一档也算进去——它对启用门槛同样是"不通过"。
        polarity = "都是正的" if shape.dearest_positive else "都不为正"
        return (
            f"同号：{every} 这几档下期望{polarity}。结论不来自成本假设的乐观误差"
            "——03-4.3 要的就是这一句（期望为负也照样成立：那是老实的负，不是假的正）。"
        )
    detail = "、".join(
        f"{_factor(factor)}→{'正' if good else '负'}"
        for factor, good in sorted(shape.signs.items())
    )
    return (
        f"**反转**：{detail}。利润来自成本假设而不是信号，亮红灯，人工归因之前不谈启用（03-4.3）。"
    )


def _shape(probes: tuple[Probe, ...]) -> Shape:
    """三档 → 每档期望的符号。没有回合的档不参与：那里没有符号可问。"""
    return Shape(
        signs={
            probe.factor: probe.metrics.expectancy > 0
            for probe in probes
            if probe.metrics.expectancy is not None
        }
    )


def status(probes: tuple[Probe, ...]) -> str:
    """把三档成本折成台账 `backtests.status` 那三个码之一（09 §五）。

    为什么是算出来的而不是给个 `--status` 开关：状态是**结论**，一个能敲出来的结论就是一笔
    能敲出来的假留痕。三档全无数据时它判不了盈亏，也就没资格说"通过"。
    """
    shape = _shape(probes)
    if shape.contentless:
        return VOID  # 什么都没测出来：票池或区间不对，这一跑不成立（不是失败，是作废）
    if shape.flipped:
        return FAIL  # 03-4.3：利润来自成本假设
    if not shape.dearest_positive:
        return FAIL  # 07-5.4 第 2 条：最贵那一档也要为正，否则这套信号不赚钱
    return PASS


def _disclosure_section(lines: tuple[str, ...]) -> list[str]:
    if not lines:
        return []
    return ["## 七、这一页不能保证的事", "", *[f"- {line}" for line in lines], ""]


def _artifact_section(report: Report, m: Metrics) -> list[str]:
    return [
        "## 产物",
        "",
        f"- 本报告：`{report.output_dir}/report.md`",
        f"- 净值曲线：`{report.output_dir}/equity.csv`（{len(report.result.equity):,} 行）",
        "",
        "净值按票逐个打点、不跨票合并：合并要求所有票在同一时刻都有行，停牌与断档恰恰不让它成立"
        "（ADR-0010 决定 2）。账户层面的净值就是上面第三节那个总盈亏。",
        f"- 期末总盈亏 {_money(m.final_pnl)} 元、回合 {m.trips} 个",
        "",
    ]


def equity_csv(result: Result) -> str:
    """净值曲线：一行一个 `(时刻, 票)`，字段与 `EquityPoint` 逐一对应。"""
    rows = [
        ",".join(
            (
                day,
                clock,
                point.code,
                f"{point.cash:.10g}",
                str(point.held),
                f"{point.market_value:.10g}",
                f"{point.pnl:.10g}",
            )
        )
        for point in result.equity
        for day, _, clock in [say(point.stamp).partition(" ")]
    ]
    return "\n".join(["day,time,code,cash,held,market_value,pnl", *rows]) + "\n"
