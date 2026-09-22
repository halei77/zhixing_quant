"""`zx-compete`：做T 策略竞争的命令行入口（07 §四协议）。

compete 包自己的组合根：读盘（复用 `backtest.cli` 的公开读盘件——同一把尺，别再抄一份）、
跑 walk-forward、判 5.4 门槛、落报告。引擎调用与聚合的纯一半在 `runner.py`。

退出码两档，与"没判成"和"没开始"的老划分同一条：0 = 竞争跑完了，**哪怕结论是全部不启用**
——"不如不用"是 07 §一 的合法结论，不是失败；2 = 没开始（参数错、盘上没数据、区间颠倒）。

台账注记（09 §五）：竞争内部的批量运行是**选择过程**，报告归档不进台账；入围参数的正式
成绩单用 `zx-backtest --strategy ... --version compete-<日期>` 跑一遍，拿 backtest_id——
03-4.5 的"任何测好了的汇报必须附台账编号"由那一跑兑现。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from zhixing_quant import config
from zhixing_quant.backtest.cli import (
    check_dataset,
    check_range,
    describe_depth,
    previous_closes,
    read_minute,
)
from zhixing_quant.backtest.engine import BandLookup
from zhixing_quant.backtest.spec import Assumptions
from zhixing_quant.backtest.spec import load as load_spec
from zhixing_quant.compete import folds as fold_mod
from zhixing_quant.compete import runner, stats
from zhixing_quant.compete.grid import Slot, slots
from zhixing_quant.compete.runner import Score
from zhixing_quant.domain.bar import Bar


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zx-compete",
        description="做T 候选策略竞争（07 §四）：网格 × walk-forward × 测试段一次性评判",
    )
    parser.add_argument(
        "--codes", required=True, help="逗号分隔的票池（07 §二：用户真实持仓，≤10 只）"
    )
    parser.add_argument("--dataset", default="minute_60", help="分钟 dataset，默认 minute_60")
    parser.add_argument(
        "--start", default=None, metavar="YYYY-MM-DD", help="区间起点，默认盘上最早"
    )
    parser.add_argument("--end", default=None, metavar="YYYY-MM-DD", help="区间终点，默认盘上最晚")
    parser.add_argument("--folds", type=int, default=4, help="walk-forward 验证折数，默认 4")
    parser.add_argument(
        "--out", type=Path, default=None, help="报告目录，默认 <数据根>/reports/compete/<今天>"
    )
    return parser


def _day_arg(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"要的是 YYYY-MM-DD，收到的是 {text!r}") from exc


def _band_lookup(end: date, prev: Mapping[str, Mapping[date, float]]) -> BandLookup:
    """板查表装配——与 `backtest.cli._bands` 同源的公开件拼法，口径一处不改两处写。"""
    from zhixing_quant.backtest.cli import build_bands
    from zhixing_quant.quality import gate_config
    from zhixing_quant.sources.akshare import master as master_source
    from zhixing_quant.sources.akshare.calendar import load_calendar

    master = master_source.read_master().to_master()
    calendar = load_calendar(until=end)
    params = gate_config.load(config.gate_config_file()).rule("R004").params
    return build_bands(
        master,
        calendar,
        limits_pct=params["limits_pct"],
        no_limit_days=params["new_listing_no_limit_days"],
        prev_closes=prev,
    )


def _range(
    default_first: date, default_last: date, start: str | None, end: str | None
) -> tuple[date, date]:
    """给了就用给的，没给就用盘上深度；顺序颠倒在这里响，不等读盘。"""
    lo = date.fromisoformat(start) if start else default_first
    hi = date.fromisoformat(end) if end else default_last
    check_range(lo, hi)
    return lo, hi


def _select_per_fold(
    slot: Slot,
    bars_by_code: Mapping[str, Sequence[Bar]],
    selection: fold_mod.Selection,
    assumptions: Assumptions,
    bands: BandLookup,
) -> tuple[list[Score], dict[str, Any]]:
    """一槽位的 walk-forward：每折在训练段选参、验证段评分；返回 (各折验证成绩, 末折参数)。

    参数选择发生在**训练段**、评分发生在**验证段**——同一段日子既选又评，初筛就失去
    样本外含义（§5.3 第一条）。各折选中的参数写在报告里：它们稳不稳，本身就是"高原还是
    尖峰"的证据（§5.3 第三条）。
    """
    valid_scores: list[Score] = []
    chosen: dict[str, Any] = {}
    for fold in selection.folds:
        train_scores = [
            runner.run_phase(
                slot,
                params,
                bars_by_code,
                fold.train,
                assumptions=assumptions,
                bands=bands,
                phase="train",
            )
            for params in slot.grid
        ]
        winner = runner.best(train_scores)
        chosen = dict(winner.params)
        valid_scores.append(
            runner.run_phase(
                slot,
                chosen,
                bars_by_code,
                fold.valid,
                assumptions=assumptions,
                bands=bands,
                phase="valid",
            )
        )
    return valid_scores, chosen


def _gate_row(slot: Slot, params: dict[str, Any], score: Score, scaled: Score) -> list[str]:
    """测试段一行的判定（5.4-1/2/5；样本下限 5.4-4 单独一列）。"""
    if score.metrics.trips == 0 or score.expectancy_pct is None:
        return [slot.name, str(params), "0", "—", "—", "—", "—", "无回合，出局"]
    g = stats.gate(score.metrics.wins, score.metrics.trips, score.expectancy_pct)
    scaled_ok = scaled.expectancy_pct is not None and scaled.expectancy_pct > 0
    sample_ok = score.metrics.trips >= 100 and min(score.trips_by_code.values(), default=0) >= 10
    streak_ok = score.metrics.max_loss_streak <= 8
    verdict = "过" if g.ok and scaled_ok and sample_ok and streak_ok else "不过"
    return [
        slot.name,
        str(params),
        str(score.metrics.trips),
        f"{g.win_rate:.1%}",
        f"p={g.binom_p:.4f}",
        f"{score.expectancy_pct * 10000:.1f}bp",
        f"{scaled.expectancy_pct * 10000:.1f}bp" if scaled.expectancy_pct is not None else "—",
        f"{verdict}（样本{'✓' if sample_ok else '✗'} 连败{'✓' if streak_ok else '✗'}）",
    ]


def _compete(
    args: argparse.Namespace,
    *,
    assumptions: Assumptions,
    minute: Mapping[str, Sequence[Bar]],
    prev: Mapping[str, Mapping[date, float]],
    bands: BandLookup | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """竞争主体。返回 (报告 markdown, 各槽位测试段明细供调用方复用)。

    `bands` 缺省在装配点现造（读主数据/日历/gate.toml）；测试注入假查表，免读盘。
    """
    active = {code: series for code, series in minute.items() if series}
    empty = sorted(set(minute) - set(active))
    if not active:
        raise ValueError("票池里没有一只票在盘上有分钟线：先跑 zx-minute")
    if bands is None:
        bands = _band_lookup(args.end_parsed, prev)
    days = sorted({bar.trade_date for series in active.values() for bar in series})
    selection = fold_mod.split(tuple(days), folds=args.folds)

    finals: list[dict[str, Any]] = []
    rows_valid: list[str] = []
    for slot in slots():
        valid_scores, chosen = _select_per_fold(slot, active, selection, assumptions, bands)
        pooled = runner.merge(valid_scores)
        rate = pooled.win_rate
        passed = rate is not None and rate > 0.5
        picks = "、".join(str(s.params) for s in valid_scores)
        rows_valid.append(
            f"| {slot.name} {slot.title} | {picks} | {pooled.metrics.trips} | "
            f"{rate:.1%} | {pooled.streak} | {'入围' if passed else '初筛出局'} |"
        )
        if not passed:
            continue
        base = runner.run_phase(
            slot, chosen, active, selection.test, assumptions=assumptions, bands=bands, phase="test"
        )
        scaled = runner.run_phase(
            slot,
            chosen,
            active,
            selection.test,
            assumptions=assumptions.with_cost_scaled(1.5),
            bands=bands,
            phase="test-x1.5",
        )
        finals.append({"slot": slot, "params": chosen, "base": base, "scaled": scaled})

    lines = [
        "# 做T 策略竞争报告 · " + date.today().isoformat(),
        "",
        f"- 票池：{'、'.join(sorted(active))}"
        + (f"（无分钟线未入池：{'、'.join(empty)}）" if empty else ""),
        f"- 数据：`{args.dataset}`；{describe_depth(args.dataset)}",
        f"- 区间：{args.start_parsed} → {args.end_parsed}",
        f"- 切分：{args.folds} 折扩张窗；测试段 {selection.test[0]} → {selection.test[-1]}"
        f"（{len(selection.test)} 个交易日，只评这一次——§5.3）",
        "",
        "## 一、验证段（初筛，胜率 > 50% 入围；§四-2）",
        "",
        "| 槽位 | 各折选中参数 | 回合 | 胜率 | 最大连败 | 判定 |",
        "|---|---|---|---|---|---|",
        *rows_valid,
        "",
        "## 二、测试段（终选，一次性；5.4-1/2/4/5）",
        "",
        "| 槽位 | 参数 | 回合 | 胜率 | 显著性 | 净期望 | ×1.5 期望 | 判定 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    detail: list[dict[str, Any]] = []
    for final in finals:
        lines.append(
            "| "
            + " | ".join(_gate_row(final["slot"], final["params"], final["base"], final["scaled"]))
            + " |"
        )
        detail.append(final)
    lines += [
        "",
        "## 三、结论",
        "",
    ]
    if not finals:
        lines.append(
            "**全部不启用**——没有候选同时通过初筛与测试段门槛。这是合法结论（07 §一/§七.3），"
            "不降门槛、不加赛，等数据或策略有实质变化再重跑。"
        )
    else:
        winners = "、".join(f"{f['slot'].name}" for f in finals)
        lines.append(
            f"通过全部门槛的候选：{winners}。下一步：组合评审（§四-3 多数投票）+ "
            "正式成绩单走 `zx-backtest` 登记台账（backtest_id），再进 Step 8 模拟信号期。"
        )
    lines += [
        "",
        "> 门槛数字见 07 §5.4（定死防放水）；本报告是选择过程的留痕，正式单次成绩以台账为准。",
        "",
    ]
    return "\n".join(lines), detail


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        if not codes:
            raise ValueError("--codes 是空的：没有票池就没有竞争")
        if len(codes) > 10:
            raise ValueError(f"票池 {len(codes)} 只：07 §二 的上限是 10 只（用户真实持仓）")
        dataset = check_dataset(args.dataset)
        if args.folds < 2:
            raise ValueError(f"--folds = {args.folds}：一折的 walk-forward 就是单段定终身")
        assumptions = load_spec(config.backtest_config_file())
        args.start_parsed, args.end_parsed = _range_from_args(args, dataset)
        minute = read_minute(codes, args.start_parsed, args.end_parsed, dataset)
        prev = previous_closes(codes, args.start_parsed, args.end_parsed)
        report, _ = _compete(args, assumptions=assumptions, minute=minute, prev=prev)
        out = args.out or config.reports_dir() / "compete" / date.today().isoformat()
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.md").write_text(report, encoding="utf-8")
        print(report)
        print(f"报告落 {out / 'report.md'}")
        return 0
    except (ValueError, OSError) as exc:
        print(f"竞争没开始：{exc}", file=sys.stderr)
        return 2


def _range_from_args(args: argparse.Namespace, dataset: str) -> tuple[date, date]:
    """区间解析：给了就用给的（check_range 判顺序），没给就从盘上深度取。"""
    from zhixing_quant.storage.query import depth

    if args.start and args.end:
        return _range(date.min, date.max, args.start, args.end)
    cover = depth(dataset, root=config.parquet_dir())
    if cover is None:
        raise ValueError(f"{dataset} 在盘上没有数据：先跑 zx-minute")
    return _range(cover.first, cover.last, args.start, args.end)


if __name__ == "__main__":
    sys.exit(main())
