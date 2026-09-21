"""数据源最小探测：把"这个接口能不能用"从猜的变成有档可查的（Step 9 前置；04 §五
有偿转接源专项"只接实测可用接口清单内的接口"——本工具就是那份清单的生产器）。

方法纪律承袭 Qoute 调用纪律：每个接口先单标的 + 少字段 + 短区间，跑通才算"可用"，
跑不通连同错误一起落档。探测是只读的，产出是证据不是结论：通过探测 ≠ 准进干净区，
进门禁还要黄金样本、R 规则与逐表审计（04 §五）。

产出：--out 下一个 JSON 一本 summary.md。JSON 留全量字段名与首行样本（截断），
summary 给人快速读。退出码：跑完即 0——探测到失败是正常结果，不是工具失败；
参数错误 / 输出目录拒绝落进数据根以外未指定路径时 2。

超时：akshare 没有 per-call 超时参数，挂起的请求用线程池 result(timeout) 掐表。
掐表后那个线程可能还挂着，本工具是短命进程，随退出带走——别把它当长驻服务调。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date
from importlib import import_module
from pathlib import Path
from typing import Any

from zhixing_quant import config

TIMEOUT = 90.0
BODY_CAP = 4_096

#: 双源入口（ADR-0014 决定 2；URL 出自 Qoute nodea/client.py 与 2026-09-21 实测）。
RELAYS: Mapping[str, str] = {
    "rds": "http://datahubco.com/app-api/openapi/v1/tushare",
    "promax": "https://pcd.mobcvb.cn/tushare/pro",
}

#: A 桶探测对象（docs/01-路线图 Step 9 前置清单）。kwargs 刻意小：探测不追求覆盖，追求
#: "最小可复现的可用性证据"。同一接口给多个变体时按序尝试，先通者赢（变体是想覆盖
#: 签名差异，不是重试——重试纪律属于采集任务，不属于探测）。
AK_PROBES: Sequence[tuple[str, str, Sequence[tuple[str, str, Mapping[str, object]]]]] = (
    (
        "profit_forecast_em",
        "盈利预测（东财）",
        (("默认", "stock_profit_forecast_em", {"symbol": "600519"}),),
    ),
    (
        "rank_forecast_cninfo",
        "研报预测排行（巨潮）",
        (("默认", "stock_rank_forecast_cninfo", {"date": "20260918"}),),
    ),
    (
        "financial_indicator",
        "财务指标（新浪）",
        (
            (
                "按年",
                "stock_financial_analysis_indicator",
                {"symbol": "600519", "start_year": "2023"},
            ),
        ),
    ),
    (
        "dividend",
        "分红",
        (
            ("巨潮按票", "stock_dividend_cninfo", {"symbol": "600519"}),
            ("历史全表", "stock_history_dividend", {}),
        ),
    ),
    (
        "repurchase",
        "回购（东财全市场）",
        (("默认", "stock_repurchase_em", {}),),
    ),
    (
        "pledge",
        "股权质押",
        (
            ("按日全市场", "stock_gpzy_pledge_ratio_em", {"date": "20260918"}),
            ("个股明细", "stock_gpzy_individual_pledge_ratio_detail_em", {"symbol": "600519"}),
        ),
    ),
    (
        "restricted_release",
        "解禁",
        (("默认", "stock_restricted_release_stockholder_em", {"symbol": "600519"}),),
    ),
    (
        "margin",
        "两融",
        (
            ("沪市汇总", "stock_margin_sse", {"start_date": "20260901", "end_date": "20260918"}),
            ("深市汇总", "stock_margin_szse", {"start_date": "20260901", "end_date": "20260918"}),
        ),
    ),
    (
        "main_holder",
        "主力股东",
        (("默认", "stock_main_stock_holder", {"stock": "600519"}),),
    ),
    (
        "sw_index",
        "申万行业",
        (
            ("二级列表", "sw_index_second_info", {}),
            ("三级成分", "sw_index_third_cons", {"symbol": "801010"}),
        ),
    ),
)

#: B 桶探测对象：转接源上的六张表（ADR-0014 决定 4 逐表验收的先行证据）。daily 锚点行
#: 与干净区对账，其余看"接口通不通 + 字段长什么样"。
RELAY_PROBES: Sequence[tuple[str, str, str]] = (
    ("daily_anchor", "日线锚点（对账 600519@2026-09-18 收盘）", "daily"),
    ("forecast", "盈利预期 FY1-FY3", "forecast"),
    ("fina_audit", "非标审计意见", "fina_audit"),
    ("stk_limit", "涨跌停价", "stk_limit"),
    ("index_daily", "指数日线", "index_daily"),
    ("stk_holdernumber", "股东户数", "stk_holdernumber"),
    ("cyq_perf", "筹码胜率", "cyq_perf"),
    ("daily_basic", "估值每日指标（PE/PB/市值）", "daily_basic"),
)

RELAY_PARAMS: Mapping[str, Mapping[str, str]] = {
    "daily": {"ts_code": "600519.SH", "start_date": "20260918", "end_date": "20260918"},
    "forecast": {"ts_code": "600519.SH", "limit": "5"},
    "fina_audit": {"ts_code": "600519.SH", "limit": "5"},
    "stk_limit": {"trade_date": "20260918", "limit": "5"},
    "index_daily": {"ts_code": "000001.SH", "start_date": "20260914", "end_date": "20260918"},
    "stk_holdernumber": {"ts_code": "600519.SH", "limit": "5"},
    "cyq_perf": {"ts_code": "600519.SH", "limit": "5"},
    "daily_basic": {"ts_code": "600519.SH", "limit": "5"},
}


#: 探测的一次实际执行：给参数，回一个可 JSON 化的结果 dict。Callable 别名而非
#: Protocol：mypy 对回调 Protocol 的结构性检查在这里会误报，别名没有这个毛病。
Invoke = Callable[[str, str, Mapping[str, object]], dict[str, Any]]


@dataclass(frozen=True)
class Attempt:
    label: str
    target: str  # akshare 函数名，或转接源的 api 名
    kwargs: Mapping[str, object]


@dataclass(frozen=True)
class Probe:
    name: str
    bucket: str
    note: str
    attempts: tuple[Attempt, ...]


@dataclass
class Result:
    name: str
    bucket: str
    note: str
    ok: bool
    winner: str | None
    rows: int | None
    columns: list[str] = field(default_factory=list)
    sample: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def _summarize(frame: Any) -> tuple[int, list[str], dict[str, Any]]:
    """DataFrame → (行数, 列名, 首行样本)。样本值截断，防一页报告被长文本淹掉。"""
    rows = [dict(r) for r in frame.to_dict(orient="records")]
    columns = [str(c) for c in frame.columns]
    sample = {}
    if rows:
        for key, value in rows[0].items():
            text = str(value)
            sample[str(key)] = text[:120]
    return len(rows), columns[:40], sample


def _ak_invoke(_attempt: str, target: str, kwargs: Mapping[str, object]) -> dict[str, Any]:
    """真的调 akshare（探测运行时才 import，与 fetch.py 同一个Lazy 理由）。"""
    ak: Any = import_module("akshare")
    call = getattr(ak, target, None)
    if call is None:
        raise AttributeError(f"akshare 没有 {target}")
    frame = call(**dict(kwargs))
    rows, columns, sample = _summarize(frame)
    return {"rows": rows, "columns": columns, "sample": sample}


def _relay_invoke(relay: str) -> Invoke:
    """转接源 GET。key 从配置指的文件读，绝不落进结果 dict。"""

    def invoke(_attempt: str, target: str, kwargs: Mapping[str, object]) -> dict[str, Any]:
        key_file = config.relay_key_file(f"{relay}.key")
        key = key_file.read_text(encoding="utf-8").strip()
        query = urllib.parse.urlencode({k: str(v) for k, v in kwargs.items()})
        url = f"{RELAYS[relay]}/{target}?{query}"
        request = urllib.request.Request(url, headers={"X-API-Key": key})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {exc.read()[:200]!r}") from exc
        data = body.get("data") or {}
        items = data.get("items") or []
        fields = [str(f) for f in data.get("fields") or []]
        sample = {}
        if items:
            sample = {f: str(v)[:120] for f, v in zip(fields, items[0], strict=False)}
        return {
            "http": 200,
            "code": body.get("code"),
            "msg": str(body.get("msg"))[:120],
            "rows": len(items),
            "columns": fields[:40],
            "sample": sample,
            "raw_head": json.dumps(body, ensure_ascii=False)[:BODY_CAP],
        }

    return invoke


def _timed(invoke: Invoke, attempt: Attempt) -> dict[str, Any]:
    """掐表执行。超时也记成结果的一种——"挂住不返"本身就是可用性证据。"""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(invoke, attempt.label, attempt.target, attempt.kwargs)
        return future.result(timeout=TIMEOUT)
    finally:
        pool.shutdown(wait=False)


def run_probe(probe: Probe, invoke: Invoke) -> Result:
    """按序试变体，先通者赢；失败的变体连同错话全留档。"""
    result = Result(probe.name, probe.bucket, probe.note, ok=False, winner=None, rows=None)
    for attempt in probe.attempts:
        started = time.monotonic()
        try:
            payload = _timed(invoke, attempt)
        except Exception as exc:  # 探测的语义就是"什么都可能坏"，逐条记录
            result.errors.append(f"{attempt.label}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        result.ok = True
        result.winner = attempt.label
        result.rows = int(payload.get("rows", 0))
        result.columns = list(payload.get("columns", []))
        result.sample = dict(payload.get("sample", {}))
        result.extra = {k: v for k, v in payload.items() if k not in {"rows", "columns", "sample"}}
        result.extra["seconds"] = round(time.monotonic() - started, 2)
        break
    return result


def build_probes() -> list[Probe]:
    probes = [
        Probe(
            name,
            "A-akshare",
            note,
            tuple(Attempt(label, target, kwargs) for label, target, kwargs in attempts),
        )
        for name, note, attempts in AK_PROBES
    ]
    for name, note, api in RELAY_PROBES:
        for relay in RELAYS:
            probes.append(
                Probe(
                    f"{relay}.{name}",
                    f"B-{relay}",
                    f"{note}（{relay}）",
                    (Attempt("默认", api, RELAY_PARAMS[api]),),
                )
            )
    return probes


def write_report(results: Sequence[Result], out: Path, as_of: date) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    summary = out / "summary.md"
    lines = [
        f"# 数据源最小探测 · {as_of.isoformat()}",
        "",
        "探测结果≠准入：进门禁另走黄金样本与逐表审计（04 §五）。",
        "",
        "| 探测项 | 桶 | 通 | 行数 | 用时s | 胜出变体 / 错话 |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.ok:
            tail = f"{r.winner}（{r.extra.get('seconds', '?')}s）"
        else:
            tail = "；".join(r.errors)[:160] or "未执行"
        lines.append(
            f"| {r.name} | {r.bucket} | {'✅' if r.ok else '❌'} | "
            f"{r.rows if r.rows is not None else '—'} | {r.extra.get('seconds', '—')} | {tail} |"
        )
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for r in results:
        (out / f"{r.name}.json").write_text(
            json.dumps(asdict(r), ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, default=None, help="报告目录，默认 <数据根>/reports/source-probe/<今天>"
    )
    parser.add_argument("--only", default=None, help="只跑名字含此串的探测项")
    parser.add_argument("--list", action="store_true", help="列出全部探测项后退出")
    args = parser.parse_args(argv)

    probes = build_probes()
    if args.list:
        for probe in probes:
            print(f"{probe.name}\t{probe.bucket}\t{probe.note}")
        return 0

    picked = [p for p in probes if args.only is None or args.only in p.name]
    if not picked:
        print(f"没有匹配 --only {args.only!r} 的探测项（--list 看全部）", file=sys.stderr)
        return 2

    out = args.out or config.reports_dir() / "source-probe" / date.today().isoformat()
    results: list[Result] = []
    for probe in picked:
        if probe.bucket.startswith("B-"):
            relay = probe.bucket.removeprefix("B-")
            invoke: Invoke = _relay_invoke(relay)
        else:
            invoke = _ak_invoke
        results.append(run_probe(probe, invoke))
        print(("✅" if results[-1].ok else "❌"), probe.name, flush=True)

    summary = write_report(results, out, date.today())
    print(f"\n报告落 {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
