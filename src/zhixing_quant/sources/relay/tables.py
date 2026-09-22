"""转接源参考表的行模型、解析与表级校验（ADR-0015 决定 1、3）。

每张表三样东西，缺一不接：行模型（NamedTuple，列名与 `storage.tables.TableSpec` 对齐）、
`tushare fields/items → 行` 的解析器（`zip(fields, row)`，字段顺序不固定是这族源的已知
陷阱）、值域校验（不合法当场响，不产出"看着像数据"的行）。

解析器的输入就是快照里的形状（fields + items 的裸列表），CI 里离线重放用的同一形状。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any, NamedTuple

from zhixing_quant.domain.symbol import normalize_code


class StkLimitRow(NamedTuple):
    """`stk_limit` 一行：某票某日的涨跌停价（不复权绝对价，ADR-0015 背景第二条）。"""

    source: str
    symbol: str
    trade_date: date
    up_limit: float
    down_limit: float


def _field(row: Sequence[object], fields: Sequence[str], name: str) -> str:
    """按名取列。字段顺序随参数集漂是这族源的已知陷阱，按下标取等于埋雷。"""
    try:
        index = fields.index(name)
    except ValueError:
        raise ValueError(f"响应缺字段 {name!r}（fields={'、'.join(fields)}）") from None
    return str(row[index])


def _as_date(text: str) -> date:
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"trade_date {text!r} 不是 YYYYMMDD") from exc


def parse_stk_limit(
    source: str, fields: Sequence[str], items: Sequence[Sequence[object]]
) -> list[StkLimitRow]:
    """一页 stk_limit → 校验过的行。值域不合法整批响：涨跌停价错一个就不是"少一行"的事，
    是这一天整页不可信——锚点对账（装载器）会再拦一层，这里只挡形状级错误。
    """
    rows: list[StkLimitRow] = []
    seen: set[tuple[str, date]] = set()
    for raw in items:
        symbol = normalize_code(_field(raw, fields, "ts_code"))
        trade_date = _as_date(_field(raw, fields, "trade_date"))
        up = float(_field(raw, fields, "up_limit"))
        down = float(_field(raw, fields, "down_limit"))
        if not up > down > 0:
            raise ValueError(f"{symbol}@{trade_date} 涨跌停价不成立：up={up} down={down}")
        key = (symbol, trade_date)
        if key in seen:
            raise ValueError(f"{symbol}@{trade_date} 在同一页里出现两次（limit/offset 分页重叠）")
        seen.add(key)
        rows.append(StkLimitRow(source, symbol, trade_date, up, down))
    return rows


class DailyBasicRow(NamedTuple):
    """`daily_basic` 一行：某票某日的每日估值指标（不复权口径，Qoute §8 陷阱清单的
    null 纪律在这里生效——源给 null 的字段保持 None，**禁止 0 填充**参与任何计算）。"""

    source: str
    symbol: str
    trade_date: date
    close: float
    turnover_rate: float | None
    turnover_rate_f: float | None
    volume_ratio: float | None
    pe: float | None
    pe_ttm: float | None
    pb: float | None
    ps: float | None
    ps_ttm: float | None
    dv_ratio: float | None
    dv_ttm: float | None
    total_share: float | None
    float_share: float | None
    free_share: float | None
    total_mv: float | None
    circ_mv: float | None


def _opt(row: Sequence[object], fields: Sequence[str], name: str) -> float | None:
    """可空数值列：源给 null（字符串 'None' 或空）就保持 None——0 填充会把"没这数"
    洗成"这数为 0"，银行/保险的估值科目大面积是 null，那是它们的常态不是异常。"""
    text = _field(row, fields, name)
    if text in ("", "None", "nan", "NULL", "null"):
        return None
    value = float(text)
    if value != value:  # NaN
        return None
    return value


def parse_daily_basic(
    source: str, fields: Sequence[str], items: Sequence[Sequence[object]]
) -> list[DailyBasicRow]:
    rows: list[DailyBasicRow] = []
    seen: set[tuple[str, date]] = set()
    for raw in items:
        symbol = normalize_code(_field(raw, fields, "ts_code"))
        trade_date = _as_date(_field(raw, fields, "trade_date"))
        # close 为 null 的行（停牌日的估值行）没有信息量，跳行不崩——崩会把整天的
        # 5553 行都拖死；close 若有值但 ≤0 才是真错。
        raw_close = _field(raw, fields, "close")
        if raw_close in ("", "None", "nan", "NULL", "null"):
            continue
        close = float(raw_close)
        if close <= 0:
            raise ValueError(f"{symbol}@{trade_date} 收盘价 {close} 不成立")
        key = (symbol, trade_date)
        if key in seen:
            raise ValueError(f"{symbol}@{trade_date} 在同一页里出现两次（分页重叠）")
        seen.add(key)
        rows.append(
            DailyBasicRow(
                source,
                symbol,
                trade_date,
                close,
                _opt(raw, fields, "turnover_rate"),
                _opt(raw, fields, "turnover_rate_f"),
                _opt(raw, fields, "volume_ratio"),
                _opt(raw, fields, "pe"),
                _opt(raw, fields, "pe_ttm"),
                _opt(raw, fields, "pb"),
                _opt(raw, fields, "ps"),
                _opt(raw, fields, "ps_ttm"),
                _opt(raw, fields, "dv_ratio"),
                _opt(raw, fields, "dv_ttm"),
                _opt(raw, fields, "total_share"),
                _opt(raw, fields, "float_share"),
                _opt(raw, fields, "free_share"),
                _opt(raw, fields, "total_mv"),
                _opt(raw, fields, "circ_mv"),
            )
        )
    return rows


#: 表名 → 解析器。zx-relay 按名取；新表在这里登记才算"可接"（ADR-0015 后果第一条）。
#: 类型显式给全：两张表的行类型不同，不注解会被 mypy 并成 object。
class ForecastRow(NamedTuple):
    """`forecast` 一行：一次业绩预告（08"基本面未受损"排除项的原料）。

    主键是 (ann_date, end_date)：同一次预告的修正以不同 ann_date 出现，各自留行
    （update_flag 只是源的标注，不构成行身份——修正史本身就是"点时可见性"的证据）。
    """

    source: str
    symbol: str
    ann_date: date
    end_date: date
    type: str
    p_change_min: float | None
    p_change_max: float | None
    net_profit_min: float | None
    net_profit_max: float | None
    summary: str


#: 类型枚举以源实测为准（2026-09-22 真跑被 '增亏' 教育过一次——解析器拒绝而非吞下，
#: 枚举因此是长出来的不是拍的）。
_FORECAST_TYPES = {
    "预增",
    "预减",
    "扭亏",
    "首亏",
    "续亏",
    "续盈",
    "略增",
    "略减",
    "增亏",
    "减亏",
    "不确定",
}


def parse_forecast(
    source: str, fields: Sequence[str], items: Sequence[Sequence[object]]
) -> list[ForecastRow]:
    rows: list[ForecastRow] = []
    seen: set[tuple[date, date]] = set()
    for raw in items:
        symbol = normalize_code(_field(raw, fields, "ts_code"))
        ann_date = _as_date(_field(raw, fields, "ann_date"))
        end_date = _as_date(_field(raw, fields, "end_date"))
        type_ = _field(raw, fields, "type")
        if type_ not in _FORECAST_TYPES:
            raise ValueError(
                f"{symbol}@{ann_date} 预告类型 {type_!r} 不在已知枚举里：先扩枚举再入库"
            )
        p_min = _opt(raw, fields, "p_change_min")
        p_max = _opt(raw, fields, "p_change_max")
        if p_min is not None and p_max is not None and p_min > p_max:
            raise ValueError(f"{symbol}@{ann_date} 预告区间颠倒：min {p_min} > max {p_max}")
        n_min = _opt(raw, fields, "net_profit_min")
        n_max = _opt(raw, fields, "net_profit_max")
        if n_min is not None and n_max is not None and n_min > n_max:
            raise ValueError(f"{symbol}@{ann_date} 净利区间颠倒：min {n_min} > max {n_max}")
        key = (ann_date, end_date)
        if key in seen:
            raise ValueError(f"{symbol}@{ann_date} 同一页里出现两次（分页重叠）")
        seen.add(key)
        rows.append(
            ForecastRow(
                source,
                symbol,
                ann_date,
                end_date,
                type_,
                p_min,
                p_max,
                n_min,
                n_max,
                _field(raw, fields, "summary")[:500],
            )
        )
    return rows


class FinaAuditRow(NamedTuple):
    """`fina_audit` 一行：年报审计意见（08"基本面未受损"的硬否决项原料）。

    audit_result 是自由文本（"标准无保留意见"/"保留意见"/…），不在解析器里枚举——
    "非标 = 不是'标准无保留意见'"这个判定留给消费方按自己的口径做，这里只验字段在。
    """

    source: str
    symbol: str
    ann_date: date
    end_date: date
    audit_result: str
    audit_fees: float | None
    audit_agency: str
    audit_sign: str


def parse_fina_audit(
    source: str, fields: Sequence[str], items: Sequence[Sequence[object]]
) -> list[FinaAuditRow]:
    rows: list[FinaAuditRow] = []
    seen: set[tuple[date, date]] = set()
    for raw in items:
        symbol = normalize_code(_field(raw, fields, "ts_code"))
        ann_date = _as_date(_field(raw, fields, "ann_date"))
        end_date = _as_date(_field(raw, fields, "end_date"))
        result = _field(raw, fields, "audit_result")
        if not result:
            raise ValueError(f"{symbol}@{ann_date} 审计意见为空：这是否决判据本体，缺了不如不要")
        key = (ann_date, end_date)
        if key in seen:
            raise ValueError(f"{symbol}@{ann_date} 同一页里出现两次（分页重叠）")
        seen.add(key)
        fees = _opt(raw, fields, "audit_fees")
        rows.append(
            FinaAuditRow(
                source,
                symbol,
                ann_date,
                end_date,
                result,
                fees,
                _field(raw, fields, "audit_agency"),
                _field(raw, fields, "audit_sign"),
            )
        )
    return rows


class HolderNumberRow(NamedTuple):
    """`stk_holdernumber` 一行：某报告期的股东户数（08 情绪/筹码因子的原料）。"""

    source: str
    symbol: str
    ann_date: date
    end_date: date
    holder_num: float


def parse_holder_number(
    source: str, fields: Sequence[str], items: Sequence[Sequence[object]]
) -> list[HolderNumberRow]:
    rows: list[HolderNumberRow] = []
    seen: set[tuple[date, date]] = set()
    for raw in items:
        symbol = normalize_code(_field(raw, fields, "ts_code"))
        ann_date = _as_date(_field(raw, fields, "ann_date"))
        end_date = _as_date(_field(raw, fields, "end_date"))
        num = _opt(raw, fields, "holder_num")
        if num is None or num <= 0:
            raise ValueError(f"{symbol}@{ann_date} 股东户数 {num!r} 不成立：缺了给 None 也轮不到 0")
        key = (ann_date, end_date)
        if key in seen:
            raise ValueError(f"{symbol}@{ann_date} 同一页里出现两次（分页重叠）")
        seen.add(key)
        rows.append(HolderNumberRow(source, symbol, ann_date, end_date, num))
    return rows


class IndexDailyRow(NamedTuple):
    """`index_daily` 一行：一只指数的日线 OHLC（站点"查指数"的数据底座）。

    指数不是股票：R004 的涨跌幅限值、R005 的因子对它都不适用——它走 ADR-0015 的
    表级校验，OHLC 不变量在解析器里逐行验（与 R001 同一条不等式，表内自己的那一份）。
    vol=手、amount=千元（Tushare 谱系口径，2026-09-22 实测：上证当日 amount 1.008e9 千元）。
    """

    source: str
    symbol: str  # 指数代码带市场后缀（000001.SH / 399006.SZ）——它与个股代码空间重叠，必须带
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    amount: float | None


def parse_index_daily(
    source: str, fields: Sequence[str], items: Sequence[Sequence[object]]
) -> list[IndexDailyRow]:
    rows: list[IndexDailyRow] = []
    seen: set[tuple[str, date]] = set()
    for raw in items:
        symbol = _field(raw, fields, "ts_code")
        trade_date = _as_date(_field(raw, fields, "trade_date"))
        o, h = float(_field(raw, fields, "open")), float(_field(raw, fields, "high"))
        low, close = float(_field(raw, fields, "low")), float(_field(raw, fields, "close"))
        # R001 同一条不等式的表内版本：指数没有涨跌停语义，但 OHLC 不变量一样是硬的。
        if not h >= max(o, close) >= min(o, close) >= low > 0:
            raise ValueError(f"{symbol}@{trade_date} OHLC 不成立：O={o} H={h} L={low} C={close}")
        key = (symbol, trade_date)
        if key in seen:
            raise ValueError(f"{symbol}@{trade_date} 同一页里出现两次（分页重叠）")
        seen.add(key)
        rows.append(
            IndexDailyRow(
                source,
                symbol,
                trade_date,
                o,
                h,
                low,
                close,
                _opt(raw, fields, "vol"),
                _opt(raw, fields, "amount"),
            )
        )
    return rows


PARSERS: dict[str, Callable[[str, Sequence[str], Sequence[Sequence[object]]], list[Any]]] = {
    "stk_limit": parse_stk_limit,
    "daily_basic": parse_daily_basic,
    "forecast": parse_forecast,
    "fina_audit": parse_fina_audit,
    "stk_holdernumber": parse_holder_number,
    "index_daily": parse_index_daily,
}
