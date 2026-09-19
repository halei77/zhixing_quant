"""akshare/sina 分钟线适配器：源行 → `BarDraft`（ADR-0009；01 Step 4 验收 1）。

与日线适配器同一条规矩：**这里不碰网络**，抓取留在 `fetch.py`，样本在 CI 里离线重放
（03 §二 L2）。源是 sina `stock_zh_a_minute`——本机够不着东财的分钟接口（实测：直连与走
代理都是 000，pitfall #22），而它没有窗口参数，每次固定回最近 1970 根。

三件事只有分钟线有，写在这里：

- **`day` 一列喂两个字段**。sina 的 `day` 是那根K线的收盘时刻（右端点：09:35 那根覆盖
  09:30–09:35，实测三种周期的标签都是右端点），`trade_date` 与 `ts` 都从它来（ADR-0009 决定 2）。
  它们因此不会互相矛盾——日期与时刻本来就不是源给的两件事。
- **没有 `adj_factor`**。分钟线不落因子：因子是阶梯函数、日内不变，复权价 = 分钟价 × 当日
  日线因子（ADR-0009 决定 4）。留 None 而不是 1.0，理由与日线适配器里"不用 1.0 兜底"同一条。
- **源标识按周期分开**。`akshare_minute_5/30/60`，因为 04 §三 按源打分：60 分钟的接口挂了
  不该扣 5 分钟的健康分。名字从 `layout.minute_dataset` 派生，周期表因此只有一份。

源给的所有值都是字符串（实测：`open`/`volume` 都是 `"1685.01"` 这种），归一交给 `rows.py`；
适配器不 `float()`，因为"变不成数"与"没给"是两条不同的结论（04 §二 R001 与 R010 的分界）。

缺时刻的行不在这里编造：`to_datetime` 认不出就给 None，那批数据会在存储层以"没有身份的行"
响掉（`write.store_bars`），或者先被 R008 判成一天里的重复行。两种都比"适配器替源补一个时刻"
好——补出来的那条在回测里是一根真实存在过的K线。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from zhixing_quant.domain.bar import BarDraft
from zhixing_quant.sources.rows import to_date, to_datetime, to_float
from zhixing_quant.storage import layout


def source_for(period: str) -> str:
    """周期（`5`/`30`/`60`）→ 源标识。认不出的周期在这里就抛，不发请求也不落盘。

    名字 = `"akshare_"` + dataset 名（`minute_5` → `akshare_minute_5`）：周期表只有
    `layout.SPECS` 一份，这里不重列，否则加一种周期要改两处而两处都会"看起来对"。
    """
    return f"akshare_{layout.minute_dataset(period)}"


def minute_drafts(
    rows: Sequence[Mapping[str, object]], *, symbol: str, period: str = "5"
) -> list[BarDraft]:
    """源行 → 待判定的分钟K线。一帧一个周期，所以 `period` 是参数而不是列（ADR-0009 决定 7）。

    行序原样保留：R009 判的是"源交来的顺序"，在这里排一次序就等于替源把乱序掩盖掉。
    """
    source = source_for(period)
    return [
        BarDraft(
            source=source,
            symbol=symbol,
            trade_date=to_date(row.get("day")),
            ts=to_datetime(row.get("day")),
            open=to_float(row.get("open")),
            high=to_float(row.get("high")),
            low=to_float(row.get("low")),
            close=to_float(row.get("close")),
            volume=to_float(row.get("volume")),
            amount=to_float(row.get("amount")),
            adj_factor=None,
            is_suspended=False,
        )
        for row in rows
    ]
