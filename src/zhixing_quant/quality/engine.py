"""门禁规则引擎（04 §一；ADR-0002 方案 C 的落地）。

引擎只做三件事：装配事实 → 按配置表跑谓词 → 按级别分流。它不认识任何一条具体规则，
所以 04 §二 加规则时这里不动（Step 1 验收 3）。诚实边界写在 `facts.RowFacts` 的文档里：
新规则若需要引擎没装配的**事实**，得先扩事实——改的是事实，不是判定。

顺序上有两条不能反过来：

1. 批级（FATAL）先跑，命中就整批拒收、逐条规则不再执行。逐条规则的结果在一批"日期
   都不对/字段都不全"的数据上没有意义，报出来只会把日报刷满噪声。
2. 分流后一定要过一遍 `Bar`。引擎放行而干净区契约拒收，说明两边判据漂开了——那是
   门禁自己的漏洞，抛 `GateInconsistency` 让它立刻响，而不是悄悄把那条塞进隔离区
   假装"门禁拦下了"。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date

from zhixing_quant.domain.bar import Bar, BarDraft
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import SecurityMaster, SecurityState
from zhixing_quant.quality.facts import BatchFacts, RowFacts, Violation
from zhixing_quant.quality.gate_config import GateConfig

LEVELS_ORDER = ("fatal", "reject", "warn")


class GateInconsistency(RuntimeError):
    """门禁放行但 Bar 拒收：两条判据漂开了，属于门禁的 bug。"""


@dataclass(frozen=True)
class QuarantinedRow:
    """隔离区条目：原始数据不删不改（ADR-0002 第 2 条），附违规规则与原因。"""

    draft: BarDraft
    violations: tuple[Violation, ...]

    @property
    def rule_ids(self) -> tuple[str, ...]:
        return tuple(v.rule_id for v in self.violations)


@dataclass(frozen=True)
class GateOutcome:
    source: str
    total: int
    accepted: tuple[Bar, ...]
    quarantined: tuple[QuarantinedRow, ...]
    warned: tuple[QuarantinedRow, ...]
    fatal: tuple[Violation, ...]

    @property
    def rejected_count(self) -> int:
        return len(self.quarantined)

    @property
    def reject_rate(self) -> float:
        return self.rejected_count / self.total if self.total else 0.0

    @property
    def warn_rate(self) -> float:
        return len(self.warned) / self.total if self.total else 0.0

    @property
    def has_fatal(self) -> bool:
        return bool(self.fatal)

    @property
    def clean_zone(self) -> tuple[Bar, ...]:
        """策略层唯一入口（04 §一 关键不变量）：FATAL 时为空，整批都没进干净区。"""
        return () if self.has_fatal else self.accepted


class GateEngine:
    def __init__(
        self,
        config: GateConfig,
        master: SecurityMaster | None = None,
        calendar: TradingCalendar | None = None,
    ) -> None:
        self._config = config
        self._master = master
        self._calendar = calendar
        # 启用集在装载时就定了，逐行再筛一遍是纯浪费：五万行 × 十条规则 = 三十五万次
        # 元组重建。配置是 frozen 的，缓存不会漂。
        self._row_rules = config.enabled_rules("row")
        self._batch_rules = config.enabled_rules("batch")

    # --- 事实装配 --------------------------------------------------------------

    def _row_facts(self, drafts: Sequence[BarDraft]) -> list[RowFacts]:
        seen: set[tuple[str, date | None]] = set()
        last_date: dict[str, date] = {}
        out: list[RowFacts] = []
        for draft in drafts:
            already = (draft.code, draft.trade_date) in seen
            seen.add((draft.code, draft.trade_date))
            previous = last_date.get(draft.code)
            out_of_order = (
                previous is not None
                and draft.trade_date is not None
                and draft.trade_date < previous
            )
            if draft.trade_date is not None:
                last_date[draft.code] = draft.trade_date
            out.append(
                RowFacts(
                    draft=draft,
                    state=self._state_on(draft),
                    prev_close=None,
                    prev_factor=None,
                    already_present=already,
                    out_of_order=out_of_order,
                    master=self._master,
                    calendar=self._calendar,
                )
            )
        return _attach_previous_by_date(out)

    def _state_on(self, draft: BarDraft) -> SecurityState | None:
        """主数据没有该股时返回 None 而不是抛：让规则自己判"判不了该拒还是该放行"。"""
        if self._master is None or draft.trade_date is None:
            return None
        try:
            return self._master.state_on(draft.code, draft.trade_date)
        except KeyError:
            return None

    # --- 跑批 ------------------------------------------------------------------

    def run(self, drafts: Sequence[BarDraft]) -> GateOutcome:
        sources = {d.source for d in drafts}
        if len(sources) > 1:
            raise GateInconsistency(
                f"一批只能来自一个数据源，收到 {sorted(sources)}："
                "混批会把 A 源的脏算进 B 源的健康分（04 §三 按源打分）"
            )
        source = next(iter(sources)) if sources else ""
        batch = BatchFacts(drafts=tuple(drafts), master=self._master, calendar=self._calendar)
        fatal: list[Violation] = []
        for spec in self._batch_rules:
            reasons = _as_reasons(spec.predicate(batch, spec.params))
            fatal += [Violation(spec.id, spec.level, why) for why in reasons]
        if fatal:
            return GateOutcome(source, len(drafts), (), (), (), tuple(fatal))

        accepted: list[Bar] = []
        quarantined: list[QuarantinedRow] = []
        warned: list[QuarantinedRow] = []
        for facts in self._row_facts(drafts):
            rejected: list[Violation] = []
            warnings: list[Violation] = []
            for spec in self._row_rules:
                reason = spec.predicate(facts, spec.params)
                if not reason:
                    continue
                violation = Violation(spec.id, spec.level, str(reason))
                (warnings if spec.level == "warn" else rejected).append(violation)
            if rejected:
                quarantined.append(QuarantinedRow(facts.draft, tuple(rejected)))
                continue
            # 通过行必须先变成干净区契约再进桶：warned 与 accepted 指向同一条数据，
            # 各存一份迟早漂成两份事实。
            bar = _to_clean_bar(facts.draft, self._config.enabled_ids)
            accepted.append(bar)
            if warnings:
                warned.append(QuarantinedRow(facts.draft, tuple(warnings)))
        return GateOutcome(
            source=source,
            total=len(drafts),
            accepted=tuple(accepted),
            quarantined=tuple(quarantined),
            warned=tuple(warned),
            fatal=(),
        )


def _as_reasons(result: object) -> Sequence[str]:
    if result is None:
        return ()
    if isinstance(result, str):
        return (result,) if result else ()
    if isinstance(result, Sequence):
        return tuple(str(x) for x in result if x)
    return (str(result),)


def _attach_previous_by_date(facts: list[RowFacts]) -> list[RowFacts]:
    """按票、按日期升序回填"上一行"的收盘与因子，返回顺序仍保持输入顺序。

    缺交易日时"上一行"是更早的一天，偏差本就该更大——R007 报出来是对的，不为它开
    "必须是相邻交易日"的分支：那会让断档数据看起来干净。
    """
    order = sorted(
        range(len(facts)),
        key=lambda i: (facts[i].draft.code, facts[i].draft.trade_date or date.min),
    )
    previous: dict[str, RowFacts] = {}
    patched = list(facts)
    for i in order:
        row = facts[i]
        last = previous.get(row.draft.code)
        if last is not None:
            row = replace(row, prev_close=last.draft.close, prev_factor=last.draft.adj_factor)
        patched[i] = row
        previous[row.draft.code] = row
    return patched


def _to_clean_bar(draft: BarDraft, enabled_ids: Sequence[str]) -> Bar:
    try:
        return Bar.from_draft(draft)
    except ValueError as exc:  # pydantic ValidationError 是 ValueError 的子类
        raise GateInconsistency(
            f"门禁放行但干净区拒收（已启用 {','.join(enabled_ids)}）：{draft.code} "
            f"@{draft.trade_date} → {exc}"
        ) from exc
