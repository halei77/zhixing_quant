"""R011 的全量重扫：把盘上所有 (票 × 天 × 周期) 组合对一遍，报出偏差的**分布**。

为什么有这么个东西，两条理由都是这两天踩出来的：

- ADR-0009 补充决定（2026-09-20）把"改了判据或换了源，全量重扫一遍是必须的一步"写成了规矩，
  而 02 §一 要求每一条规矩都对应一个可执行的检查。只写一句"要重扫"而没有可跑的东西，等于没写。
- #33（`tolerance_pct` 要不要按全量重标定）要的是**别人能重跑的数**。2026-09-20 那批结论
  （-0.50%~-8.64%、命中 105 个 (票,天)）出自一只 `/tmp` 探针，谁也没法复核，那它就跟 agent
  自述同级——而坑 #39 教的就是"拿自己的输出当依据"这一类错。

它不做的事：**不改容差，也不接受把容差传进来**。判据与容差都从 `config/gate.toml` 与
`quality/reconcile.py` 那两处拿（全项目只有一个"多少算噪声"）。想回答"换成 1% 会少报几条"，
看的是下面那张分档表——分档是**读出来的数**，不是一个能传进去的口径。

日常任务仍然只报报告日（`sources/jobs/minute.py` 的 `reconcile_pool`，理由写在 ADR-0009 决定 6
修订里）；这个工具是例外通道，跑一次几分钟，改判据时才跑。

退出码：0 = 只报了 `volume` 或什么都没报；1 = 报了 `volume` 以外的任何一种（那是要人看的那种）；
2 = 盘上没有任何分钟行（"没查"不等于"查了且干净"）。
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from zhixing_quant import config
from zhixing_quant.domain.bar import Bar
from zhixing_quant.quality import gate_config
from zhixing_quant.quality.reconcile import KINDS, Finding, daily_gap, reconcile_day
from zhixing_quant.storage import layout
from zhixing_quant.storage.query import read_bars

#: 全量扫描的时间窗：只要宽到盖住任何一次落盘过的年份就行，真正的边界由盘上的行决定。
LO, HI = date(2000, 1, 1), date(2100, 1, 1)

#: 分档表的刻度（百分数）。跨过 `tolerance_pct` 的那一档就是"今天会报出来的那些"。
BANDS = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0)


class Scan:
    """一次重扫的账：查了多少组合、报了什么、偏差长什么样。"""

    def __init__(self) -> None:
        self.combos = 0
        self.findings: list[Finding] = []
        #: 每个 (票,天,周期) 组合的"合成日K 相对日线"的有符号量偏差（负 = 分钟侧偏少）。
        self.gaps: list[tuple[float, str, str, date]] = []

    @property
    def kinds(self) -> Counter[str]:
        out: Counter[str] = Counter()
        out.update(f.kind for f in self.findings)
        return out


def scan(
    periods: Sequence[str] = ("5", "30", "60"),
    *,
    root: Path | None = None,
    symbols: Sequence[str] | None = None,
    tolerance_pct: float | None = None,
) -> Scan:
    """盘上所有 (票 × 天 × 周期) 对一遍。

    `symbols=None` 用"该周期在盘上有行的那些票"：审的是已经落盘的东西，一次抓取都不发起。
    每只票每个 dataset 只读一次整段（不是每天读一次），493 天 × 五只票是 5 次读盘而不是 2,465 次。
    `tolerance_pct=None` 时从 `gate.toml` 的 `[defaults]` 读——与 R004/R007 同一个数。
    """
    tol = (
        float(gate_config.load(config.gate_config_file()).defaults["tolerance_pct"])
        if tolerance_pct is None
        else tolerance_pct
    )
    out = Scan()
    for period in periods:
        dataset = layout.minute_dataset(period)
        pool = layout.dataset_symbols(dataset, root=root) if symbols is None else tuple(symbols)
        for symbol in pool:
            daily_bars = read_bars(symbol, LO, HI, dataset=layout.DAILY, root=root)
            daily = {b.trade_date: b for b in daily_bars}
            by_day: dict[date, list[Bar]] = defaultdict(list)
            for bar in read_bars(symbol, LO, HI, dataset=dataset, root=root):
                by_day[bar.trade_date].append(bar)
            # 首日不含：区间是从盘上那一刻起的，那天的分钟根可能只有尾巴一段，
            # 拿它跟整天比是探针自己造的偏差，不是源的。家里这份盘上它不是假设：三个
            # dataset 的首日各只有尾盘 2 根，对出来 -48%~-96.9%，足以压弯下面那张分档表。
            for day in sorted(by_day)[1:]:
                out.combos += 1
                bars = by_day[day]
                line = daily.get(day)
                out.findings += list(
                    reconcile_day(symbol, day, dataset, bars, line, tolerance_pct=tol)
                )
                gap = daily_gap(bars, line)
                if gap is not None:
                    out.gaps.append((gap, dataset, symbol, day))
    return out


def render(s: Scan, *, worst: int = 5) -> str:
    """把一次重扫写成给人读的几段。计数一律给三个层级：条 / 组合 / 独立交易日。"""
    lines: list[str] = []
    tally = s.kinds
    lines.append(f"扫描 {s.combos} 个 (票,天,周期) 组合")
    lines.append("finding 计数：" + "、".join(f"{k}={tally.get(k, 0)}" for k in KINDS))
    hit = {(f.symbol, f.day) for f in s.findings if f.kind == "volume"}
    lines.append(
        f"volume 命中：{len(hit)} 个 (票,天)，分布在 {len({d for _, d in hit})} 个独立交易日"
    )
    if s.gaps:
        gaps = sorted(g[0] for g in s.gaps)
        lines.append(
            f"量偏差（负 = 分钟合成比日线少）：最小 {gaps[0]:.2f}%、中位 "
            f"{gaps[len(gaps) // 2]:.2f}%、最大 {gaps[-1]:.2f}%"
        )
        lines.append("  超过阈值的组合数（含负方向同绝对值）：")
        for band in BANDS:
            n = sum(1 for g in s.gaps if abs(g[0]) > band)
            lines.append(f"    >{band:>4.1f}% : {n}")
        lines.append(f"  最差的 {worst} 个：")
        for gap, dataset, symbol, day in sorted(s.gaps)[:worst]:
            lines.append(f"    {gap:7.2f}%  {dataset:<9} {symbol} {day}")
    for f in s.findings:
        if f.kind != "volume":
            lines.append(f"  [{f.kind}] {f.symbol} {f.day} {f.dataset}：{f.detail}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reconcile_history",
        description="R011 全量重扫：改了判据或换了源之后跑一次，日常不需要。",
    )
    parser.add_argument("--periods", default="5,30,60", help="要扫的分钟周期，逗号分隔")
    parser.add_argument("--symbols", default=None, help="限定票池；默认扫该周期在盘上有行的所有票")
    args = parser.parse_args(argv)
    periods = [p.strip() for p in args.periods.split(",") if p.strip()]
    symbols = (
        None if args.symbols is None else [s.strip() for s in args.symbols.split(",") if s.strip()]
    )
    result = scan(periods, symbols=symbols)
    if result.combos == 0:
        print("盘上没有可对的分钟行（任何周期都没有第二天）：这次什么都没查，不算干净")
        return 2
    print(render(result))
    serious = {k: n for k, n in result.kinds.items() if k != "volume" and n}
    if serious:
        print(f"\n退出 1：{serious} 是要人看的那几种")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
