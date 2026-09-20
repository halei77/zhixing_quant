"""每日质量日报：把门禁结果渲染成 04 §四 那份 Markdown，并归档到数据根。

`report.py` 算分，本模块**说人话**。分开是因为一次改动只该碰一件事：调分数权重要动
`report.py`，改日报版式（多一列、抽样条数）只动这里。

两份落盘，一份转发：

- `reports/YYYY-MM-DD.md`：给人读的，格式照 04 §四。
- `reports/scores.csv`：给机器读的分数流水。04 §四 要求"近 7 日曲线"和"等级变化"，那就
  得有昨天的分数——从 Markdown 里回读等于把日报版式变成解析对象，改一个字就断。
- 标准输出：由调用方（每日任务）打印同一份 Markdown，17:00 定时任务的日志里就能看到内容，
  不必再去数据根翻文件。本模块不碰 stdout，日报的测试因此不需要截获输出。

`append_scores` 按 (日期, 源) 覆盖而不是追加：重跑是常态（网络抖动、口径修完重来），
追加会让同一天在曲线里出现两次，而看起来只是"那天分数波动大"。
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from zhixing_quant.quality.engine import GateOutcome, QuarantinedRow
from zhixing_quant.quality.report import DataQualityReport, HealthGrade, SourceMetrics
from zhixing_quant.storage import layout
from zhixing_quant.storage.quarantine import QuarantineReport
from zhixing_quant.storage.write import WriteReport

#: 04 §四 "明细 Top10"——**每个源**十条，不是全市场十条：后接入的源不能因为前面的源
#: 报错多就被挤出日报。全量明细在隔离区，按运行日一个 Parquet 文件（ADR-0008）。
DETAIL_TOP = 10
#: 隔离区抽样条数。抽样而不是全量：一天真错 3000 行时，全量清单会把手要看的那几条埋掉。
SAMPLE_ROWS = 5

SPARKLINE = "▁▂▃▄▅▆▇█"
SCORE_COLUMNS = ("day", "source", "total", "rejected", "warned", "fatal", "score", "grade")
#: 某源的一天分数。等级一起存：从分数回推等级要复制 `report.py` 的分档阈值，那两处一改就分家。
Trend = tuple[tuple[date, float, HealthGrade], ...]


@dataclass(frozen=True)
class RuleTally:
    """一条规则今天在一个源上判了多少次。04 §四 的"明细"就是这个聚合。"""

    source: str
    rule_id: str
    level: str
    count: int
    sample: str


def tally(outcomes: Sequence[GateOutcome]) -> tuple[RuleTally, ...]:
    """按 (源, 规则, 级别) 聚合，每个源各自取命中最多的 `DETAIL_TOP` 条，多的在前。

    级别也要进键：同一条规则配成 warn 和配成 reject 是两件事，混在一起数会把"放行但计数"
    的告警算进拒收明细。
    """
    counts: Counter[tuple[str, str, str]] = Counter()
    first: dict[tuple[str, str, str], str] = {}
    for outcome in outcomes:
        violations = [
            *(v for v in outcome.fatal),
            *(v for row in (*outcome.quarantined, *outcome.warned) for v in row.violations),
        ]
        for violation in violations:
            key = (outcome.source, violation.rule_id, violation.level)
            counts[key] += 1
            first.setdefault(key, violation.reason)
    ordered = sorted(counts.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1:]))
    out: list[RuleTally] = []
    per_source: Counter[str] = Counter()
    for (source, rule_id, level), count in ordered:
        if per_source[source] >= DETAIL_TOP:
            continue
        per_source[source] += 1
        out.append(RuleTally(source, rule_id, level, count, first[(source, rule_id, level)]))
    return tuple(out)


def sparkline(scores: Sequence[float]) -> str:
    """分数 → 文本曲线（04 §四）。

    按 0..100 全量程归一，不按窗口内 min-max：后者会把"某天全员 99 分"拉成一根剧烈震荡的
    假曲线，而日报上最不该出现的就是一种误导。
    """
    out = []
    for value in scores:
        slot = round(min(max(value, 0.0), 100.0) / 100.0 * (len(SPARKLINE) - 1))
        out.append(SPARKLINE[slot])
    return "".join(out)


def render(
    report: DataQualityReport,
    outcomes: Sequence[GateOutcome],
    trends: Mapping[str, Trend],
    landed: WriteReport,
    quarantined: QuarantineReport,
) -> str:
    """日报正文。`trends` 按源给近 N 日的 (日期, 分数, 等级)，旧在前；没有历史就传空。

    `landed` 与 `quarantined` 是这次运行留下的两本账：门禁判成多少行说的是"源给的数据能不能要"，
    落盘多少行说的是"明天有没有数据可用"，拒收条目落了几条说的是下一节的抽样之外还有没有东西
    可查。三个数各报各的，才不会在"判成八千行、磁盘写满"那天报平安。
    """
    lines = [
        f"# 数据质量日报 {report.day.isoformat()}",
        "",
        f"- 行数 {report.total_rows:,}，源 {len(report.metrics)} 个，"
        f"降级或停用 {len(report.blocked)} 个",
        _landed_line(landed),
        _quarantine_line(report.day, quarantined),
        "",
        "## 各源",
        "",
        "| 源 | 总条数 | REJECT | WARN | FATAL | 健康分 | 等级 | 处置 |",
        "|---|---:|---:|---:|:-:|---:|:-:|---|",
        *[_source_row(metrics) for metrics in report.metrics],
        "",
        "## 违规明细 Top10",
        "",
    ]
    tallies = tally(outcomes)
    if tallies:
        lines += ["| 源 | 规则 | 级别 | 条数 | 首例原因 |", "|---|---|---|---:|---|"]
        lines += [
            f"| {t.source} | {t.rule_id} | {t.level} | {t.count} | {t.sample} |" for t in tallies
        ]
    else:
        lines.append("（今天没有任何规则命中）")
    lines += ["", "## 隔离区抽样", ""]
    samples = [row for outcome in outcomes for row in outcome.quarantined]
    # 说"报告日"而不是"今天"：抽样来自裁过的那张表，而首行那句落盘账来自整批——两日批里
    # 上一日被拒收时，两个数本来就不同时为 0，含糊的说法会让其中一句看起来在撒谎。
    lines += [f"- {_sample_line(row)}" for row in samples[:SAMPLE_ROWS]] or [
        "（报告日没有拒收行，无可抽样；这次运行的全量见首行那句落盘账）"
    ]
    for metrics in report.metrics:
        lines += ["", *_trend_lines(metrics, trends.get(metrics.source, ()))]
    return "\n".join(lines) + "\n"


def _landed_line(landed: WriteReport) -> str:
    """落盘那行。三种情形要分开说，因为它们是三件不同的事实：

    没写任何东西（今天没有一行通过门禁）、写了但一个文件都没改（重跑，幂等的直接证据）、
    真的新增了行。把第二种写成"+0 行"就等于把验收 3 的观测点藏进一个看起来像失败的数字里。
    """
    if not landed.partitions:
        return "- 进干净区：0 行——今天没有通过门禁的数据"
    if not landed.added and not landed.repaired:
        return (
            f"- 进干净区：+0 行，{landed.partitions} 个分区文件在盘上已是最新"
            "（重跑幂等，未改动任何文件）"
        )
    return (
        f"- 进干净区：新增 {landed.added:,} 行、改写 {landed.repaired:,} 行，"
        f"重写 {landed.rewritten:,}/{landed.partitions:,} 个分区文件"
    )


def _quarantine_line(day: date, quarantined: QuarantineReport) -> str:
    """隔离区落盘那行：把 04 §四 的"全量在隔离区可查"落到一个具体文件上。

    "这次运行"与"那一天"是两本账（独立审计 O4）：一天可以跑好几趟，而隔离区按运行日落成一个
    文件。只报前者却写成"今天没有拒收条目"，指着的又是那个装着前一跑 2 行的文件——读者不必推理
    就能抓到日报自相矛盾。所以这一行无论哪种情形都带上"当日累计"那个数，它来自 `total`，也就是
    落盘之后那个文件的真实行数。

    三种情形分别是三件事：这次没拒收任何东西、拒收了但盘上已经有（重跑幂等）、真的新记了几条。
    把第三种写成"见隔离区"是要人自己去猜有没有写进去，而第二种不写清楚就等于宣称"重跑会重复记"。
    """
    path = layout.quarantine_path(day)
    total = f"当日累计 {quarantined.total:,} 条"
    if not quarantined.entries:
        return f"- 隔离区：本次运行没有拒收条目（{total}，全量可查于 `{path}`）"
    if not quarantined.recorded:
        return (
            f"- 隔离区：{quarantined.entries:,} 条重跑前就已在盘上"
            f"（幂等，未改动文件；{total}，全量见 `{path}`）"
        )
    return (
        f"- 隔离区：新记 {quarantined.recorded:,}/{quarantined.entries:,} 条拒收条目，"
        f"{total}，全量可查于 `{path}`"
    )


def _trend_lines(metrics: SourceMetrics, trend: Trend) -> list[str]:
    """一个源的趋势段：曲线 + 上一次运行的分数与等级（04 §四 的"等级变化"）。

    对齐用"流水里最近一条"而不是"昨天"：周末、停牌、采集失败都会让最近一条不是昨天，
    写成"昨日"就是在报一个不存在的事实，所以把那个日期本身打出来。
    """
    if not trend:
        return []
    day, score, grade = trend[-1]
    return [
        f"## 趋势 · {metrics.source}",
        "",
        f"- 近 {len(trend)} 日：{sparkline([s for _, s, _ in trend])}",
        f"- 日期：{' '.join(d.isoformat()[5:] for d, _, _ in trend)}",
        f"- {day.isoformat()} {score:.1f}（{grade.value}）"
        f" → 今天 {metrics.score:.1f}（{metrics.grade.value}）",
    ]


def _source_row(metrics: SourceMetrics) -> str:
    return (
        f"| {metrics.source} | {metrics.total:,} | {metrics.rejected} | {metrics.warned} "
        f"| {'是' if metrics.fatal else '否'} | {metrics.score:.1f} | {metrics.grade.value} "
        f"| {metrics.action} |"
    )


def _sample_line(row: QuarantinedRow) -> str:
    """一行隔离条目 → 人读的一句。

    原始值必须一起打出来：原因文案说"high 低于 open/close 较高者"，人还得回去查那天的
    四个数才能判断，而抽样本来就是给人一眼定性的。
    """
    fields = ("open", "high", "low", "close", "volume", "amount", "adj_factor")
    values = " ".join(f"{name}={getattr(row.draft, name)}" for name in fields)
    reasons = "；".join(v.reason for v in row.violations)
    return f"{row.draft.code} {row.draft.trade_date} {'/'.join(row.rule_ids)}：{reasons}｜{values}"


def archive(markdown: str, day: date, directory: Path) -> Path:
    """落 `reports/YYYY-MM-DD.md`（04 §四 的归档路径）。同一天重跑就是覆盖。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{day.isoformat()}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def append_scores(report: DataQualityReport, path: Path) -> None:
    """分数流水：本次报告涉及的 (日期, 源) 一律覆盖，其余行原样留下。"""
    today = report.day.isoformat()
    sources = {metrics.source for metrics in report.metrics}
    rows = [
        row for row in read_scores(path) if not (row["day"] == today and row["source"] in sources)
    ]
    rows += [
        {
            "day": today,
            "source": metrics.source,
            "total": str(metrics.total),
            "rejected": str(metrics.rejected),
            "warned": str(metrics.warned),
            "fatal": str(metrics.fatal),
            "score": f"{metrics.score:.2f}",
            "grade": metrics.grade.value,
        }
        for metrics in report.metrics
    ]
    rows.sort(key=lambda row: (row["day"], row["source"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(SCORE_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)


def read_scores(path: Path) -> list[dict[str, str]]:
    """整份流水。文件不在就是空——第一天没有历史，不是错误。"""
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def history_for(path: Path, source: str, day: date, *, days: int = 7) -> Trend:
    """该源在 `day` 之前（不含当天）最近 `days` 天的 (日期, 分数, 等级)，旧在前。

    不含当天：曲线要比的是"和过去比"，把今天也画进去等于让最后一天同时当基线和本值。
    等级从流水里读，不按分数回推：回推要把 `report.py` 的分档阈值抄第二份。
    """
    cutoff = day.isoformat()
    rows = [
        (date.fromisoformat(row["day"]), float(row["score"]), HealthGrade(row["grade"]))
        for row in read_scores(path)
        if row["source"] == source and row["day"] != cutoff
    ]
    return tuple(sorted(rows, key=lambda row: row[:2])[-days:])
