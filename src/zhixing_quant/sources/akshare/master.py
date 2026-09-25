"""akshare 证券主数据：交易所上市列表 → `Listing`（04 §五 接入清单第 3 项）。

一个函数同时服务沪市（`stock_info_sh_name_code`）与深市（`stock_info_sz_name_code`）：
两个源的列名不同而形状相同，别名表交给 `pick` 处理，比开两个适配器少一份会漂的重复逻辑。

不取源自带的"板块"列：板块一律由代码前缀经 `domain.symbol.board_of` 现判。落库就有两份
事实，而两份不一致时（源的板块列写的是历史归属）R004 按哪一份判都没有依据。

退市名单（`stock_info_sh_delist` 等）不在本层：Step 2a 只要"在册 + 上市日"，退市区间是
Step 3 股票池 PIT 还原的活，届时它和 `Listing.delisted_on` 一起接。

ST 帽是从证券简称的**前缀**推出来的，而前缀只是"抓取那天看到的样子"：所以那一段区间的起点
是 `manifest.csv` 里的抓取日，快照日之前一律不判（帽可能是那之后才戴上的）。真正的 ST 历史
要等一个能给出起止日期的源（01 §四 主数据条目里的"ST 状态历史"）。

停牌区间走另外两份快照（东财区间 + 百度按日事件），解析与并集合并在 `sources/akshare/suspend.py`
——R006 的豁免通道读的就是它们（04 §二："已知停市需由调用方登记"）。
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

from zhixing_quant import config
from zhixing_quant.domain.security import Interval, Listing, SecurityMaster
from zhixing_quant.domain.symbol import UnknownCode, board_of, normalize_code
from zhixing_quant.sources.akshare.suspend import (
    intervals_from_baidu,
    intervals_from_em,
    merge_intervals,
)
from zhixing_quant.sources.rows import SourceSchemaError, pick, to_date

# ts_code：relay stock_basic 那份 BSE 名单（akshare 没有 BSE 名单接口）。
CODE_ALIASES = ("证券代码", "A股代码", "ts_code", "code")
NAME_ALIASES = ("证券简称", "A股简称", "name")
LISTED_ALIASES = ("上市日期", "A股上市日期", "list_date")
#: 四份上市名单的文件名——**主数据要什么，快照就得有什么**，这条清单是它唯一的出处。
#: 2026-09-22 用户裁决扩板：主板/创业板/科创板/北交所一个不能少（ADR-0013 补充决定三）。
#: 前三份来自 akshare（`fetch.LISTINGS`，科创板同函数换 symbol），北交所那份来自 relay 的
#: stock_basic（akshare 没有 BSE 名单接口），列名 ts_code/name/list_date 落在 ALIASES 的
#: 英文兜底里，不用特判。
LISTING_SNAPSHOT_NAMES = (
    "stock_info_sh_name_code__主板A股",
    "stock_info_sz_name_code__A股列表",
    "stock_info_sh_name_code__科创板",
    "relay_stock_basic__BJ",
)
#: 停牌两份（任务 #57）：东财给区间、百度给按日事件，**并集**才盖得住 R006 FATAL 的票
#: （单用东财 71%、合并后抽样 49/49 = 100%，2026-09-24 实测）。key 与
#: `tools/capture_golden.py` 的 fetcher 一字不差——read_master 按名字找文件。
SUSPENSION_SNAPSHOT_NAMES = (
    "stock_tfp_em__suspend",
    "news_trade_notify_suspend_baidu__suspend",
)
#: 名单组 + 停牌组：`captured_on` 与 capture 覆盖检查要的是**全部**，少一份就拒收。
#: 这四 + 两份必须都能被 `tools/capture_golden.py` 抓到：那个工具重抓会**覆盖** manifest，
#: 少抓一份就是把它的 captured_at 抹掉，`captured_on` 随即拒收（zx-daily / zx-site 全线断）。
#: 该不变量由 `tests/test_capture_golden.py::test_the_capture_tool_covers_every_snapshot_
#: the_master_needs` 机器判定——"两处同名"这种话写在注释里挡不住漂移，2026-09-23 漂过一次。
SNAPSHOT_NAMES = LISTING_SNAPSHOT_NAMES + SUSPENSION_SNAPSHOT_NAMES
#: 抓取清单的文件名，同样与 `tools/capture_golden.py` 一致。ST 判定要读它：没有抓取日期，
#: "这名字挂着帽"就是一条不知道从哪天起生效的断言，而 PIT 模型不接受没有时点的状态。
MANIFEST_NAME = "manifest.csv"
#: 快照里真实存在的两种写法（`ST人福`、`*ST九鼎`）。`SST`/`S*ST` 是股权分置改革年代的老帽子，
#: 源早就不产出了，但认出来不花钱——漏认等于拿 10% 的上限去判一只只许涨 5% 的票。
ST_PREFIXES = ("ST", "*ST", "SST", "S*ST")


def is_st_name(name: str) -> bool:
    """证券简称是否挂着风险警示帽。只认前缀：帽子写在名字里任何别的位置都不是帽子。"""
    text = name.strip().upper()
    return any(text.startswith(prefix) for prefix in ST_PREFIXES)


@dataclass(frozen=True)
class SkippedRow:
    """一行没进主数据的位置、原文与原因。计数要有出处，否则"这次少 200 只"查不回去。"""

    position: int
    raw_code: str
    reason: str


@dataclass(frozen=True)
class MasterLoad:
    listings: tuple[Listing, ...]
    skipped: tuple[SkippedRow, ...]
    #: 只由 `read_master` 填：行级入口（`listings_from_rows`）拿不到抓取日期。
    st_periods: tuple[Interval, ...] = ()
    #: 停牌区间（已并集合并，`read_master` 填）。空 = R006 豁免通道没接线，照旧整批 FATAL。
    suspensions: tuple[Interval, ...] = ()

    def to_master(self) -> SecurityMaster:
        """装配成可查询的主数据。

        装配放在这里而不是让调用方自己拼参数：漏掉 `st_periods` 的那份主数据在日报上
        与"这个市场今天没有 ST 票"一模一样，而它真正的意思是 R004 的 5% 档整条失效；
        漏掉 `suspensions` 则是 R006 豁免通道静默失效——745 个批次整批 FATAL、健康分
        100→60，与"源真的断了"同形（任务 #57 实测）。
        """
        return SecurityMaster(self.listings, self.st_periods, self.suspensions)


def _parse_row(row: Mapping[str, object]) -> Listing | str:
    """一行 → 要么是一条登记，要么是它不能入库的原因。"""
    code = str(pick(row, *CODE_ALIASES) or "").strip()
    name = str(pick(row, *NAME_ALIASES) or "").strip()
    listed_on = to_date(pick(row, *LISTED_ALIASES))
    try:
        # 沪市列表里混着 B 股（900xxx），深市混着债券：代码本身合法，但不属于本项目的
        # 四档板块表，R004 对它们没有判据。
        board_of(normalize_code(code))
    except UnknownCode:
        return f"代码 {code!r} 不属于四档板块（B股/债券/表外代码）"
    if not name:
        return "缺证券简称"
    if listed_on is None:
        return "上市日期解析不出来"
    return Listing(code=code, name=name, listed_on=listed_on)


def listings_from_rows(rows: Sequence[Mapping[str, object]]) -> MasterLoad:
    """源行 → 上市登记。跳过的行一律带原因返回，不静默丢。

    为什么这里不像日历那样整批拒收：交易所列表**本来**就含 B 股与债券，那些不是数据错误
    而是范围之外，拒收会让这个源永远装不进来。但"跳了几行、为什么跳"必须是返回值的一部分
    ——真出事时（列名变了、某板块整批前缀没进表）无声的 `continue` 会把股票池缩小 20%
    而没有任何一处代码报错。全空才拒收，那才是列名变了的样子。

    重复代码不在这里去重：交给 `SecurityMaster` 抛 `MasterConflict`。两个源各说一遍
    "什么算重复"，迟早判得不一致。
    """
    listings: list[Listing] = []
    skipped: list[SkippedRow] = []
    for position, row in enumerate(rows):
        parsed = _parse_row(row)
        if isinstance(parsed, str):
            raw_code = str(pick(row, *CODE_ALIASES) or "").strip()
            skipped.append(SkippedRow(position=position, raw_code=raw_code, reason=parsed))
            continue
        listings.append(parsed)
    if not listings:
        raise SourceSchemaError(
            f"{len(rows)} 行主数据一行都没解析出来，多半是列名变了："
            f"{sorted({s.reason for s in skipped})}"
        )
    return MasterLoad(listings=tuple(listings), skipped=tuple(skipped))


def snapshot_paths(directory: Path | None = None) -> tuple[Path, ...]:
    """四份上市名单的快照位置（数据根 `golden/`，与日历同一处）。

    停牌两份**不在**这里：它们列名不同、走 `suspend.py` 的解析器，混进名单组会被
    `_parse_row` 整批拒掉。停牌快照的位置由 `suspension_snapshot_paths` 给。
    """
    root = directory if directory is not None else config.golden_dir()
    return tuple(root / f"{name}.csv" for name in LISTING_SNAPSHOT_NAMES)


def suspension_snapshot_paths(directory: Path | None = None) -> tuple[Path, ...]:
    """停牌两份快照的位置。缺任何一份都由 `read_master` 拒收（完整性同名单组）。"""
    root = directory if directory is not None else config.golden_dir()
    return tuple(root / f"{name}.csv" for name in SUSPENSION_SNAPSHOT_NAMES)


def _read_csv_rows(path: Path, *, what: str) -> list[Mapping[str, object]]:
    """读一份快照成行；文件缺失即拒——少一份快照在日报上与"今天没有停牌"同形。"""
    if not path.is_file():
        raise SourceSchemaError(f"没有主数据快照 {path}：先跑 tools/capture_golden.py（{what}）")
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def captured_on(directory: Path) -> date:
    """快照是哪一天抓的——ST 那段区间唯一的起点候选。

    名单组 + 停牌组**六份**各有一条 `captured_at` 时取**最晚**的那个：09-01 抓的名单里的
    ST 票，从 09-19 起算才不越界（我们只见过 09-19 那天的名字）。宁可少判几天，不多判
    没有证据的日子。停牌两份也进这道完整性：缺它们 = R006 豁免通道断了却看起来像
    "今天没有停牌"，与 ST 缺抓取日是同一种病。
    """
    path = directory / MANIFEST_NAME
    if not path.is_file():
        raise SourceSchemaError(
            f"没有抓取清单 {path}：ST 帽的生效日期只能从快照的抓取时间拿，"
            "先跑 tools/capture_golden.py"
        )
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    days = [to_date(row.get("captured_at")) for row in rows if row.get("key") in SNAPSHOT_NAMES]
    if len(days) != len(SNAPSHOT_NAMES) or any(day is None for day in days):
        raise SourceSchemaError(
            f"{path} 里没有全部 {len(SNAPSHOT_NAMES)} 份快照的抓取时间"
            f"（要 {sorted(SNAPSHOT_NAMES)}）：ST 帽从哪天起算判不出来，而判不出来在"
            "日报上与「今天没有 ST 票」长得一模一样"
        )
    return max(day for day in days if day is not None)


def read_master(directory: Path | None = None) -> MasterLoad:
    """离线读主数据快照：四份名单 + 两份停牌合起来读，跳过的行照原样带原因返回。

    合起来读而不是各读各的：`SecurityMaster` 要的是"那天在册的全市场"，两个入口迟早被
    用成一个（日报上就是少一半票）。这里不刷新、不联网——快照旧不旧是抓取边界的事。

    ST 区间在这里填：简称挂着帽的票，从抓取日起算一段"持续中"的 ST。往前不填——帽可能是
    快照那天之后才戴上的；往后填到下一次抓取为止，因为摘帽同样没有源可查，而帽一摘，下一次
    重抓就把这段截断了。这就是"主数据要定期重抓"这条运维口径在代码里的落点。

    停牌区间同样在这里填（任务 #57）：东财 + 百度两份各自解析，**并集合并去重**后才进
    `MasterLoad.suspensions`——相接/重叠不合就会在 `SecurityMaster` 装载时抛
    `MasterConflict`，六个 `read_master` 调用方一起退出码 2。
    """
    root = directory if directory is not None else config.golden_dir()
    observed_on = captured_on(root)
    rows: list[Mapping[str, object]] = []
    for path in snapshot_paths(root):
        rows += _read_csv_rows(path, what="少一份名单等于少一个交易所，股票池凭空缩小一半")
    load = listings_from_rows(rows)
    em_rows: list[Mapping[str, object]] = []
    baidu_rows: list[Mapping[str, object]] = []
    for path in suspension_snapshot_paths(root):
        raw = _read_csv_rows(path, what="缺停牌快照 = R006 豁免通道断线，745 个批次会整批 FATAL")
        # 两份源列名不同，按文件名分流给各自的解析器：混在一起没有哪个谓词认得出对方的列。
        if path.name.startswith("stock_tfp_em"):
            em_rows += raw
        else:
            baidu_rows += raw
    return replace(
        load,
        # 帽从**抓取次日**起生效：名称是快照日盘后抓的，而快照日当天的涨跌停早已按旧状态定板
        # （2026-09-25 实测：40 只当前 ST 抽样里 37 只当日板价仍是 ±10%）。按抓取日起套，
        # 等于拿"市场还没看到的公告"去判已经定板的价——R004 与 stk_limit 锚都会整行错判（#68）。
        st_periods=tuple(
            Interval(item.code, observed_on + timedelta(days=1))
            for item in load.listings
            if is_st_name(item.name)
        ),
        suspensions=merge_intervals(
            [*intervals_from_em(em_rows), *intervals_from_baidu(baidu_rows)]
        ),
    )
