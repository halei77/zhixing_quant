"""akshare 的真实抓取：全项目唯一碰网络的地方（04 §五 接入清单第 1 项的另一半）。

适配器（`daily.py` / `calendar.py` / `master.py`）一律不联网，抓取边界交给调用方——黄金
样本要在 CI 里离线重放（03 §二 L2），会自己联网的适配器重放不出任何断言。但"离线可测"
不等于"不抓取"：真请求总得有人发，这一层就是它。发完之后立刻交给适配器，本模块不做任何
判定，所以它薄到可以整体不看。

为什么这层在库里而不在 `tools/` 的一次性脚本里：每日任务（`sources/jobs/daily.py`）和
黄金样本抓取要的是同一个抓取口径。写两遍就会漂，而漂了没人报错的那种差异，最后一定表现成
"样本重放全绿、线上天天告警"。

DataFrame → 行列表的转换用 `to_dict(orient="records")`：它保列序、把 NaN 留成 float 的
`nan` 而不是 None。快照重放回来的行就是这个形状，两边共用同一个形状，才不会出现"只有
真网络才有的字段"。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from zhixing_quant.domain.symbol import Board, board_of, normalize_code
from zhixing_quant.sources.rows import Pair, Rows
from zhixing_quant.storage import layout

#: akshare 的调用边界：函数名 + 关键字参数 → 一个 DataFrame（本模块往下不留 pandas 类型）。
AkCall = Any


def _rows(function: str, call: AkCall | None = None, **kwargs: object) -> Rows:
    """调一次 akshare 并落成行列表。

    `call` 是给测试的注入点：假 DataFrame 不用装 pandas 就能测这一层的参数拼装。传 None
    才真的 `import akshare`——不联网的场合（CI、门禁重放）不该为它付导入代价。
    """
    if call is None:
        from importlib import import_module

        ak: Any = import_module("akshare")
        call = getattr(ak, function)
    frame: Any = call(**kwargs)
    return [dict(row) for row in frame.to_dict(orient="records")]


#: 板块 → sina 的市场前缀。主板不在表里：它横跨沪深两所（60x 在上、000/002 在下），
#: 板块本身推不出前缀，只有它判不出交易所时才可疑。
#:
#: 这张表故意从 `domain.symbol.board_of` 派生而不是另列一份前缀表：哪些数字段属于北交所
#: 是领域事实（它决定 R004 的限幅档），在源适配层重列一遍就是两份事实，改 `_PREFIX_TABLE`
#: 时不会有人想起来同步这里——而这里错的表现为"某天北交所整批抓回空表"。
_BOARD_MARKET = {Board.GEM: "sz", Board.STAR: "sh", Board.BSE: "bj"}


def market_of(code: str) -> str:
    """代码 → `sh`/`sz`/`bj`。板块表认不出的代码，这里同样认不出（抛 UnknownCode）。"""
    normalized = normalize_code(code)
    market = _BOARD_MARKET.get(board_of(normalized))
    if market is not None:
        return market
    return "sh" if normalized.startswith("6") else "sz"


def sina_symbol(code: str) -> str:
    """akshare `stock_zh_a_daily` 要的 `symbol` 形如 `sh600519`。

    各源写法不一（600519 / sh600519 / 600519.SH），先归一再拼前缀。ETF、B 股、可转债在
    `market_of` 里就抛了：让它们走到网络层只会拿回空表或不相干的行情，而日报上表现为
    "这只票今天零行"——那比在这里抛错难查一个数量级。
    """
    normalized = normalize_code(code)
    return f"{market_of(normalized)}{normalized}"


def _ymd(day: date) -> str:
    return day.strftime("%Y%m%d")


def daily_frame(
    code: str, start: date, end: date, *, adjust: str = "", call: AkCall | None = None
) -> Rows:
    """一帧日线。`adjust` 是源的参数：`""` 不复权、`"hfq"` 后复权、`"qfq"` 前复权。

    单独暴露这一层是因为黄金样本要把两帧落成两个文件（`capture_golden` 的 key 里带
    `__raw` / `__hfq`），一次取两帧反而没法分别记清单。
    """
    return _rows(
        "stock_zh_a_daily",
        call,
        symbol=sina_symbol(code),
        start_date=_ymd(start),
        end_date=_ymd(end),
        adjust=adjust,
    )


def fetch_daily(code: str, start: date, end: date, *, call: AkCall | None = None) -> Pair:
    """一只票、一个闭区间，取回 (不复权, 后复权) 两帧——`sources/jobs` 的 `Fetcher`。

    两帧各发一次请求是源的接口决定的（`adjust` 参数二选一）。只取回一帧会让 `adj_factor`
    变成 None，R005 于是报"没有因子"——那在日报上是和"没有复权事件"不同的两件事。
    """
    raw = daily_frame(code, start, end, call=call)
    hfq = daily_frame(code, start, end, adjust="hfq", call=call)
    return raw, hfq


def minute_frame(code: str, period: str = "5", *, call: AkCall | None = None) -> Rows:
    """一帧分钟K线。**没有窗口参数**：源固定回最近 1970 根（实测，三种周期同数）。

    两件事在这里硬掉，都不给调用方留旋钮：

    - `period` 必须是 `layout.SPECS` 里有的那个周期。放任意数字过去，源真的会回数据（`1` 也行），
      于是几十万次请求打完才发现没有对应的 dataset 可落——请求在前、形状校验在后是这里最贵的错法。
    - `adjust` 恒为空。源的 qfq/hfq 用它自己那套因子，与"分钟价 × 当日日线因子"
      （ADR-0009 决定 4）不是同一个口径；两种口径混进同一个 dataset，之后没人分得清哪根是哪个。
    """
    layout.minute_dataset(period)  # 只为校验周期：认不出的不发请求。dataset 名由落盘侧同一个函数取
    return _rows("stock_zh_a_minute", call, symbol=sina_symbol(code), period=period, adjust="")


def fetch_calendar(*, call: AkCall | None = None) -> Rows:
    """交易日历全量。没有窗口参数：源本身就是"从开市到今天"的一张表。"""
    return _rows("tool_trade_date_hist_sina", call)


def listing_frame(function: str, group: str, *, call: AkCall | None = None) -> Rows:
    """一所的上市列表。

    按所分开抓而不是并成一份：`master.read_master` 读的是两个文件，文件名里带着所名——
    一份合并后的快照重放不出"某个交易所今天什么都没给"这种只发生在单所上的故障。
    """
    return _rows(function, call, symbol=group)


#: 两所列表的 `(akshare 函数名, 源要求的 symbol)`，与 `master.SNAPSHOT_NAMES` 一一对应。
#: 写在一处是因为漏掉一个交易所等于股票池凭空少一半，而这件事只有"这里列了两行"能挡住。
LISTINGS = (("stock_info_sh_name_code", "主板A股"), ("stock_info_sz_name_code", "A股列表"))


def fetch_listings(*, call: AkCall | None = None) -> Rows:
    """沪深两所的主板/全列表主数据，拼成一份——`SecurityMaster` 要的形状。"""
    frames = [listing_frame(function, group, call=call) for function, group in LISTINGS]
    return [row for frame in frames for row in frame]
