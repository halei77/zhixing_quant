"""Step 3 验收 1 的度量：全市场规模下查单票历史要 < 100ms（01 §Step 3）。

为什么放在 `tools/` 而不是测试里：这条判据要的是"跟生产同量级的目录宽度 + 原生 ext4 的
随机读"，塞进 CI 要么把 CI 拖成几分钟，要么缩水成"三个文件里查一行很快"——后者测的是
Python 循环，不是验收标准。所以这里手动跑，数字进任务账本（09 §五）。

默认落到临时目录，且**永远用 `bench_daily` 这个数据集名**：基准要写两万个分区文件，落在
真数据的 `daily/` 旁边一旦清不干净，下次查询就会读到合成K线。

`--symbols` 决定目录宽度（`existing_partitions` 的 glob 成本），`--bars` 决定单文件大小
（Parquet 读取消本）。两个都拉满是 4430 × 1250 ≈ 550 万行；本机跑一次十几分钟，日常按
默认值即可，两个维度分别测得再相加就是上界。
"""

from __future__ import annotations

import argparse
import shutil
import statistics
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

from zhixing_quant.domain.bar import Bar
from zhixing_quant.storage import layout, query, write

#: 01 §Step 3 验收 1 的原文数字。改它等于改验收口径，要先改文档。
TARGET_MS = 100.0

#: 与真数据隔开的数据集名（见模块文档）。
BENCH = "bench_daily"


def synth(symbol: str, days: int) -> list[Bar]:
    """一只票 `days` 根K线：周末跳过，每约 250 天来一次除权（因子是阶梯函数，台阶数
    决定 `factor_on` 的扫描量，全给同一个因子测不出真实形状）。"""
    out: list[Bar] = []
    day = date(2019, 1, 2)
    price = 10.0
    factor = 1.0
    for index in range(days):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        if index and index % 250 == 0:
            factor *= 1.05
        price *= 1.001
        out.append(
            Bar(
                source=BENCH,
                symbol=symbol,
                trade_date=day,
                open=price,
                high=price * 1.02,
                low=price * 0.98,
                close=price,
                volume=1000.0,
                amount=price * 1000,
                adj_factor=factor,
            )
        )
        day += timedelta(days=1)
    return out


ADJUSTMENTS: tuple[query.Adjust, ...] = ("raw", "backward", "forward")


def sample_reads(root: Path, symbols: list[str], runs: int) -> dict[query.Adjust, list[float]]:
    """每个口径各测 `runs` 次单票查询（5 年窗口）。取中位数与最大值，不取平均：
    一次 GC 或页缓存未命中就能把平均数拉偏，而验收问的是"会不会慢到不能用"。"""
    span = (date(2020, 1, 1), date(2024, 12, 31))
    timings: dict[query.Adjust, list[float]] = {kind: [] for kind in ADJUSTMENTS}
    for adjust in timings:
        for i in range(runs):
            symbol = symbols[i % len(symbols)]
            started = time.perf_counter()
            query.read_bars(symbol, *span, adjust=adjust, dataset=BENCH, root=root)
            timings[adjust].append((time.perf_counter() - started) * 1000)
    return timings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=None, help="基准数据的落点，默认临时目录")
    parser.add_argument("--symbols", type=int, default=800, help="目录宽度（票数）")
    parser.add_argument("--bars", type=int, default=1250, help="每只票的K线根数")
    parser.add_argument("--runs", type=int, default=25, help="每个口径测多少次")
    parser.add_argument("--keep", action="store_true", help="跑完不删数据，打印路径")
    args = parser.parse_args(argv)

    scratch = args.root is None
    root = args.root if args.root is not None else Path(tempfile.mkdtemp(prefix="zx-bench-"))
    symbols = [f"{600000 + i:06d}" for i in range(args.symbols)]
    print(f"落盘 {len(symbols)} 票 × {args.bars} 根 → {root / BENCH}")

    rows = 0
    started = time.perf_counter()
    # 逐票生成逐票落：先把全市场生成完要几百 MB 内存，而基准要测的就是落盘这一段。
    for symbol in symbols:
        bars = synth(symbol, args.bars)
        rows += len(bars)
        write.store_bars(bars, dataset=BENCH, root=root)
    elapsed = time.perf_counter() - started
    partitions = sum(1 for _ in layout.dataset_dir(BENCH, root).rglob("*.parquet"))
    per_partition = elapsed / partitions * 1000
    print(f"  {rows} 行 / {elapsed:.1f}s / {partitions} 个分区 / 每分区 {per_partition:.1f} ms")

    timings = sample_reads(root, symbols, args.runs)
    worst = 0.0
    for adjust, values in timings.items():
        median = statistics.median(values)
        worst = max(worst, median)
        print(f"  单票 5 年 {adjust:8}：中位 {median:.1f} ms，最大 {max(values):.1f} ms")

    if scratch and not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    else:
        print(f"  数据留在 {root}（自己删）")
    if worst >= TARGET_MS:
        print(f"未达验收 1：中位数 {worst:.1f} ms ≥ {TARGET_MS} ms", file=sys.stderr)
        return 1
    print(f"验收 1 达标：最慢口径中位 {worst:.1f} ms < {TARGET_MS} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
