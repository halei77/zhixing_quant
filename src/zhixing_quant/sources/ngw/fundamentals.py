"""牛股王基本面采集器**骨架**：stockshare / finacereport 响应 → 参考表行（NamedTuple）。

本批次只做到「解析出 NamedTuple + 测试」：**不接进 `zx-*` 任务、不落盘**——落盘要走
ADR-0015 逐表验收（表级校验器 + 锚点对账 + 黄金样本），那是后续批次的事。

两条响应的形状（2026-09-24 实测，黄金样本在数据根 `golden/`）：

- **stockshare**：百来个键的扁平对象。数值分三类，解析各走各的路：
  纯数字符串（`pe: "17.37"`）直接 float；带 `%` 的（`roe: "16.75%"`）剥掉再 float，
  字段名以 `_pct` 结尾存百分点数；带符号的显示串（`updownrate: "-1.14%"`）符号是真符号
  （涨跌方向），保留。股本市值有 `*origin` 数值孪生（`totalstockvalueorigin:
  1546350940437`），一律取孪生，不解析「1.546万亿」这种显示串——两种写法并存时，
  解析显示串迟早撞上换单位的那天。
- **finacereport**：`{ColumnInfo, ValueInfo}`。`ColumnInfo` 是指标名录（Name 中文 /
  FiledName 英文键），`ValueInfo` 是逐报告期倒序的值列表；期内 `Datas` 按 **FiledName
  对齐**（禁下标——04 §五 转接专项同款纪律，字段顺序随参数集会变），值取 `OriginValue`
  数值，`Value` 为 `"--"`/空时记 None（显示层的「无数据」，0 与无数据是两件事）。
  `reporttype` 四个值按实测内容钉死：1=资产负债表、2=利润表、3=现金流、4=主要指标。

**没有发布日**：`ValueInfo` 只有 `EndDate`（报告期），没有 `ann_date`——ngw 供给不了
点时可见性，所以 Forward PE 的正源仍是 rds `fina_report_rc`（ADR-0014；探测结论见数据根
`reports/source-probe/2026-09-24-ngw-niuguwang-source-access.md`），骨架这里只按报告期
序列解析，不假装有发布日。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import NamedTuple

from zhixing_quant.sources.rows import to_date, to_float

#: `reporttype` → 中文名（接口描述.txt 的 URL 逐字对照 + 四个值都实测过响应内容）。
REPORT_TYPES: Mapping[str, str] = {
    "1": "资产负债表",
    "2": "利润表",
    "3": "现金流",
    "4": "主要指标",
}


def _text(body: Mapping[str, object], key: str) -> str:
    value = body.get(key)
    return "" if value is None else str(value)


def _pct(value: object) -> float | None:
    """`"16.75%"` / `"-1.14%"` / `4.1577` → 百分点 float；空/认不出给 None。

    百分点不是比率：`16.75` 意思是 16.75%。存百分点与源显示一致，换算成比率是用它的人
    的事——这里偷偷除 100，下游就会把 16.75% 当 1675% 用。
    """
    if isinstance(value, str):
        text = value.strip().removesuffix("%").strip()
        if not text:
            return None
        return to_float(text)
    return to_float(value)


class StockShare(NamedTuple):
    """估值/股本快照里本项目要的那几项。字段名是项目口径，不是源的键名。"""

    stockcode: str
    stockname: str
    innercode: str
    boardname: str
    industry: str  # 源键 `platename`
    exchange: str  # 源键 `exchangeMarket`
    price: float | None  # `nowv` 元
    preclose: float | None
    updown: float | None  # 涨跌额（带符号）
    updown_rate_pct: float | None  # `"-1.14%"` → -1.14
    pe: float | None
    pe_ttm: float | None
    pb: float | None
    ps: float | None
    eps_ttm: float | None  # 源键 `epsttm`
    roe_pct: float | None
    dividend_yield_pct: float | None  # 源键 `dividendRatio`（已是百分点数）
    turnover_pct: float | None
    total_shares: float | None  # `totalstocknumorigin` 股
    float_shares: float | None  # `publicfloatshareqtyorigin` 股
    total_mv: float | None  # `totalstockvalueorigin` 元
    float_mv: float | None  # `totalfloatsharevalorigin` 元
    volume: float | None  # `totaltradevolume` 股（当日累计）
    amount: float | None  # `totalvaluetrade` 元（当日累计）
    suspended: bool
    openstatus: str


def stock_share_of(body: Mapping[str, object]) -> StockShare:
    """stockshare 响应 → 快照行。缺键给空串/None——骨架不替源猜值（R010 同款取向）。"""
    return StockShare(
        stockcode=_text(body, "stockcode"),
        stockname=_text(body, "stockname"),
        innercode=_text(body, "innercode"),
        boardname=_text(body, "boardname"),
        industry=_text(body, "platename"),
        exchange=_text(body, "exchangeMarket"),
        price=to_float(body.get("nowv")),
        preclose=to_float(body.get("preclose")),
        updown=to_float(body.get("updown")),
        updown_rate_pct=_pct(body.get("updownrate")),
        pe=to_float(body.get("pe")),
        pe_ttm=to_float(body.get("pettm")),
        pb=to_float(body.get("pb")),
        ps=to_float(body.get("ps")),
        eps_ttm=to_float(body.get("epsttm")),
        roe_pct=_pct(body.get("roe")),
        dividend_yield_pct=_pct(body.get("dividendRatio")),
        turnover_pct=_pct(body.get("turnoverrate")),
        total_shares=to_float(body.get("totalstocknumorigin")),
        float_shares=to_float(body.get("publicfloatshareqtyorigin")),
        total_mv=to_float(body.get("totalstockvalueorigin")),
        float_mv=to_float(body.get("totalfloatsharevalorigin")),
        volume=to_float(body.get("totaltradevolume")),
        amount=to_float(body.get("totalvaluetrade")),
        suspended=str(body.get("suspend") or "") == "1",
        openstatus=_text(body, "openstatus"),
    )


class ReportCell(NamedTuple):
    """一个（报告期 × 指标）格子。`end_date` 是报告期，**不是发布日**（源没有发布日）。"""

    end_date: date | None
    filed_name: str
    metric: str  # 中文名（ColumnInfo.Name），查不到给空串
    value: float | None  # OriginValue；显示为 `--`/空时 None
    rate_pct: float | None  # 同比/占比串（`Rate`）剥 % 后的百分点；含义随指标而异


def fina_report_rows(body: Mapping[str, object]) -> tuple[ReportCell, ...]:
    """finacereport 响应 → 格子序列。**保源序**（报告期倒序）：骨架不排序——点时排序是
    后续批次表级校验器的事。

    按 `FiledName` 对齐（04 §五：禁硬编码下标）；标题行（FiledName 为空）跳过——它们是
    排版，不是指标。
    """
    column_info = body.get("ColumnInfo") or []
    names: dict[str, str] = {}
    if isinstance(column_info, Sequence):
        for column in column_info:
            if isinstance(column, Mapping):
                filed = str(column.get("FiledName") or "")
                if filed:
                    names[filed] = str(column.get("Name") or "")
    value_info = body.get("ValueInfo") or []
    cells: list[ReportCell] = []
    if not isinstance(value_info, Sequence):
        return ()
    for period in value_info:
        if not isinstance(period, Mapping):
            continue
        end = to_date(period.get("EndDate"))
        datas = period.get("Datas") or []
        if not isinstance(datas, Sequence):
            continue
        for data in datas:
            if not isinstance(data, Mapping):
                continue
            filed = str(data.get("FiledName") or "")
            if not filed:
                continue  # 标题行
            display = str(data.get("Value") or "").strip()
            value = None if display in ("", "--") else to_float(data.get("OriginValue"))
            cells.append(
                ReportCell(
                    end_date=end,
                    filed_name=filed,
                    metric=names.get(filed, ""),
                    value=value,
                    rate_pct=_pct(data.get("Rate")),
                )
            )
    return tuple(cells)
