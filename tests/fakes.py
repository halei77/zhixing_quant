"""测试用的假数据：假的 akshare 响应与假的干净区K线。

放在一处而不是每个测试文件各写一份：抓取层的注入点是全项目共用的那一个形状（`to_dict
(orient="records")` 的帧），两份定义一旦漂开，就会出现"某一边测过的形状，真网络里没有"。
`bar` 同理——它的默认值就是"一根正常K线"的定义，散在各文件里迟早长成几种不一样的"正常"。
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

from zhixing_quant.domain.bar import Bar
from zhixing_quant.sources.akshare import master as akshare_master


class FakeFrame:
    """够用的 DataFrame 形：`fetch._rows` 只用到 `to_dict(orient="records")`。

    故意断言 `orient`：换成别的取值方式（比如 `values`）等于换了快照的行形状，而样本重放
    与真抓取共用的就是这个形状。
    """

    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = [dict(row) for row in rows]

    def to_dict(self, *, orient: str) -> list[dict[str, Any]]:
        assert orient == "records", "换了转换方式就是换了快照的形状，重放不再等价"
        return [dict(row) for row in self._rows]


class Recorder:
    """按调用顺序吐出预设的帧，并记下每次收到的 kwargs。

    测"参数拼装"就是测这个：源对错的参数不报错，只安静地返回别的东西。
    """

    def __init__(self, *frames: Sequence[Mapping[str, Any]]) -> None:
        self.frames = list(frames)
        self.kwargs: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> FakeFrame:
        self.kwargs.append(kwargs)
        return FakeFrame(self.frames[len(self.kwargs) - 1])


def bar(
    day: date,
    close: float = 10.0,
    *,
    symbol: str = "600519",
    factor: float | None = 8.0,
    source: str = "akshare_daily",
    volume: float = 1000.0,
    suspended: bool = False,
) -> Bar:
    """一根干净的日线：OHLC 有序、价格为正、默认带着因子 8.0（茅台那量级，好一眼看出复权生效了）。

    默认 `factor=8.0` 而不是 1.0：因子为 1 时三个口径完全一样，复权路径没走通也测不出来。
    """
    return Bar(
        source=source,
        symbol=symbol,
        trade_date=day,
        open=close,
        high=close * 1.02,
        low=close * 0.98,
        close=close,
        volume=volume,
        amount=close * volume,
        adj_factor=factor,
        is_suspended=suspended,
    )


def minute_bar(
    when: datetime,
    close: float = 10.0,
    *,
    symbol: str = "600519",
    period: int = 5,
    volume: float = 100.0,
    suspended: bool = False,
) -> Bar:
    """一根干净的分钟K线：`ts` 是收盘时刻，`adj_factor` 为空（ADR-0009 决定 4）。

    交易日从 `when` 推出来，不是各给一个参数：分钟线的 `trade_date` 与 `ts` 在源那边就是
    同一个字段（sina 的 `day`），能拼出"日期与时刻不匹配"的假K线，测出来的"能存"其实是
    "存了个坏数据还能查回来"。
    """
    return Bar(
        source=f"akshare_minute_{period}",
        symbol=symbol,
        trade_date=when.date(),
        ts=when,
        open=close,
        high=close * 1.01,
        low=close * 0.99,
        close=close,
        volume=volume,
        amount=close * volume,
        adj_factor=None,
        is_suspended=suspended,
    )


#: 快照日历覆盖的三天：`zx-daily` 与 `zx-minute` 的报告日都落在这三天里。
CALENDAR_DAYS = ("2024-01-02", "2024-01-03", "2024-01-04")


def snapshot_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个只装了快照的数据根，并把 `ZX_DATA_ROOT` 指过去：CLI 读什么、写什么全落在 tmp 里。

    两个采集入口共用它。各写一份的话，快照形状一改就只有一边会红——而"快照还能不能被读出
    主数据与日历"恰恰是这两个入口唯一自己不管的事（判定与落盘都在被注入的那一层）。
    """
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))
    golden = tmp_path / "golden"
    golden.mkdir()
    (golden / "tool_trade_date_hist_sina.csv").write_text(
        "trade_date\n" + "\n".join(CALENDAR_DAYS) + "\n", encoding="utf-8"
    )
    (golden / f"{akshare_master.SNAPSHOT_NAMES[0]}.csv").write_text(
        "证券代码,证券简称,上市日期\n600519,贵州茅台,2001-08-27\n", encoding="utf-8"
    )
    (golden / f"{akshare_master.SNAPSHOT_NAMES[1]}.csv").write_text(
        "A股代码,A股简称,A股上市日期\n300750,宁德时代,2018-06-11\n", encoding="utf-8"
    )
    rows = [
        f"{name},{name}.csv,2,,2024-01-03T08:56:05,ok," for name in akshare_master.SNAPSHOT_NAMES
    ]
    (golden / akshare_master.MANIFEST_NAME).write_text(
        "key,file,rows,columns,captured_at,status,detail\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return tmp_path
