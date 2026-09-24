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


#: 停牌快照的地板日期（`date=` 参数 / 百度回填起点）。东财语义是「停牌截止 ≥ date 或
#: 未复牌」：新停市的 end 一定 ≥ 今天 ≥ 地板，**同一地板重抓永远包含新增，地板无需前移**
#: （任务 #57 调研实测）。百度侧按交易日循环回填 floor..today，吃的是同一条"区间只会往后
#: 长"的性质。
SUSPEND_FLOOR = "20240830"


def em_suspend_frame(*, call: AkCall | None = None) -> Rows:
    """东财停牌**区间**快照（`stock_tfp_em`）：一次调用回全市场在册停牌，实测 865 行 / 0.6s。"""
    return _rows("stock_tfp_em", call, date=SUSPEND_FLOOR)


def baidu_suspend_frame(day: date, *, call: AkCall | None = None) -> Rows:
    """百度停牌**按日事件**快照（`news_trade_notify_suspend_baidu`）：一天一次调用，实测
    mean 0.28s/日。调用方负责按交易日循环与限速——循环边界（floor..today、日历读盘）是
    抓取编排，不是单次请求的形状。"""
    return _rows("news_trade_notify_suspend_baidu", call, date=day.strftime("%Y%m%d"))


def listing_frame(function: str, group: str, *, call: AkCall | None = None) -> Rows:
    """一所的上市列表。

    按所分开抓而不是并成一份：`master.read_master` 读的是两个文件，文件名里带着所名——
    一份合并后的快照重放不出"某个交易所今天什么都没给"这种只发生在单所上的故障。
    """
    return _rows(function, call, symbol=group)


#: akshare 侧的三份上市列表 `(函数名, 源要求的 symbol)`。北交所那份**不在这里**：akshare
#: 没有 BSE 名单接口，它走 relay（ADR-0013 补充决定三）。这三份加上 relay 那份才是
#: `master.SNAPSHOT_NAMES` 的全部——那条"一份不能少"由 `tests/test_capture_golden.py`
#: 的覆盖检查机器判定，不靠这段注释里的"一一对应"（2026-09-23 就是这句话先漂的）。
#: 写在一处是因为漏掉一个板块等于股票池凭空少一块，而这件事只有"这里列了三行"能挡住。
LISTINGS = (
    ("stock_info_sh_name_code", "主板A股"),
    ("stock_info_sz_name_code", "A股列表"),
    ("stock_info_sh_name_code", "科创板"),
)


def fetch_listings(*, call: AkCall | None = None) -> Rows:
    """akshare 侧三份上市列表拼成一份。北交所那份不在内，见 `LISTINGS`。"""
    frames = [listing_frame(function, group, call=call) for function, group in LISTINGS]
    return [row for frame in frames for row in frame]


#: 东财公告两端点（data.eastmoney.com 公开 JSON）。akshare `stock_individual_notice_report`
#: 只封了列表且**丢 `display_time`、没有正文**；正文端点 akshare 未封装——消息组件要
#: 原文与毫秒挂网时刻，只能直调这两个（2026-09-25 探测报告 §2.1：25 连发全 200、
#: 无 rate 头、headerless 可用、0.05–0.25s/发；调用方负责 ≥0.3s 限速与重试）。
_NOTICE_LIST_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_NOTICE_CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"


def notice_list(
    symbol: str,
    begin: date,
    end: date,
    *,
    page: int = 1,
    page_size: int = 100,
    timeout: float = 20.0,
    get: Any = None,
) -> dict[str, Any]:
    """一页单票公告列表的**原始响应**。网络边界：只发请求不判形状——解析在
    `sources.akshare.notice.parse_notice_list`（黄金样本重放测那边，本函数薄到不用测）。

    `sr=-1` 按日期倒序（窗口查询实测有序），`ann_type=A` 只要 A 股；
    `begin_time/end_time` 是东财自己的 YYYY-MM-DD 窗口参数。
    """
    if get is None:
        import requests

        get = requests.get
    response = get(
        _NOTICE_LIST_URL,
        params={
            "sr": "-1",
            "page_size": str(page_size),
            "page_index": str(page),
            "ann_type": "A",
            "client_source": "web",
            "stock_list": symbol,
            "f_node": "0",
            "s_node": "0",
            "begin_time": begin.strftime("%Y-%m-%d"),
            "end_time": end.strftime("%Y-%m-%d"),
        },
        timeout=timeout,
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


def notice_content(
    art_code: str, *, page: int = 1, timeout: float = 20.0, get: Any = None
) -> dict[str, Any]:
    """一条公告一页正文的**原始响应**。分页由 `page` 控制（每页 5000 字，`page_size` 是总页数）
    ——回填只取首页（探测报告 §九 的存储口径），解析同样在 `notice.parse_notice_content`。
    """
    if get is None:
        import requests

        get = requests.get
    response = get(
        _NOTICE_CONTENT_URL,
        params={"art_code": art_code, "client_source": "web", "page_index": str(page)},
        timeout=timeout,
    )
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body
