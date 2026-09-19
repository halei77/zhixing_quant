"""测试用的假东西：假的 akshare 响应、假的干净区K线、假策略与假板。

放在一处而不是每个测试文件各写一份：抓取层的注入点是全项目共用的那一个形状（`to_dict
(orient="records")` 的帧），两份定义一旦漂开，就会出现"某一边测过的形状，真网络里没有"。
`bar` 同理——它的默认值就是"一根正常K线"的定义，散在各文件里迟早长成几种不一样的"正常"。

回测那一组（`assumptions` / `minutes` / `flat_bands` / `Scripted`）住这里的理由是同一件事的
另一面：引擎、指标、报告、CLI 四层都要"三天几根K线、不封板、按剧本下单"这套道具，而它们对
同一份输入必须看到同一个东西——否则"报告里的数与引擎算的是不是一回事"这种判据根本没得判。
"""

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from zhixing_quant.backtest.engine import BandLookup, Signal, View
from zhixing_quant.backtest.limits import PriceBand
from zhixing_quant.backtest.spec import Assumptions, Cost, Execution, Side, Sizing
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


def minute_span(
    when: datetime,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float = 100.0,
    symbol: str = "600519",
    dataset: str = "minute_5",
) -> Bar:
    """一根四个价格各给各的分钟K线。

    与 `minute_bar` 的分工：那一个把 open/high/low 钉在 close 上，是"一根干净的K线"；这几个字段
    一旦相等，"合成日的开盘取自最早那根的 open"这类判据就测不出来了——取错字段看不出区别。
    合成与对账两层判的正是字段归属与两边的相对高低，所以它们要的是这一个。

    参数是 dataset 而不是周期数字：对账给出来的那条 `Finding` 带的就是 dataset 名，而 `source`
    由它派生（`sources/akshare/minute.py` 同一条式子）——测试手里握着"哪一批"，不该再换算一次。
    """
    return Bar(
        source=f"akshare_{dataset}",
        symbol=symbol,
        trade_date=when.date(),
        ts=when,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=close * volume,
        adj_factor=None,
    )


def daily_span(
    day: date,
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    amount: float,
    symbol: str = "600519",
    factor: float | None = 1.0,
    suspended: bool = False,
) -> Bar:
    """一根四个价格与量额各自给定的日线。`minute_span` 同一条理由。

    量额在这里是分开的两个参数而不像另外几个 builder 那样由价格乘出来：对账要能只让成交额
    对不上而成交量对得上，否则"这两条判据是两条"这件事永远测不到。
    """
    return Bar(
        source="akshare_daily",
        symbol=symbol,
        trade_date=day,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=amount,
        adj_factor=factor,
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


#: 与 `config/backtest.toml` 同一组数（佣金万 2.5 最低 5 元、印花税卖出千 0.5、过户费十万分之一、
#: 滑点万 2；一手 100 股、期初底仓 3 手）。抄在这里而不是去读那张表：这几个数是测试的**判据**
#: 的一部分——期望值是按它们手算出来的，表改了而测试没改，红的应该是测试。
BACKTEST_COST = Cost(
    commission_pct=0.025, commission_min=5.0, stamp_pct=0.05, transfer_pct=0.001, slippage_pct=0.02
)
BACKTEST_SIZING = Sizing(lot_size=100, base_lots=3)


def assumptions(delay: int = 1, participation: float = 5.0) -> Assumptions:
    """一套能跑引擎的执行假设。`delay` 与 `participation` 是要被单独拧的旋钮，其余钉死。"""
    return Assumptions(
        cost=BACKTEST_COST,
        execution=Execution(delay_bars=delay, max_participation_pct=participation),
        sizing=BACKTEST_SIZING,
    )


def minutes(
    day: date, prices: list[float], *, symbol: str = "600519", volume: float = 1e6
) -> list[Bar]:
    """从 09:30 起每 5 分钟一根，`ts` 是收盘时刻：第一根 09:35。四价全等，好核对成交在哪个价上。"""
    start = datetime.combine(day, datetime.min.time()).replace(hour=9, minute=30)
    return [
        minute_span(
            start + timedelta(minutes=5 * (offset + 1)),
            open_=price,
            high=price,
            low=price,
            close=price,
            volume=volume,
            symbol=symbol,
        )
        for offset, price in enumerate(prices)
    ]


def flat_bands(prev_close: float = 10.0) -> BandLookup:
    """一只不封板的板：主板 10% 档、昨收 `prev_close`。判据本身有别处测，这里要的是"有板"。

    参数带下划线是因为它真的不看是哪只票、哪天——`BandLookup` 的签名要求它在那儿。
    """

    def bands(_code: str, _day: date) -> PriceBand:
        return PriceBand.bound(10.0, prev_close)

    return bands


class Scripted:
    """按"该票第几根"给信号的桩策略。索引按票各算，与 `delay_bars` 同一把尺。"""

    name = "scripted"

    def __init__(self, plan: dict[tuple[str, int], Side]) -> None:
        self.plan = plan
        self.seen: list[View] = []

    def signals(self, view: View, /) -> Signal | None:
        self.seen.append(view)
        side = self.plan.get((view.code, view.index))
        return None if side is None else Signal(side=side, lots=3)
