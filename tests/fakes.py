"""测试用的假数据：假的 akshare 响应与假的干净区K线。

放在一处而不是每个测试文件各写一份：抓取层的注入点是全项目共用的那一个形状（`to_dict
(orient="records")` 的帧），两份定义一旦漂开，就会出现"某一边测过的形状，真网络里没有"。
`bar` 同理——它的默认值就是"一根正常K线"的定义，散在各文件里迟早长成几种不一样的"正常"。
"""

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from zhixing_quant.domain.bar import Bar


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
