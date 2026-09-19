"""akshare/sina 分钟线适配器 + 采→判→存→查一条链（ADR-0009；01 Step 4 验收 1）。

行形状照 2026-09-18 实测的那次响应：列 `day,open,high,low,close,volume,amount`，**全是字符串**，
时刻在 `day` 里且是右端点标签（5 分钟给 09:35…15:00）。黄金样本进仓要用户批准（05 Q1-③），
所以这里用合成行覆盖映射，真实响应由 `tools/capture_golden.py` 落在数据根。

一条链的测试在本文件末尾：分钟线新的是一个字段（`ts`）加三个 dataset，字段映射、门禁粒度、
落盘主键、查询排序四件事必须同时对，任何一件单独"看起来对"都不算数。
"""

from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from zhixing_quant import config as app_config
from zhixing_quant.domain.bar import BarDraft
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Listing, SecurityMaster
from zhixing_quant.quality import gate_config
from zhixing_quant.quality.engine import GateEngine
from zhixing_quant.sources.akshare import minute
from zhixing_quant.storage import layout, query, write

#: 一根正常的 5 分钟K线，源侧原样（字符串）。数值取 2024-01-02 茅台区间的量级。
ROW: dict[str, Any] = {
    "day": "2024-01-02 09:35:00",
    "open": "1715.00",
    "high": "1718.19",
    "low": "1678.10",
    "close": "1685.01",
    "volume": "32156",
    "amount": "54400825.0",
}
DAY = date(2024, 1, 2)
STAMP = datetime(2024, 1, 2, 9, 35)

CALENDAR = TradingCalendar([DAY, date(2024, 1, 3)])
MASTER = SecurityMaster([Listing("600519", "测试白酒", date(2000, 1, 4))])


def _row(**over: Any) -> dict[str, Any]:
    return {**ROW, **over}


def _drafts(*rows: dict[str, Any], period: str = "5") -> list[BarDraft]:
    return minute.minute_drafts(list(rows) or [_row()], symbol="600519", period=period)


def test_the_stamp_column_feeds_both_date_and_time() -> None:
    """`day` 一列喂两个字段：`trade_date` 与 `ts` 在源那边本来就不是两件事（ADR-0009 决定 2）。

    分开喂才会出现的形状是"日期 1 月 2 日、时刻 1 月 3 日 09:35"——那种行没人能解释，而它
    落进分钟 dataset 之后 R008 与 R009 都判不出来。
    """
    (draft,) = _drafts(_row())
    assert draft.symbol == "600519"
    assert (draft.trade_date, draft.ts) == (DAY, STAMP)
    assert (draft.open, draft.high, draft.low, draft.close) == (1715.0, 1718.19, 1678.1, 1685.01)
    assert (draft.volume, draft.amount) == (32156.0, 54400825.0)


def test_the_source_symbol_is_passed_through_untouched() -> None:
    """代码归一属于契约层（`Bar`/门禁），不在适配器做：日线适配器同一条。"""
    (draft,) = minute.minute_drafts([_row()], symbol="sh600519")
    assert draft.symbol == "sh600519"


def test_no_adjust_factor_is_invented_for_a_minute_bar() -> None:
    """分钟线不落因子：复权 = 分钟价 × 当日日线因子（ADR-0009 决定 4）。

    写 1.0 会让"这只票从没除过权"与"分钟线没有因子这回事"在盘上长成同一行，而 R005 在日线
    上判的就是这两种区别。
    """
    (draft,) = _drafts(_row())
    assert draft.adj_factor is None
    assert draft.is_suspended is False  # 与日线适配器同一条：sina 不给停牌字段


@pytest.mark.parametrize("period", ["5", "30", "60"])
def test_each_period_is_its_own_source(period: str) -> None:
    """源标识按周期分开（04 §三 按源打分）：60 分钟的接口挂了不该扣 5 分钟的健康分。"""
    assert minute.source_for(period) == f"akshare_minute_{period}"
    (draft,) = _drafts(_row(), period=period)
    assert draft.source == f"akshare_minute_{period}"


def test_an_unsupported_period_never_reaches_a_source_name() -> None:
    """周期表只有一份（`layout.SPECS`）：这里不另列一串数字，所以 15 分钟连名字都取不出来。"""
    with pytest.raises(ValueError, match="不支持的分钟周期"):
        minute.source_for("15")


def test_a_date_only_day_leaves_the_bar_without_identity() -> None:
    """源哪天只给日期，适配器不补 00:00：那会凭空造出"当天第一根K线"（`rows.to_datetime`）。"""
    (draft,) = _drafts(_row(day="2024-01-02"))
    assert (draft.trade_date, draft.ts) == (DAY, None)


def test_a_tz_stamped_bar_is_left_without_identity_too() -> None:
    """带时区的时刻不自己换算：换算一次就是一条时间轴整体偏移，而 R009 判不出来。"""
    (draft,) = _drafts(_row(day="2024-01-02T09:35:00+08:00"))
    assert draft.ts is None


def test_rows_are_left_in_the_order_the_source_gave_them() -> None:
    """适配器不排序：在这里排一次，R009 就永远看不到源的乱序了。"""
    late = _row(day="2024-01-02 15:00:00")
    assert [d.ts for d in _drafts(late, _row())] == [datetime(2024, 1, 2, 15, 0), STAMP]


def test_a_renamed_column_becomes_a_missing_field_not_an_exception() -> None:
    """列名漂移 → None → R010 整批 FATAL。在这里抛 KeyError 会把采集炸成"半批已入库"。"""
    (draft,) = minute.minute_drafts(
        [{"day": "2024-01-02 09:35:00", "px": "1685.01"}], symbol="600519"
    )
    assert draft.close is None
    assert draft.ts == STAMP


# --- 采 → 判 → 存 → 查：验收 1 的那一条链 -----------------------------------------


def _gate() -> GateEngine:
    return GateEngine(gate_config.load(app_config.gate_config_file()), MASTER, CALENDAR)


@pytest.mark.parametrize("period", ["5", "30", "60"])
def test_a_minute_batch_can_be_judged_stored_and_queried(period: str, tmp_path: Path) -> None:
    """三种周期各自走完整条链：映射、粒度判定、按 `ts` 合并、按时刻排序。

    两根故意倒着交，于是这一条同时钉住三件事：R009 拒掉后到的那根（分钟批真的跑分钟规则，
    日线批根本不跑它）、合并键是收盘时刻不是日期（09:30 那根不会因为"同日已有 09:35"被并掉）、
    以及分钟 dataset 与日线目录互不侵犯。
    """
    drafts = _drafts(_row(), _row(day="2024-01-02 09:30:00", close="1700.00"), period=period)
    outcome = _gate().run(drafts)
    assert [tuple(v.rule_id for v in q.violations) for q in outcome.quarantined] == [("R009",)]
    assert [b.ts for b in outcome.clean_zone] == [STAMP]

    dataset = layout.minute_dataset(period)
    report = write.store_bars(outcome.clean_zone, dataset=dataset, root=tmp_path)
    assert (report.partitions, report.added, report.rewritten) == (1, 1, 1)
    assert layout.partition_path("600519", DAY.year, dataset=dataset, root=tmp_path).is_file()

    back = query.read_bars("600519", DAY, DAY, dataset=dataset, root=tmp_path)
    assert [(b.ts, b.close, b.adj_factor) for b in back] == [(STAMP, 1685.01, None)]
    assert back[0].source == f"akshare_minute_{period}"
    assert query.read_bars("600519", DAY, DAY, root=tmp_path) == []


def test_a_batch_without_times_is_caught_before_it_reaches_a_minute_file(
    tmp_path: Path,
) -> None:
    """源只给日期时，噪声要响在落盘前：一天 48 根压成一根是这里最坏的失效。

    前两站都不响——没有 `ts` 的批被引擎认成日线（粒度由数据说），R010 的必需字段里也确实没有
    `ts`，于是两行都放行。第三站存储层按 dataset 的主键拒收。这条链记下来，是因为"哪一站该响"
    在这件事上并不是显然的。
    """
    drafts = _drafts(_row(day="2024-01-02"), _row(day="2024-01-03"))
    outcome = _gate().run(drafts)
    assert [b.ts for b in outcome.clean_zone] == [None, None]
    with pytest.raises(ValueError, match="主键列为空"):
        write.store_bars(outcome.clean_zone, dataset=layout.MINUTE_5, root=tmp_path)
    assert list(tmp_path.rglob("*.parquet")) == []
