"""stk_limit 锚点的两大修复（#68，子代理诊断实锤）的直测。

1. **档位带日期**：`limit_of` 必须收到**这一行的交易日**——按"今天"查会把当前的 ST 帽
   前套整段历史（#65 首跑 971 只拒收、其中错档 197 只全在当前 ST 名单）。
2. **除权按因子换算参考价**：真除权 0.66~0.83 的折算比对"两界反推 + 0.8~1.2 邻域"一律是
   假超阈（683 只 / 12.9%）；给了 `ref_ratio_of` 必须先换算再判，缺因子才退回旧逃生门。
"""

from datetime import date
from types import SimpleNamespace
from typing import Any

from zhixing_quant.sources.jobs import relay_cli

DAY = date(2026, 5, 13)
PREV = date(2026, 5, 12)
ROW_DAY = DAY


def row(up: float, down: float, *, symbol: str = "600100", when: date = DAY) -> SimpleNamespace:
    return SimpleNamespace(symbol=symbol, trade_date=when, up_limit=up, down_limit=down)


def prev_of(_symbol: str, _day: date) -> float | None:
    return 10.0


def no_same(_symbol: str, _day: date) -> float | None:
    return None


def flat(_symbol: str, _when: date) -> float:
    return 10.0


def run(
    rows: list[SimpleNamespace],
    *,
    limit_of: relay_cli.LimitPctFn = flat,
    ref_ratio_of: relay_cli.RefRatioFn | None = None,
) -> tuple[list[str], int, list[Any]]:
    relay_cli._exdiv_notes.clear()
    return relay_cli._anchor_stk_limit(rows, prev_of, no_same, limit_of, ref_ratio_of)


# ── 1. 档位带日期 ────────────────────────────────────────────────────────────


def test_limit_of_receives_the_rows_trade_date_not_today() -> None:
    seen: list[tuple[str, date]] = []

    def spy(symbol: str, when: date) -> float:
        seen.append((symbol, when))
        return 5.0 if when >= date(2026, 9, 24) else 10.0

    old = row(11.0, 9.0, when=ROW_DAY)  # ±10%：历史日（非ST）合法
    new = row(10.5, 9.5, when=date(2026, 9, 25))  # ±5%：快照日后 ST 帽内合法
    problems, _unanchored, anchored = run([old, new], limit_of=spy)
    assert [when for _s, when in seen] == [ROW_DAY, date(2026, 9, 25)], "档位查询收的是行日期"
    assert problems == [] and len(anchored) == 2


def test_current_st_hat_does_not_reject_its_own_history() -> None:
    """今天 ST 的票，两年前的 ±10% 行照过——错档 197 只的病根形状。"""

    def st_from_snapshot(_symbol: str, when: date) -> float:
        return 5.0 if when >= date(2026, 9, 24) else 10.0

    problems, _un, anchored = run(
        [row(11.0, 9.0, when=date(2024, 7, 1))], limit_of=st_from_snapshot
    )
    assert problems == [] and len(anchored) == 1


# ── 2. 除权换算 ─────────────────────────────────────────────────────────────


def test_exdiv_day_passes_via_factor_conversion() -> None:
    """prev 10 → 除权参考价 7（ratio 0.7）：板价 7.7/6.3 对昨收"超阈"、对参考价逐边 +10/-10。"""
    problems, _un, anchored = run([row(7.7, 6.3)], ref_ratio_of=lambda _s, _d: 0.7)
    assert problems == [] and len(anchored) == 1


def test_exdiv_conversion_note_carries_the_ratio() -> None:
    run([row(7.7, 6.3)], ref_ratio_of=lambda _s, _d: 0.7)
    notes = relay_cli.take_exdiv_notes()
    assert len(notes) == 1 and "除权换算因子" in notes[0] and "0.7000" in notes[0]


def test_no_exdiv_ratio_one_still_rejects_real_breach() -> None:
    """因子说"没除权"（ratio=1.0）而板价真超阈：换算救不了它，照拒。"""
    problems, _un, anchored = run([row(12.5, 9.9)], ref_ratio_of=lambda _s, _d: 1.0)
    assert len(anchored) == 1 and any("涨停" in p for p in problems)  # 跌停边没超，只记涨停


def test_missing_factor_falls_back_to_two_sided_self_check() -> None:
    """因子缺档（ratio None）：温和除权 0.85 仍走旧两界反推逃生门——旧行为不丢。"""
    problems, _un, anchored = run([row(9.35, 7.65)], ref_ratio_of=None)
    assert problems == [] and len(anchored) == 1
    assert "因子查不到" in relay_cli.take_exdiv_notes()[0]


def test_hard_breach_rejected_under_every_path() -> None:
    """+25% 的涨停价：换算(0.7 对不上)、两界(反推不自洽)都救不了——三头都记超阈。"""
    problems, _un, _anchored = run([row(12.5, 9.9)], ref_ratio_of=lambda _s, _d: 0.7)
    assert len(problems) == 1 and "涨停" in problems[0]


def test_unanchored_when_no_prev_close() -> None:
    relay_cli._exdiv_notes.clear()
    problems, unanchored, _anchored = relay_cli._anchor_stk_limit(
        [row(11.0, 9.0)], lambda _s, _d: None, no_same, flat, None
    )
    assert unanchored == 1 and problems == []
