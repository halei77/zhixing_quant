"""站点壳：一张页 + 四个端点（ADR-0013 决定 1、3、4）。

`/` 发 `site/static/` 里那三件静态件，`/api/*` 回数据。壳里没有口径（ADR-0013 决定 5）：token
数、价格口径行、"盘上 N 天而模板要 M 天"那句缺口，全部由 `prompt.build` 的返回值原样搬进响应，
页面只是把它们摆出来。这里只判三件事——口令（决定 3）、留痕（决定 4）、请求里的字段到调用的
对应。写在这里的东西一旦被允许变多，站点就会长出第二套口径，而 ADR-0011 决定 3 那句"口径行由
代码跟着配置生成，模板作者删不掉"就白立了。

数据根与采集端同一个根（ADR-0012 决定 1）：站点读的是**发布过来**的干净区（ADR-0006 决定 1/2），
不是另抓一份。所以这个进程不需要采集能力，也不许在生成之路上联网（ADR-0012 决定 2）。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from zhixing_quant import config
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Listing
from zhixing_quant.domain.symbol import UnknownCode, normalize_code
from zhixing_quant.site import prompt, search, templates
from zhixing_quant.site.search import search_indexes
from zhixing_quant.site.templates import Selection
from zhixing_quant.sources.akshare.calendar import load_calendar
from zhixing_quant.sources.akshare.master import read_master

#: 06 §四 那句"上限 10 只"里的 10。它是产品口径，不是"这一页显示几条"，所以不从请求里来。
RECENT_CAP = 10
#: 搜索一次回几条（下拉框容量），以及它的天花板——字典只有四千多只，而这是公网。
SEARCH_LIMIT = 20
SEARCH_MAX = 50
#: 06 §二 那句"固定口令"的环境变量名（ADR-0013 决定 3）。
TOKEN_ENV = "ZX_SITE_TOKEN"
#: 旧前端那三件静态件的位置：包内目录，路径由本文件反推（与 `config.repo_root()` 同一手法——
#: 写死绝对路径换机即废，也过不了 02 §六 的路径扫描）。ADR-0018 后果二：新前端通过功能
#: 验收前它仍是线上，验收通过后退役（文件保留到 05 Q1-② 批准删除）。
STATIC_DIR = Path(__file__).resolve().parent / "static"
#: 新前端（Vue 3 + Vite）的构建产物（ADR-0018 决定 2）。它在 `.gitignore` 里（构建产物不
#: 入库），所以只在本机与发布目标上存在——测试一律读 `frontend/src`，不读这里。
FRONTEND_DIST = config.repo_root() / "frontend" / "dist"


def _assets_dir(static_dir: Path) -> Path:
    """`/assets` 该映到哪个目录：两种前端的落盘形状不一样，这一格就是那个不一样。

    - 新前端（`frontend/dist`）：Vite 把构建产物放在 `dist/assets/` 里面，`index.html` 引用
      的就是 `/assets/<hash>.js`，所以映到 `dist/assets`。
    - 旧前端（`site/static`）：三件**平铺**在目录里，`index.html` 引用 `/assets/style.css`，
      于是只能映到目录本身。

    判据是盘上有没有 `assets/` 子目录，不是"哪个前端"——那是 `served_dir()` 的事。两个形状都在
    并存期里活着（ADR-0018 后果二），所以这里都得能发，且各有测试钉着。
    """
    nested = static_dir / "assets"
    return nested if nested.is_dir() else static_dir


def served_dir(built: Path = FRONTEND_DIST, legacy: Path = STATIC_DIR) -> Path:
    """该发哪个前端目录（ADR-0018 决定 2）：构建产物在就发它，不在就退回旧的 `site/static`。

    退回**是契约内的情形**：后果二明写并存期里 `site/static` 仍是线上（它还带着旧的口令存储
    偏离——localStorage 关页不清），所以退回不是谎报，是并存期的规定状态。两个都没有才起不来。

    那为什么不默默挑一个？因为代价二另外半句是「哪个是真前端要写清楚」——目录不能由构建时序
    悄悄决定，`main()` 会把它打进 stderr（写在服务自己的日志里，不是藏在代码里）。

    两个目录都走参数：测试于是能在临时目录里各造一份，把三个分支全钉住，而不依赖 npm build
    （CI 的干净 checkout 里没有 dist——它在 .gitignore 里）。
    """
    for candidate in (built, legacy):
        if (candidate / "index.html").is_file():
            return candidate
    raise RuntimeError(
        f"两个前端目录都没有 index.html：{built} 与 {legacy}。"
        "先在 frontend/ 跑 npm run build（ADR-0018 决定 2）"
    )


#: 鉴权中间件的下一个处理器。这一版 FastAPI 没导出这个别名，所以自己写。
_Dispatch = Callable[[Request], Awaitable[Response]]


class Entry(BaseModel):
    """留痕里的一条（ADR-0013 决定 4）。`at` 是写下的时刻，给人看"多久之前"。"""

    code: str
    name: str
    at: str


class CustomEntry(BaseModel):
    """自定义组合的一段数据选择（ADR-0017）：K线 dataset + 交易日数。"""

    dataset: str
    days: int


class PromptBody(BaseModel):
    """生成请求。`as_of` 不给就是今天：周日生成的提示词，末行自己退到周五那根K线

    （ADR-0012 决定 2：区间终点是 ≤ as_of 的最后一个交易日，不是 as_of 本身。）
    `custom` 只在 `template == "自定义"` 时用（ADR-0017）：K线四种 × 天数，字段与
    口径固定，前端传不进来。
    """

    code: str
    template: str
    as_of: date | None = None
    custom: list[CustomEntry] | None = None


class RecentBody(BaseModel):
    code: str


class Recent:
    """最近搜索的票：一份 JSON 数组，同码再搜置顶，满了淘汰队尾（ADR-0013 决定 4）。

    记的是**票**不是敲过的字符串，所以 `record` 之前先按代码去掉旧的自己——按条目数上限的话，
    把一只票搜三次就挤掉了另外两只，而 06 §四 那句的量词是"只"。
    """

    def __init__(self, path: Path, *, cap: int = RECENT_CAP) -> None:
        self._path = path
        self._cap = cap

    def load(self) -> list[Entry]:
        """没有文件就是"还没搜过"，不是错误。形状不对则是错误，不清空。"""
        if not self._path.is_file():
            return []
        raw: list[dict[str, Any]] = json.loads(self._path.read_text(encoding="utf-8"))
        return [Entry.model_validate(item) for item in raw]

    def record(self, code: str, name: str, *, at: str) -> list[Entry]:
        entries = [entry for entry in self.load() if entry.code != code]
        entries.insert(0, Entry(code=code, name=name, at=at))
        kept = entries[: self._cap]
        self._save(kept)
        return kept

    def _save(self, entries: Sequence[Entry]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = [entry.model_dump() for entry in entries]
        self._path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def site_token(env: Mapping[str, str] | None = None) -> str:
    """口令从环境读；没配就**起不来**，不是"跳过鉴权"（ADR-0013 决定 3）。

    "忘了配"实现成"没鉴权"是最坏的一种能跑：它的日志与"部署成功"完全同形，而它已经在公网上裸奔。
    """
    source = os.environ if env is None else env
    token = source.get(TOKEN_ENV, "").strip()
    if not token:
        raise RuntimeError(f"{TOKEN_ENV} 没有设置：站点不许裸奔（ADR-0013 决定 3）")
    return token


def create_app(
    *,
    listings: Sequence[Listing],
    cfg: templates.Config,
    calendar: TradingCalendar,
    recent: Recent,
    token: str,
    static_dir: Path = STATIC_DIR,
) -> FastAPI:
    """装配应用。依赖全部从参数进来（与 ADR-0010 决定 1 同一手法），只有 `main` 从盘上取。

    `static_dir` 也走参数：发哪个前端是装配决定，不是模块常量——测试于是能在临时目录里
    造一份假 dist 判"发的是谁"，而不必先跑一次 npm build（CI 的干净 checkout 里没有 dist）。

    字典既用来搜，也用来判"这只票存不存在"（`_known`）——同一份名单，不另立第二份答案。
    """
    book = search.Index(listings)
    names = {listing.code: listing.name for listing in listings}
    app = FastAPI(title="知行 · 提示词站点", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def guard(request: Request, call_next: _Dispatch) -> Response:
        """回数据的一律要口令，回壳本身的不必（ADR-0013 决定 3）。

        比"逐个端点记得加 Depends"可靠：新加一个 `/api/...` 而忘了鉴权，是这种写法唯一会犯的错，
        而它的表现是"一切正常"。前缀这条规则做不到忘。
        """
        if not request.url.path.startswith("/api") or _authorized(request, token):
            return await call_next(request)
        return Response(status_code=401, content="口令不对")

    app.mount("/assets", StaticFiles(directory=_assets_dir(static_dir)), name="assets")

    @app.middleware("http")
    async def no_asset_cache(request: Request, call_next: _Dispatch) -> Response:
        """静态件永远重验（etag/304 很便宜）：页面更新而 JS 被旧缓存拖着走的现场，
        排查的人会以为是新代码坏了——这次站点二期的"说明不变"就是它。"""
        response = await call_next(request)
        if request.url.path.startswith("/assets") or request.url.path == "/":
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def index() -> FileResponse:
        """站点那张页（06 §八-1 的全流程在这里走）。

        它不在 `/api` 前缀下，于是中间件天然放行（决定 3 那句"回壳本身的不必"）：口令框就画在页
        上，被拒的人得先看见这张页，才知道要往里面填什么。服务端不注口令——它不认人，无会话无
        账号，代价记在 ADR-0013 代价六。
        """
        return FileResponse(static_dir / "index.html")

    @app.get("/api/search")
    def api_search(q: str, limit: int = Query(SEARCH_LIMIT, ge=1, le=SEARCH_MAX)) -> Any:
        stocks = [{**asdict(hit), "kind": "stock"} for hit in book.match(q, limit=limit)]
        indexes = [
            {"code": h.code, "name": h.name, "by": h.by, "kind": h.kind} for h in search_indexes(q)
        ]
        return {"hits": [*indexes, *stocks]}

    @app.get("/api/templates")
    def api_templates() -> Any:
        return {"templates": [asdict(template) for template in cfg.templates]}

    def _custom_template(entries: list[CustomEntry]) -> Any:
        """ADR-0017：自定义 = 一次性合成的 Template。role/task/output 固定文案不由前端传，
        可变面收窄到 dataset × days；校验粒度与模板装载同级，全部 ValueError → 400 带原话。"""
        if not entries:
            raise ValueError("自定义组合是空的：至少要选一个K线类型")
        allowed = {"daily", "minute_5", "minute_30", "minute_60"}
        seen: set[str] = set()
        selections: list[Selection] = []
        for entry in entries:
            if entry.dataset not in allowed:
                raise ValueError(
                    f"自定义的 K线类型 {entry.dataset!r} 不在可选里：{'、'.join(sorted(allowed))}"
                )
            if entry.dataset in seen:
                raise ValueError(f"K线类型 {entry.dataset} 选了两次")
            if not 1 <= entry.days <= 750:
                raise ValueError(f"{entry.dataset} 的天数 {entry.days} 越界（1–750）")
            seen.add(entry.dataset)
            selections.append(Selection(dataset=entry.dataset, days=entry.days))
        from zhixing_quant.site.templates import Template as _T

        return _T(
            name="自定义",
            role="你是资深 A 股分析师",
            task="按用户自选的K线周期与深度，给出趋势、量价与关键价位判断",
            data=tuple(selections),
            output=(
                "先给结论（看多/看空/观望 + 一句话理由），再给依据。\n"
                "每条依据都要引用具体日期与表中的价格，不许出现表中没有的数字。"
            ),
            format="markdown",
            fields=("open", "high", "low", "close", "volume", "amount"),
            adjust="backward",
            status="ready",
            waiting_on="",
        )

    @app.post("/api/prompt")
    def api_prompt(body: PromptBody) -> Any:
        """生成一条提示词。失败一律 400 带上原来那句话，不改写、不翻译。

        收口成一条 `except ValueError` 是因为生成路上**可预期**的失败全是它的子类——pending 模板、
        缺分母、空表、日历不覆盖、代码不认、模板名不存在、自定义组合不合法。不是 ValueError 的
        就是程序错，让它 500：把 bug 伪装成"请求不对"是排查路上最贵的一种礼貌。
        指数不走这里——模板是股票口径（06 §四），指数有自己的 K线查询。
        """
        try:
            if body.template == "自定义":
                template = _custom_template(body.custom or [])
            else:
                template = cfg.by_name(body.template)
            code = _known(body.code, names)
            built = prompt.build(
                template,
                code,
                body.as_of or date.today(),
                calendar=calendar,
                token_warn_above=cfg.token_warn_above,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"text": built.text, "tokens": built.tokens, "warn": built.warn}

    @app.get("/api/kline")
    def api_kline(
        code: str,
        kind: str = Query("stock", pattern="^(stock|index)$"),
        days: int = Query(120, ge=5, le=500),
    ) -> Any:
        """K线查询（不复权；指数走参考表）。数据的口径与形状由 `prompt.read_kline` 定。"""
        if kind == "stock":
            code = _known(code, names)
        bars = prompt.read_kline(code, days=days, kind=kind)
        return {
            "kind": kind,
            "code": code,
            "bars": bars,
            # 面板那一行的展示值（含涨跌幅）在纯层算好，壳只搬不改（ADR-0013 决定 5）。
            "quote": prompt.quote_of(bars),
        }

    @app.get("/api/recent")
    def api_recent_list() -> Any:
        return {"entries": recent.load()}

    @app.post("/api/recent")
    def api_recent_add(body: RecentBody) -> Any:
        """记下"看过的那只票"。名字从字典现取，不信前端传来的副本（决定 4 那句"真值是 code"）。"""
        code = _known(body.code, names)
        return {"entries": recent.record(code, names[code], at=_now())}

    return app


def _known(text: str, names: Mapping[str, str]) -> str:
    """把请求里的代码规成字典的键。认不出、或不在字典里，都是 404。

    "这只票不存在"与"这只票没有K线"是两件事：前者在这里响，后者由 `prompt.build` 里那张空表的
    拒绝来说（ADR-0013 代价三）。混成一件事的话，用户会以为站点在骗他。
    """
    try:
        code = normalize_code(text)
    except UnknownCode as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if code not in names:
        raise HTTPException(status_code=404, detail=f"主数据里没有 {code}：搜索字典是它的出处")
    return code


def _authorized(request: Request, token: str) -> bool:
    """`X-Token` 逐字节比。用 `compare_digest` 不是玄学：口令只有一个、服务在公网上，而返回真假
    的时间差是这台机器上唯一一条能问出"第几个字符对了"的通道。字节化是因为它只吃 ASCII。
    """
    given = request.headers.get("x-token", "")
    return secrets.compare_digest(given.encode(), token.encode())


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def build_parser() -> argparse.ArgumentParser:
    """`zx-site` 的参数表，一张空表——**空表不等于不判**。

    这个进程的可调项全在环境变量里（`ZX_SITE_TOKEN`/`ZX_SITE_HOST`/`ZX_SITE_PORT`），所以没有
    flag 可选；而 `parse_args` 对多出来的参数直接退 2，于是 `zx-site --port 8765` 的结局是"这条
    命令写错了"，不是"起了一个监听 8000 的服务"（独立审计 M1）。忽略参数的那条路在 systemd 里
    一定会走一遍：写单位文件的人照别的服务的习惯加 `--port`，然后端口就对不上反代，而日志上说服务
    起来了。
    """
    return argparse.ArgumentParser(
        prog="zx-site",
        description="提示词站点：一张页 + 四个端点。可调项只有 ZX_SITE_TOKEN / "
        "ZX_SITE_HOST / ZX_SITE_PORT 这三个环境变量，没有任何命令行参数",
    )


def main(argv: Sequence[str] | None = None) -> None:
    """进程入口（`zx-site`）。默认只绑本机：公网那一步在阿里云侧由反代给出（ADR-0006）。"""
    build_parser().parse_args(argv)
    served = served_dir()
    print(f"zx-site 发前端目录：{served}", file=sys.stderr)
    cfg = templates.load(config.prompt_templates_file())
    app = create_app(
        listings=read_master().listings,
        cfg=cfg,
        calendar=load_calendar(until=date.today()),
        recent=Recent(config.site_recent_file()),
        token=site_token(),
        static_dir=served,
    )
    host = os.environ.get("ZX_SITE_HOST", "127.0.0.1")
    port = int(os.environ.get("ZX_SITE_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
