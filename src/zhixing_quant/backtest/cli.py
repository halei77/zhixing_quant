"""`zx-backtest`：回测的装配层（ADR-0010 决定 1、决定 10；09 §五）。

全链路**唯一**允许 import `storage` 与 `sources` 的模块，也是唯一碰磁盘与台账的地方。
`backtest/` 另外七个模块之所以能被属性测试在无磁盘环境下轰一万次，代价就是这个文件：
所有"从盘上读、往库里写"都收在这里，收在一处才查得清哪一行代码动过数据。
（这条边界不是文档里的一句话，`tests/test_backtest_boundaries.py` 逐文件扫 import 判它。）

它只做四件事：读K线 → 造板 → 跑三档成本 → 落产物与登记。判定一条都不在这里写（在
`report.py`），成本假设一条都不在这里编（在 `config/backtest.toml`），票池一条都不在这里猜
（不给 `--codes` 就直接退出 2，与 `zx-minute` 同一条理由：范围是等用户裁决的事）。

产物目录名带的是**运行时间戳**而不是台账编号：`report_path` 在登记时就必填，而编号与"第 N 次"
要等插入之后才拿得到，报告首段又必须引用它。顺序只能是 建目录 → 登记 → 写文件，所以名字只能
先由时间戳定下（ADR-0010 决定 10）。

退出码：跑完并登记 0；根本没开始（缺票池、假设表读不出、dataset 不是分钟线、区间颠倒、策略路径
不存在、快照读不出、日线因子没落盘）2；`--task` 指向一个不存在的任务 3。**回测结论是负的也退出
0**——"这套信号不赚钱"是一次成功的实验，不是一次失败的运行（07 §一：宁可不用是合法结论）。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from zhixing_quant import config
from zhixing_quant.backtest import report as page
from zhixing_quant.backtest import spec
from zhixing_quant.backtest.engine import BandLookup, Strategy, run
from zhixing_quant.backtest.limits import PriceBand
from zhixing_quant.backtest.metrics import Metrics, measure
from zhixing_quant.backtest.report import Identity, Probe, Scope, equity_csv, render
from zhixing_quant.domain.bar import Bar
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.limits import limit_pct, new_listing_days
from zhixing_quant.domain.security import SecurityMaster
from zhixing_quant.quality import gate_config
from zhixing_quant.sources.akshare import master as master_source
from zhixing_quant.sources.akshare.calendar import load_calendar
from zhixing_quant.sources.rows import SourceSchemaError
from zhixing_quant.storage import layout
from zhixing_quant.storage.query import Adjust, Cover, Unadjustable, depth, read_bars
from zhixing_quant.tasks import db
from zhixing_quant.tasks.store import Backtest, Store, TaskNotFound

#: 03-4.3 的三档：成本上下各浮动 50%，中间那档就是本次真正的假设。
FACTORS: tuple[float, ...] = (0.5, 1.0, 1.5)
#: 本次假设在哪一档上。台账登记、报告正文引用的都是这一档的成绩，另两档只进敏感性表。
BASE_FACTOR = 1.0
#: 往回多带几天日线，好让区间**首日**也有昨收可判板。30 个日历天够跨一个长假。
PREVIOUS_DAYS = 30
#: 没跑出来的那个数在 JSON 里是 null，不是 0（04 §四 的三态规矩一路带到台账）。
NO_OUT_OF_SAMPLE = ""


class ScopeError(ValueError):
    """范围没给全，或者给了一个这台引擎不该吃的 dataset：与 `zx-minute` 同一条 fail-closed。"""


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期要写成 2026-09-17，收到 {text!r}") from exc


def _codes(text: str) -> tuple[str, ...]:
    """`--codes 600519,000001` → 去重排序的代码元组。

    排序是为了参数哈希：同一批票写成两种顺序不是两次实验，台账要把它们认成同一条。
    """
    codes = sorted({code.strip() for code in text.split(",") if code.strip()})
    if not codes:
        raise argparse.ArgumentTypeError("--codes 至少要写一只票：票池不许由这里猜")
    return tuple(codes)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zx-backtest",
        description="日内做T 回测：读干净区分钟线 → 事件驱动引擎 → 报告 + 台账登记",
    )
    parser.add_argument(
        "--strategy",
        required=True,
        metavar="包.模块:类名",
        help="策略实现的位置，例如 zhixing_quant.backtest.engine:NeverSignal",
    )
    parser.add_argument(
        "--version", required=True, help="策略版本（台账要它分辨同一份代码的两次改动）"
    )
    parser.add_argument("--codes", required=True, type=_codes, metavar="代码[,代码…]")
    parser.add_argument("--start", required=True, type=_day)
    parser.add_argument("--end", required=True, type=_day)
    parser.add_argument(
        "--dataset", default=layout.MINUTE_5, help=f"分钟线 dataset，默认 {layout.MINUTE_5}"
    )
    parser.add_argument("--task", type=int, default=None, help="挂到哪条任务上（zx-task 的 id）")
    return parser


def load_strategy(dotted: str) -> Strategy:
    """`包.模块:类名` → 一个实例。没有默认策略：装一个"看起来会动"的桩来自欺。"""
    module_name, _, class_name = dotted.partition(":")
    if not module_name or not class_name:
        raise ScopeError(f"策略要写成 模块:类名，收到 {dotted!r}")
    try:
        factory: Any = getattr(importlib.import_module(module_name), class_name)
    except ImportError as exc:
        raise ScopeError(f"导入不了 {module_name}：{exc}") from exc
    except AttributeError as exc:
        raise ScopeError(f"{module_name} 里没有 {class_name}：{exc}") from exc
    try:
        strategy: Strategy = factory()
    except TypeError as exc:
        # 需要构造参数的策略是 Step 7 候选池的事（那一步要先定义"参数怎么进台账"）。
        raise ScopeError(f"{dotted} 不能无参构造：这一步的策略必须零参数，{exc}") from exc
    if not callable(getattr(strategy, "signals", None)) or not getattr(strategy, "name", ""):
        raise ScopeError(f"{dotted} 不是策略：既要有 name，也要有 signals(view)")
    return strategy


def check_dataset(dataset: str) -> str:
    """这台引擎只吃分钟线。日线是另一种引擎（07 §5.1 第一条禁令）。"""
    if not dataset.startswith("minute_"):
        raise ScopeError(
            f"日内引擎吃的是 {layout.MINUTE_5}/{layout.MINUTE_30}/{layout.MINUTE_60}，"
            f"收到 {dataset!r}：日线向量化是另一个引擎，混用就是 07 §5.1 的第一条禁令"
        )
    layout.dataset_spec(dataset)  # 认不出的名字在这里响，不等 glob 出空目录再假装"没数据"
    return dataset


def check_range(start: date, end: date) -> None:
    """区间颠倒属于参数层就该响的事，不该等到第一次读盘才由查询层抛出来。

    写在两个 `required` 参数之外的地方判：argparse 只管单个字段像不像日期，管不了两者之间的
    顺序——而一个逃逸到 `main` 之外的 `ValueError` 会变成一段 traceback，不是一句"没开始"。
    """
    if end < start:
        raise ScopeError(f"区间颠倒了：--start {start} 晚于 --end {end}")


def build_bands(
    master: SecurityMaster,
    calendar: TradingCalendar,
    *,
    limits_pct: Mapping[str, float],
    no_limit_days: Mapping[str, int],
    prev_closes: Mapping[str, Mapping[date, float]],
) -> BandLookup:
    """造引擎要的板查表：主数据 + 日历 + `gate.toml` 的 R004 那张表 + 昨收。

    顺序是有意的：**先问在不在无涨跌幅窗口，再问有没有昨收**。新股上市首日根本没有昨收，
    先查昨收就会把"制度上今天不设限"报成"我判不出来"——一笔本来能成交的单子被一个数据缺口
    挡掉，而报告里两个原因长得一样（决定 4：判不出与不存在是两件事）。
    """

    def bands(code: str, on: date) -> PriceBand:
        try:
            state = master.state_on(code, on)
        except KeyError:
            return PriceBand.unknown(f"主数据没有 {code}：板块与 ST 都判不出，不许假设一个上限")
        days = new_listing_days(state.board, no_limit_days)
        if days and master.no_limit_period(code, on, calendar, days):
            return PriceBand.unlimited(f"{code} 在上市后 {days} 个交易日的无涨跌幅窗口内")
        prev = prev_closes.get(code, {}).get(on)
        if prev is None:
            return PriceBand.unknown(f"{code} 在 {on} 没有昨收：首日、停牌或日线缺失，板判不出来")
        return PriceBand.bound(
            limit_pct(state.board, is_st=state.is_st, limits_pct=limits_pct), prev
        )

    return bands


def read_minute(codes: Sequence[str], start: date, end: date, dataset: str) -> dict[str, list[Bar]]:
    """一只票一段分钟线，全部**后复权**（ADR-0010 决定 9）。

    `root` 写死成 `config.parquet_dir()`：查询层与采集层必须落在同一个根上，写成数据根的话
    两边都读到空目录，"这根K线没数据"就成了假话。
    """
    return _read(codes, start, end, dataset, adjust="backward")


def _read(
    codes: Sequence[str], start: date, end: date, dataset: str, *, adjust: Adjust
) -> dict[str, list[Bar]]:
    return {
        code: read_bars(code, start, end, adjust=adjust, dataset=dataset, root=config.parquet_dir())
        for code in codes
    }


def previous_closes(codes: Sequence[str], start: date, end: date) -> dict[str, dict[date, float]]:
    """每只票每个交易日的昨收（后复权口径，与引擎吃的那批K线同一把尺）。

    区间首日也要有昨收，所以日线往回多读 `PREVIOUS_DAYS` 个日历天。这里不读分钟线：分钟线
    自己不带因子（ADR-0009 决定 4），而"上一交易日收盘价"本来就是日线上的一个数。
    """
    earlier = start - timedelta(days=PREVIOUS_DAYS)
    out: dict[str, dict[date, float]] = {}
    for code, bars in _read(codes, earlier, end, layout.DAILY, adjust="backward").items():
        by_day: dict[date, float] = {}
        previous: float | None = None
        for bar in bars:
            if previous is not None:
                by_day[bar.trade_date] = previous
            previous = bar.close
        out[code] = by_day
    return out


def describe_depth(dataset: str) -> str:
    """盘上真实深度，原样印进报告第一节：区间之外有没有数，不该让读者猜。"""
    cover: Cover | None = depth(dataset, root=config.parquet_dir())
    if cover is None:
        return f"盘上一个 `{dataset}` 分区都没有（先跑 `zx-minute`）"
    return (
        f"`{dataset}` 在盘上覆盖 {cover.first} → {cover.last}，"
        f"{cover.days} 个交易日 × {cover.symbols} 只票"
    )


def disclosures(
    *, st_since: date, end: date, empty: Sequence[str], dataset: str, delay_bars: int
) -> tuple[str, ...]:
    """这一页不能保证的事。每一条都对着 ADR-0010 的一条代价，偏差方向写死，不许含糊。"""
    missing = (
        f"本次票池每只票在 `{dataset}` 都有行。"
        if not empty
        else f"本次有 {len(empty)} 只票在 `{dataset}` 上一根都没有：" + "、".join(empty) + "。"
    )
    whole_range = (
        ()
        if end >= st_since
        else (f"整个区间都早于 ST 判定生效日 {st_since}：上面第一条覆盖这一跑的每一天。",)
    )
    return (
        f"ST 帽只从快照抓取日 {st_since} 起算，更早的日期一律按非 ST 上限判板：ST 票的板取宽了"
        "（代价三，方向 = 高估可成交性）。停牌区间没有源，那一半由『没有K线就没有成交』接住。",
        "分钟线源不含退市票：区间内退市的票整段缺行，股票池因此自带**幸存者偏差**，"
        "方向 = 高估收益与可成交性（03-4.6）。" + missing,
        "一字板之外一律判成可成交：没有盘口深度，大单的冲击成本与部分成交都不建模"
        "（代价二，方向 = 高估可成交性）。",
        f"成交在信号之后第 {delay_bars} 根K线的开盘价：结论对 `delay_bars` 敏感，"
        "而 03-4.3 的敏感性只覆盖成本、不覆盖它（代价一）。要换结论就重跑一档延迟。",
        "本次只有**一段**数据，没有样本外段：07-5.3 的三段切分与 walk-forward 是 Step 7 的事，"
        "所以这一页的任何数字都不构成 07-5.4 的启用结论。",
        *whole_range,
    )


def numbers(m: Metrics) -> dict[str, Any]:
    """台账那一格：07 §七 验收 5 的五个数加钱的三个数。算不出来的存 null，不存 0。"""
    return {
        "trips": m.trips,
        "win_rate": m.win_rate,
        "payoff": m.payoff,
        "expectancy": m.expectancy,
        "max_loss_streak": m.max_loss_streak,
        "fly_rate": m.fly_rate,
        "add_rate": m.add_rate,
        "final_pnl": m.final_pnl,
        "trip_pnl": m.trip_pnl,
        "unrealized": m.unrealized,
        "base_pnl": m.base_pnl,
    }


def assumptions_json(assumptions: spec.Assumptions) -> dict[str, Any]:
    """成本与执行与仓位假设一份 JSON：台账要答得出"那次跑的是哪张费率表"。"""
    return {
        "cost": {
            "commission_pct": assumptions.cost.commission_pct,
            "commission_min": assumptions.cost.commission_min,
            "stamp_pct": assumptions.cost.stamp_pct,
            "transfer_pct": assumptions.cost.transfer_pct,
            "slippage_pct": assumptions.cost.slippage_pct,
        },
        "execution": {
            "delay_bars": assumptions.execution.delay_bars,
            "max_participation_pct": assumptions.execution.max_participation_pct,
        },
        "position": {
            "lot_size": assumptions.sizing.lot_size,
            "base_lots": assumptions.sizing.base_lots,
        },
    }


def params_hash(
    *,
    strategy: str,
    version: str,
    dataset: str,
    start: date,
    end: date,
    codes: Sequence[str],
    assumptions: spec.Assumptions,
) -> str:
    """同一次实验 = 同一个哈希（ADR-0010 决定 10）。规范化 JSON 的 sha256 前 12 位。

    票池与区间**进哈希**是有意的：换票池、换区间不是"同一份参数再跑一次"而是另一个实验，
    它该有自己的第 1 次。03-4.5 要防的是"改改参数挑最好那次报"——那件事由台账把每一行都留下
    来管，不由 run_no 把不同的实验混进同一个计数。
    """
    canonical = json.dumps(
        {
            "strategy": strategy,
            "version": version,
            "dataset": dataset,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "codes": sorted(codes),
            "assumptions": assumptions_json(assumptions),
            "sensitivity": list(FACTORS),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _label(now: Callable[[], datetime]) -> str:
    """目录名 = UTC 秒级时间戳。写 UTC 而不是本地：换台机器跑，目录名不该跟着变。"""
    return now().astimezone(UTC).strftime("%Y%m%d-%H%M%S")


def _fresh_dir(base: Path) -> Path:
    """挑一个还不存在的目录。撞上就加后缀——覆盖上一次运行的报告等于抹掉一条留痕。"""
    candidate = base
    bump = 1
    while candidate.exists():
        bump += 1
        candidate = base.with_name(f"{base.name}-{bump}")
    return candidate


def _register(
    store: Store,
    *,
    strategy: str,
    version: str,
    task: int | None,
    start: date,
    end: date,
    assumptions: spec.Assumptions,
    params: str,
    out: Path,
    verdict: str,
    metrics: Metrics,
) -> Backtest:
    """登记这一跑。状态用 `report.status` 折出来的三态，报告路径指向那个目录（09 §五）。

    样本外那一格留空：这一跑没有第二段（`disclosures` 里那条就是这个意思），填一个"看起来像"
    的数比留空危险得多。
    """
    return store.add_backtest(
        strategy=strategy,
        version=version,
        params_hash=params,
        data_start=start.isoformat(),
        data_end=end.isoformat(),
        cost_assumption=json.dumps(assumptions_json(assumptions), sort_keys=True),
        report_path=str(out),
        status=verdict,
        task_id=task,
        metrics_in=json.dumps(numbers(metrics), sort_keys=True),
        metrics_out=NO_OUT_OF_SAMPLE,
    )


def main(argv: Sequence[str] | None = None, *, now: Callable[[], datetime] = datetime.now) -> int:
    """跑一次回测：产物落在 `${ZX_DATA_ROOT}/backtests/<时间戳>/`，台账加一行。"""
    args = build_parser().parse_args(argv)
    con: Any = None
    try:
        dataset = check_dataset(args.dataset)
        check_range(args.start, args.end)
        strategy = load_strategy(args.strategy)
        con = db.connect(config.taskdb_file())
        return _execute(args, strategy=strategy, dataset=dataset, store=Store(con), now=now)
    except (
        ScopeError,
        spec.BacktestConfigError,
        gate_config.GateConfigError,
        SourceSchemaError,
        Unadjustable,
    ) as exc:
        print(f"没开始：{exc}", file=sys.stderr)
        return 2
    except TaskNotFound as exc:
        print(f"台账里没有这条任务：{exc.args[0] if exc.args else exc}", file=sys.stderr)
        return 3
    finally:
        if con is not None:
            con.close()


def _execute(
    args: argparse.Namespace,
    *,
    strategy: Strategy,
    dataset: str,
    store: Store,
    now: Callable[[], datetime],
) -> int:
    assumptions = spec.load(config.backtest_config_file())
    if args.task is not None:
        # 先验一票：跑完几分钟才发现任务号是敲错的，那几分钟白烧。抛出去就是退出码 3。
        store.get_task(args.task)
    params = params_hash(
        strategy=strategy.name,
        version=args.version,
        dataset=dataset,
        start=args.start,
        end=args.end,
        codes=args.codes,
        assumptions=assumptions,
    )
    # 读盘与三档跑完才建目录：数据没读出来就退出去（`Unadjustable`、坏快照），留一个空目录
    # 与一条没有报告的登记，等于在产物区里撒"这次跑过"的谎。
    bars, empty = _gather(args.codes, args.start, args.end, dataset)
    bands = _bands(args.codes, args.start, args.end)
    runs = {
        factor: run(
            bars, strategy=strategy, assumptions=assumptions.with_cost_scaled(factor), bands=bands
        )
        for factor in FACTORS
    }
    result = runs[BASE_FACTOR]
    base = measure(result)
    probes = tuple(Probe(factor, measure(runs[factor])) for factor in FACTORS)
    verdict = page.status(probes)

    out = _fresh_dir(config.backtests_dir() / _label(now))
    out.mkdir(parents=True)
    row = _register(
        store,
        strategy=strategy.name,
        version=args.version,
        task=args.task,
        start=args.start,
        end=args.end,
        assumptions=assumptions,
        params=params,
        out=out,
        verdict=verdict,
        metrics=base,
    )
    (out / "report.md").write_text(
        render(
            page.Report(
                identity=Identity(
                    strategy=strategy.name,
                    version=args.version,
                    params_hash=row.params_hash,
                    backtest_id=row.id,
                    run_no=row.run_no,
                ),
                scope=Scope(
                    dataset=dataset,
                    start=args.start,
                    end=args.end,
                    codes=tuple(args.codes),
                    bars=len(bars),
                    depth=describe_depth(dataset),
                ),
                assumptions=assumptions,
                metrics=base,
                result=result,
                output_dir=out,
                sensitivity=probes,
                disclosures=disclosures(
                    st_since=master_source.captured_on(config.golden_dir()),
                    end=args.end,
                    empty=empty,
                    dataset=dataset,
                    delay_bars=assumptions.execution.delay_bars,
                ),
            )
        ),
        encoding="utf-8",
    )
    (out / "equity.csv").write_text(equity_csv(result), encoding="utf-8")
    expectancy = page.NA if base.expectancy is None else f"{base.expectancy:+.4f} 元"
    print(
        f"#{row.id} 第 {row.run_no} 次回测 · {strategy.name} v{args.version} · "
        f"{row.status_label} · 回合 {base.trips} · 期望 {expectancy}"
    )
    print(f"报告：{out / 'report.md'}")
    return 0


def _bands(codes: Sequence[str], start: date, end: date) -> BandLookup:
    """把三样外部事实拼成引擎的板查表：主数据、日历、`gate.toml` 的 R004 那张表。

    三档成本共用同一个 `bands` 是有意的：敏感性只该动成本。昨收与板块跟费率无关，各档重算
    一遍只会多出三个"改了成本忘了改板"的接缝，而这三样东西都是磁盘读——重算还更贵。
    """
    master = master_source.read_master().to_master()
    calendar = load_calendar(until=end)
    params = gate_config.load(config.gate_config_file()).rule("R004").params
    limits: Mapping[str, float] = params["limits_pct"]
    no_limit: Mapping[str, int] = params["new_listing_no_limit_days"]
    return build_bands(
        master,
        calendar,
        limits_pct=limits,
        no_limit_days=no_limit,
        prev_closes=previous_closes(codes, start, end),
    )


def _gather(
    codes: Sequence[str], start: date, end: date, dataset: str
) -> tuple[list[Bar], list[str]]:
    """把所有票的分钟线合成一段，并记下哪些票一根都没有（那句披露的出处）。"""
    minute = read_minute(codes, start, end, dataset)
    bars = [bar for series in minute.values() for bar in series]
    return bars, [code for code, series in minute.items() if not series]
