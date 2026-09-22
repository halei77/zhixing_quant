"""`site/api.py`：壳的鉴权、四个端点与最近搜索留痕（ADR-0013 决定 3/4/5）。

判的全是**外部形状**：状态码、响应体、盘上那个 JSON 文件。壳里没有可单独判的东西——ADR-0013
决定 5 说它不产生口径，所以这些测试怎么发请求就怎么读响应：400 里那句话是 `prompt.build` 与
`templates` 的原话，不是这里转述的（第 12、15 两条就是这句话的可执行版本）。

口令是 ASCII 串：它要经过 HTTP 头，而头不是给汉字准备的通道。决定 3 把口令钉在请求头上，
传输形状本身就得能写成测试——第 5 条判的就是这个形状。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.fakes import bar, snapshot_root
from zhixing_quant import config
from zhixing_quant.domain.calendar import TradingCalendar
from zhixing_quant.domain.security import Listing
from zhixing_quant.site import api, templates, tokens
from zhixing_quant.site.templates import Config, Selection, Template
from zhixing_quant.storage import layout
from zhixing_quant.storage.write import store_bars

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
CALENDAR = TradingCalendar([D1, D2, D3])
TOKEN = "sesame-open-17"
AUTH = {"X-Token": TOKEN}

#: 与真表同名的两条：响应体读起来就该是站点将来要显示的东西，而不是 test1/test2。
READY = Template(
    name="短期投资",
    role="你是资深 A 股分析师",
    task="判断未来 1–4 周的机会与风险",
    data=(Selection(dataset=layout.DAILY, days=120),),
    output="先给结论，再给依据。",
    format="markdown",
    fields=("open", "high", "low", "close", "volume", "amount"),
    adjust="backward",
    status="ready",
    waiting_on="",
)
PENDING = Template(
    name="长期投资",
    role="你是资深 A 股基本面分析师",
    task="评估估值水平与基本面趋势",
    data=(Selection(dataset=layout.DAILY, days=750),),
    output="序列缺失的年份写「数据未提供」。",
    format="markdown",
    fields=("close",),
    adjust="backward",
    status="pending",
    waiting_on="二期财务数据组件（Forward PE、PE(TTM)/PB/PS 历史）",
)
CFG = Config(token_warn_above=100_000, templates=(READY, PENDING))
#: 阈值 1：任何一份真提示词都会超，用来判"警告有没有活着走到调用方"。
LOUD = Config(token_warn_above=1, templates=CFG.templates)

BOOK = (
    Listing("600519", "贵州茅台", date(2001, 8, 27)),
    Listing("300750", "宁德时代", date(2018, 6, 11)),
)
#: 12 只在册的票，够把留痕的上限（10 只）撑爆。名字不参与判定，代码才是真值（决定 4）。
WIDE = tuple(Listing(f"6000{index:02d}", f"票{index}", date(2001, 8, 27)) for index in range(12))

PROMPT_OK = {"code": "600519", "template": "短期投资", "as_of": "2024-01-04"}
#: 同一条请求，只是少了 `as_of`——那个默认值本身就是被测的东西。
PROMPT_NO_DATE: dict[str, object] = {"code": "600519", "template": "短期投资"}

#: 每个回数据的端点都上这份表，中间那格是"方法 + 路径"。中间件按前缀鉴权，运行时做不到
#: 漏一个端点（决定 3），但**这张表**会漏——所以 `test_the_endpoint_table_is_the_real_routing`
#: 拿真路由表核对它，逼我在新加端点时回来加一格。
ENDPOINTS: tuple[tuple[str, str, Callable[[TestClient], httpx.Response]], ...] = (
    ("搜索", "GET /api/search", lambda c: c.get("/api/search", params={"q": "600"})),
    ("K线", "GET /api/kline", lambda c: c.get("/api/kline", params={"code": "600519"})),
    ("模板", "GET /api/templates", lambda c: c.get("/api/templates")),
    ("生成", "POST /api/prompt", lambda c: c.post("/api/prompt", json=PROMPT_OK)),
    ("留痕·读", "GET /api/recent", lambda c: c.get("/api/recent")),
    ("留痕·写", "POST /api/recent", lambda c: c.post("/api/recent", json={"code": "600519"})),
)


def make_app(
    trace: Path,
    *,
    calendar: TradingCalendar = CALENDAR,
    listings: Sequence[Listing] = BOOK,
    cfg: Config = CFG,
    cap: int = api.RECENT_CAP,
    token: str = TOKEN,
) -> FastAPI:
    """装配一个应用，只换指名的那些零件。

    默认那套是"两只在册、三天在盘、口令为真"。`trace` 必填而不是内置：留痕落在哪是这条测试要
    不要观察它的前提，藏在默认值里就没法比对盘上那个文件。
    """
    return api.create_app(
        listings=listings,
        cfg=cfg,
        calendar=calendar,
        recent=api.Recent(trace, cap=cap),
        token=token,
    )


def make_client(
    trace: Path,
    *,
    headers: dict[str, str] = AUTH,
    calendar: TradingCalendar = CALENDAR,
    listings: Sequence[Listing] = BOOK,
    cfg: Config = CFG,
    cap: int = api.RECENT_CAP,
) -> TestClient:
    """`make_app` 加一个默认口令。

    口令从 `headers` 走而不是混进零件：绝大多数测试要的是"带对了口令"，只有鉴权那几条需要
    刻意带错，两个入口各管一头，写错的测试一眼看得出来。
    """
    app = make_app(trace, calendar=calendar, listings=listings, cfg=cfg, cap=cap)
    return TestClient(app, headers=headers)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """三天日线，且**只落在 600519 上**：另一只在册的票留给"字典里有、盘上没数"那条区分。

    顺带把 `ZX_DATA_ROOT` 指进 tmp。没有这个夹具，客户端就会去读真机上的数据根——一个忘了带
    夹具的测试能读出真市场，那种红绿都不能信。所以它挂在 `trace` 上：用得上客户端的测试躲不开它。
    """
    base = snapshot_root(tmp_path, monkeypatch)
    store_bars(
        [bar(D1, 10.0), bar(D2, 11.0), bar(D3, 12.0)],
        dataset=layout.DAILY,
        root=base / "data",
    )
    return base / "data"


@pytest.fixture
def trace(root: Path, tmp_path: Path) -> Path:
    """留痕文件的位置：在数据根下面，与 `config.site_recent_file()` 同一个形状。

    它依赖 `root` 不是凑数——留痕与K线住同一个数据根，把两件事拆成两个夹具，就会有一个只用
    前者、悄悄读了真盘的测试。
    """
    assert root == tmp_path / "data"
    return tmp_path / "site" / "recent.json"


@pytest.fixture
def client(trace: Path) -> TestClient:
    return make_client(trace)


# ── 决定 3：口令 ──────────────────────────────────────────────────────────────


def test_the_page_and_its_assets_are_public(trace: Path) -> None:
    """静态壳不鉴权（决定 3）：口令是为了不让公网把全市场扫一遍，页本身不是数据。

    页面上就有口令输入框，被拒的人得先看见它才能填——把 `/` 也 gate 掉的话，第一次访问的人
    对着一个 401 的空白页，永远走不进 06 §八-1 那条流程。
    """
    anonymous = make_client(trace, headers={})
    page = anonymous.get("/")
    assert page.status_code == 200
    assert 'id="preview"' in page.text
    assert TOKEN not in page.text
    for asset in ("/assets/app.js", "/assets/style.css"):
        served = anonymous.get(asset)
        assert served.status_code == 200, asset
        assert TOKEN not in served.text, "静态件是公用的、明文可取的，口令的值一个字符都不许写进去"


def test_no_asset_path_escapes_the_package_directory(trace: Path) -> None:
    """`/assets/../…` 的三种写法都拿不到包里的其它文件：这一格是公网服务，路径即攻击面。

    `%2e%2e` 与 `..%2f` 分开写：浏览器与 http 客户端各自会规范化一部分，只测一种的话，另一种
    到底是谁挡住的都说不清。
    """
    anonymous = make_client(trace, headers={})
    for sneaky in ("/assets/../config.py", "/assets/%2e%2e/config.py", "/assets/..%2fconfig.py"):
        response = anonymous.get(sneaky)
        assert response.status_code == 404, sneaky
        assert "parquet_dir" not in response.text


def test_the_page_writes_text_and_never_markup() -> None:
    """票名与提示词正文一律走 `textContent`：那两段文字一个来自 akshare、一个来自表格里的数据。

    `innerHTML` 把"数据"当"标记"读，是这一层唯一能把外部内容变成代码的路。页面没有账号也没有
    会话，口令就存在 `sessionStorage` 里——一次 XSS 换走的正是那串东西（代价六）。所以这条
    不是风格检查：它挡住的是这个站点唯一一条提权路径。
    """
    script = (api.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script
    assert "document.write" not in script
    assert script.count("textContent") >= 5


def test_every_id_the_script_reaches_for_exists_in_the_page() -> None:
    """JS 里每个 `$("x")` 都要在 HTML 里有 `id="x"`：改名只改一边时浏览器不报错，只白屏。

    这条是这一层唯一能做的集成判定——没有浏览器可跑。反向（HTML 里的 id 全被用到）不判：
    `#out`、`#pick` 这类是排版容器，不是引用点。下限那句是防空转：正则一旦不匹配，前两条
    断言就集体变成真。
    """
    script = (api.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    page = (api.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    wanted = set(re.findall(r'\$\("([a-z][\w-]*)"\)', script))
    declared = set(re.findall(r'id="([\w-]+)"', page))
    assert len(wanted) >= 12, f"正则没抓到东西，这条测试在空转：{sorted(wanted)}"
    assert wanted - declared == set()


@pytest.mark.parametrize(
    ("label", "route", "call"),
    ENDPOINTS,
    ids=[entry[0] for entry in ENDPOINTS],
)
def test_every_data_endpoint_demands_the_token(
    trace: Path,
    label: str,
    route: str,
    call: Callable[[TestClient], httpx.Response],
) -> None:
    """五个端点、三种口令状态：没带与带错都是 401，带对才不是 401。

    忘了鉴权的表现是"一切正常"，所以这条的价值全在"每个都试一遍"上；`label` 与 `route` 只是
    让失败信息说得出是哪个端点的哪一格。
    """
    for headers in ({}, {"X-Token": "sesame-open-1"}):
        assert call(make_client(trace, headers=headers)).status_code == 401, f"{label} {route}"
    assert call(make_client(trace)).status_code != 401, f"{label} {route}"


def test_the_endpoint_table_is_the_real_routing(trace: Path) -> None:
    """拿路由表核对那张端点表：新加一个 `/api/...` 而没进表，这里就红。

    运行时由前缀中间件兜住，这条兜的是评审时——两者不是一件事，因为漏掉的那个端点在运行时完全
    正常（那正是决定 3 挑中间件而不是挑 `Depends` 的理由）。相等而不是包含：表里留一条已经
    删掉的端点，同样是漂移。
    """
    declared = {route for _, route, _ in ENDPOINTS}
    actual = {
        f"{method} {route.path}"
        for route in make_app(trace).routes
        if isinstance(route, APIRoute) and route.path.startswith("/api")
        for method in (route.methods or set()) - {"HEAD"}
    }
    assert actual == declared


def test_the_token_is_compared_byte_for_byte(trace: Path) -> None:
    """长出来的、短一截的、大小写不同的都不算对：口令只有一个，比较没有"差不多"。

    前缀那条尤其要说：`X-Token` 的取值是请求里唯一的秘密，一个 `startswith` 写法的鉴权在公网
    上撑不过一次试探。
    """
    for given in (f"{TOKEN}-x", TOKEN[:-1], TOKEN.upper()):
        response = make_client(trace, headers={"X-Token": given}).get("/api/recent")
        assert response.status_code == 401, given
    assert make_client(trace, headers=AUTH).get("/api/recent").status_code == 200


def test_the_token_travels_in_a_header_only(trace: Path) -> None:
    """同一串口令放进查询串不算数（决定 3）：查询串会进访问日志，也会留在浏览器历史里。

    这条不是在保护日志——日志归运维。它判的是"壳只认一个地方"：认两个地方就会有两个可以写错
    的实现，而其中较松的那一个迟早被"临时"用上一次。
    """
    anonymous = make_client(trace, headers={})
    assert anonymous.get("/api/recent", params={"token": TOKEN}).status_code == 401
    written = anonymous.post("/api/recent", json={"code": "600519"}, params={"token": TOKEN})
    assert written.status_code == 401


def test_a_refused_request_says_so_in_plain_words(trace: Path) -> None:
    """401 的正文是给人看的一句话，不是一份 JSON 错误体：这一格不需要被前端解析。"""
    response = make_client(trace, headers={}).get("/api/search", params={"q": "600"})
    assert response.status_code == 401
    assert response.text == "口令不对"


def test_no_token_configured_means_the_site_does_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """没配口令是起不来，不是"跳过鉴权"（决定 3）。

    "忘了配"实现成"没鉴权"是最坏的一种能跑：它的日志与"部署成功"完全同形，而它已经在公网上
    裸奔。空白串同样不算配了——`ZX_SITE_TOKEN=` 在 shell 里就是写"我没设"的写法。
    """
    for env in ({}, {api.TOKEN_ENV: ""}, {api.TOKEN_ENV: "   "}):
        with pytest.raises(RuntimeError) as caught:
            api.site_token(env)
        assert api.TOKEN_ENV in str(caught.value)
    monkeypatch.setenv(api.TOKEN_ENV, TOKEN)
    assert api.site_token() == TOKEN
    assert api.site_token({api.TOKEN_ENV: "  padded  "}) == "padded"


# ── 决定 1/2：搜索与模板两个读端点 ───────────────────────────────────────────


def test_search_answers_from_the_dictionary(client: TestClient) -> None:
    """接线：响应里就是 `search.Index` 给的那几栏，一栏不增删。

    `by` 必须是 `"name"` 而不是布尔值：壳把三路匹配的档位原样交给前端，前端才知道要不要把汉字
    高亮。这一格如果被壳"顺手"改平，`test_site_search` 那十几条判定就白钉了。
    """
    assert client.get("/api/search", params={"q": "茅台"}).json() == {
        "hits": [
            {
                "code": "600519",
                "name": "贵州茅台",
                "listed_on": "2001-08-27",
                "by": "name",
                "kind": "stock",
            },
        ]
    }


def test_search_returns_index_hits_with_kind_index(client: TestClient) -> None:
    """指数在搜索里与个股并肩（2026-09-22 用户要求"能查指数"）；kind 是前端分流的依据。"""
    hits = client.get("/api/search", params={"q": "上证"}).json()["hits"]
    assert {"code": "000001.SH", "name": "上证指数", "by": "name", "kind": "index"} in hits
    hits = client.get("/api/search", params={"q": "hs300"}).json()["hits"]
    assert hits[0] == {"code": "000300.SH", "name": "沪深300", "by": "name", "kind": "index"}


def test_kline_serves_stocks_from_the_clean_zone(client: TestClient, root: Path) -> None:
    """个股 K线：不复权口径（查行情看真实价），升序、字段齐全。

    夹具的三根K线落在 2024-01，而 K线的窗口从今天往回数——这里种一根"今天"的K线，
    顺带把"最新一根在窗口里"这件事实也钉住。
    """
    from tests.fakes import bar as fake_bar

    today = date.today()
    store_bars([fake_bar(today, 99.5)], dataset=layout.DAILY, root=root)
    body = client.get("/api/kline", params={"code": "600519", "days": 5}).json()
    assert body["kind"] == "stock" and body["code"] == "600519"
    assert len(body["bars"]) >= 1
    assert {"date", "open", "high", "low", "close", "volume", "amount"} <= set(body["bars"][-1])


def test_kline_serves_indexes_from_the_reference_table(client: TestClient) -> None:
    """指数 K线出自参考表 index_daily；未知指数是 404 不是空表。"""
    body = client.get("/api/kline", params={"code": "000001.SH", "kind": "index"}).json()
    assert body["kind"] == "index"
    assert isinstance(body["bars"], list)
    unknown = client.get("/api/kline", params={"code": "999999.SH", "kind": "index"})
    assert unknown.json()["bars"] == []
    bad = client.get("/api/kline", params={"code": "600519", "kind": "weird"})
    assert bad.status_code == 422


def test_the_search_limit_is_clamped_at_both_ends(client: TestClient) -> None:
    """`limit` 的上下界写在 `Query` 里而不是壳的判断里：越界是 422，不是悄悄截断。

    上界（50）的理由是这是公网——一次"随便看看"不该能把四千多只票整体拉走。下界 1 是空查询
    本来就不回全市场（决定 2），`limit=0` 只能是调用方写错了。
    """
    for bad in (0, api.SEARCH_MAX + 1):
        assert client.get("/api/search", params={"q": "6", "limit": bad}).status_code == 422
    assert len(client.get("/api/search", params={"q": "6", "limit": 1}).json()["hits"]) == 1
    assert client.get("/api/search", params={"q": "6"}).json()["hits"], "默认 limit 没给出去"


def test_the_template_list_keeps_pending_rows_visible(client: TestClient) -> None:
    """pending 模板在列表里、带着它的 `waiting_on`：06 §五 说的是"改配置即生效"，不是藏起来。

    前端要拿 `status` 把那一格灰掉、拿 `waiting_on` 说明为什么灰。这两样不在响应里的话，站点
    就只能在"看不见"和"点了报错"之间选一个，而 06 §八-2 要的是前者不成立、后者有解释。
    """
    rows = client.get("/api/templates").json()["templates"]
    assert [row["name"] for row in rows] == ["短期投资", "长期投资"]
    assert [row["status"] for row in rows] == ["ready", "pending"]
    assert "Forward PE" in rows[1]["waiting_on"]
    assert rows[0]["data"] == [{"dataset": "daily", "days": 120, "fields": None}]


# ── 决定 5：生成端点不产生口径 ───────────────────────────────────────────────


def test_generating_passes_builds_words_through_unchanged(client: TestClient) -> None:
    """生成响应就是 `prompt.build` 那三样：文本、token 数、给人看的警告。

    "盘上 3 个交易日，模板要 120"这一句只有 `prompt.heading` 会写（ADR-0012 决定 3）：它出现
    在响应里，就同时证明了两件事——数据真的从干净区来（价是 80 不是 10），而壳一个字都没改。
    """
    body = client.post("/api/prompt", json=PROMPT_OK).json()
    assert "### 日K（盘上 3 个交易日，模板要 120）" in body["text"]
    assert "| 80.00 |" in body["text"]
    assert body["warn"] is None
    assert body["tokens"] == tokens.estimate(body["text"])


def test_the_token_warning_reaches_the_caller_not_the_prompt(trace: Path) -> None:
    """超阈值时警告走响应体的另一格，正文一个字都不许多（06 §八-5、ADR-0012 决定 6）。

    壳是这堵墙的最后一段：`build` 把 warn 放在 `Prompt` 上而不是拼进 `text`，如果壳图省事
    把它拼回去，模型就会把"省 token 的两条路"当成分析任务读进去。
    """
    body = make_client(trace, cfg=LOUD).post("/api/prompt", json=PROMPT_OK).json()
    assert "超过阈值" in body["warn"]
    assert "超过阈值" not in body["text"]


def test_a_market_spelling_of_the_code_reaches_the_same_prompt(client: TestClient) -> None:
    """`sh600519` 与 `600519` 生成同一条提示词：规代码用的是域里那一个函数，不在这儿再判一遍。

    `normalize_code` 已经管住了交易所前缀与后缀，所以搜索、生成、留痕三处用的是同一个写法。
    """
    spelled = client.post("/api/prompt", json=dict(PROMPT_OK, code="sh600519")).json()
    plain = client.post("/api/prompt", json=PROMPT_OK).json()
    assert spelled == plain


def test_an_unknown_template_is_a_400_that_names_the_list(client: TestClient) -> None:
    """模板名不存在：400 带回那半句"现有的：短期投资、长期投资"（决定 5 那句"不改写"）。

    报错里带着现有的名字是 `templates.by_name` 的写法，不是壳加的——壳做的事是不吞掉它。前端
    将来直接照这一句渲染，所以它在响应体里必须完整。
    """
    response = client.post("/api/prompt", json=dict(PROMPT_OK, template="价值回归"))
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail.startswith("模板表里没有「价值回归」")
    assert "短期投资" in detail
    assert "长期投资" in detail


def test_a_pending_template_is_refused_with_its_own_reason(client: TestClient) -> None:
    """pending 生成不了，理由原样返回：列表里看得见、点了要响（ADR-0011 决定 5）。"""
    response = client.post("/api/prompt", json=dict(PROMPT_OK, template="长期投资"))
    assert response.status_code == 400
    assert "二期财务数据组件" in response.json()["detail"]


def test_the_shell_does_not_invent_the_turnover_denominator(trace: Path) -> None:
    """要换手率的模板：壳不替它兜一个流通股本出来（ADR-0011 决定 5 接到壳上）。

    一期没有分母的出处（坑 #30），所以 `build` 缺的 `float_shares` 在这里也不给。少一列的表
    看起来和满列的一样完整，而模型会替你把那一列编出来。这条钉的是"壳别自作聪明"。
    """
    turnover = replace(READY, fields=("close", "turnover"))
    cfg = Config(token_warn_above=100_000, templates=(turnover,))
    response = make_client(trace, cfg=cfg).post("/api/prompt", json=PROMPT_OK)
    assert response.status_code == 400
    assert "流通股本" in response.json()["detail"]


def test_a_symbol_that_does_not_exist_and_one_with_no_bars_are_different_answers(
    client: TestClient,
) -> None:
    """404 与 400 分开（决定 3、代价三）："没有这只票"与"这只票还没采进来"不是一件事。

    混成一件事，用户会以为站点在骗他。600000 的代码规得出、但字典里没有（字典是搜索的出处）；
    300750 在册而盘上没数（本夹具只给 600519 落了盘）——两个来源、两种说法。
    """
    no_bars = client.post("/api/prompt", json=dict(PROMPT_OK, code="300750"))
    assert no_bars.status_code == 400
    assert "一张都没有的K线表" in no_bars.json()["detail"]
    not_listed = client.post("/api/prompt", json=dict(PROMPT_OK, code="600000"))
    assert not_listed.status_code == 404
    assert "主数据里没有 600000" in not_listed.json()["detail"]


def test_a_string_that_is_not_a_code_is_404(client: TestClient) -> None:
    """连"像个代码"都不是：404，用的是 `normalize_code` 那句原话，不另写一套判断。"""
    response = client.post("/api/prompt", json=dict(PROMPT_OK, code="茅台"))
    assert response.status_code == 404
    assert "茅台" in response.json()["detail"]


def test_no_as_of_means_today_and_today_never_invents_a_bar(client: TestClient) -> None:
    """不给 `as_of` 就是今天（ADR-0012 决定 2），而今天之后没有数就不许多装。

    本夹具的日历只到 2024-01-04：默认值走的仍是"今天"这条路，区间终点自己退到日历最后那一天，
    标题承认"盘上 3 个交易日"。所以它与显式给 2024-01-04 的响应一字不差——周日、假期、以及
    "今天的数还没采进来"这三种情况在站点上都是这个形状。三种情况共用一个响应，正是这里要的:
    壳不替用户挑日子。
    """
    omitted = client.post("/api/prompt", json=PROMPT_NO_DATE)
    assert omitted.status_code == 200
    assert omitted.json() == client.post("/api/prompt", json=PROMPT_OK).json()
    assert "盘上 3 个交易日" in omitted.json()["text"]


def test_a_day_before_the_calendar_starts_is_a_400_naming_that_day(client: TestClient) -> None:
    """`as_of` 早于日历第一天：区间无从定起，`CalendarTooShort` 那句原话进 400。

    顺带判了 pydantic 那一步：`as_of` 收的是 ISO 日期串，解析不来的形状是 422 而不是 400,
    而"这一天在日历之前"是一个合法的日期、一个不成立的请求。
    """
    body = client.post("/api/prompt", json=dict(PROMPT_OK, as_of="2023-01-01"))
    assert body.status_code == 400
    assert "2023-01-01" in body.json()["detail"]


# ── 决定 4：最近搜索的留痕 ────────────────────────────────────────────────────


def test_recording_a_symbol_keeps_one_copy_on_top(client: TestClient) -> None:
    """同码再搜置顶且只有一条（决定 4）。

    两次都记一条的话，一只票搜三遍就挤掉另外两只——而 06 §四 那句的量词是"只"。
    """
    for code in ("600519", "300750", "600519"):
        entries = client.post("/api/recent", json={"code": code}).json()["entries"]
    assert [entry["code"] for entry in entries] == ["600519", "300750"]
    assert [entry["code"] for entry in client.get("/api/recent").json()["entries"]] == [
        "600519",
        "300750",
    ]


def test_the_trace_holds_ten_symbols_not_ten_searches(trace: Path) -> None:
    """上限 10 数的是票（06 §四）：连记 12 只之后，最早那两只才掉出去。

    走真端点而不是直接调 `Recent`：这个 10 是产品口径，得钉在壳的请求形状上。队头是最新那只，
    所以断言写的是"倒着数"。
    """
    codes = [listing.code for listing in WIDE]
    client = make_client(trace, listings=WIDE)
    for code in codes:
        client.post("/api/recent", json={"code": code})
    kept = [entry["code"] for entry in client.get("/api/recent").json()["entries"]]
    assert len(kept) == api.RECENT_CAP == 10
    assert kept == codes[2:][::-1]


def test_the_name_in_the_trace_comes_from_the_dictionary(client: TestClient) -> None:
    """记的是票，名字从字典现取（决定 4）：请求里只有 `code` 一个字段。

    前端传来什么名字都不算——`RecentBody` 没有 name 栏，这条就是在判"没有"这件事：留痕是站点
    对"用户看过哪些票"的回答，不是对用户自己写的东西的回声。代码也被规成了 6 位。
    """
    entries = client.post("/api/recent", json={"code": "sh600519"}).json()["entries"]
    assert entries == [{"code": "600519", "name": "贵州茅台", "at": entries[0]["at"]}]


def test_the_trace_survives_a_restart(trace: Path) -> None:
    """换一个客户端（＝重启一次进程）还看得见：06 §四 那句"服务端持久化"就是这个意思。

    记在内存里的"最近搜索"活不过一次部署，而部署在这个站点上不是异常事件（ADR-0006）。
    """
    make_client(trace).post("/api/recent", json={"code": "300750"})
    fresh = make_client(trace)
    assert [entry["code"] for entry in fresh.get("/api/recent").json()["entries"]] == ["300750"]


def test_an_empty_trace_is_a_missing_file_not_an_error(trace: Path) -> None:
    """还没有文件＝还没搜过，回空数组。

    分开写这两件事是为了挡住下一个人"顺手"补一句"文件不存在就报错"：那会让每个新用户的第一个
    请求变成 500。
    """
    assert not trace.exists()
    assert make_client(trace).get("/api/recent").json() == {"entries": []}


def test_a_broken_trace_raises_instead_of_being_cleared(trace: Path) -> None:
    """形状不对就是错误，不清空（决定 4 的代价四）。

    明文 JSON 是自选的形状（它能被人和 grep 直接读），代价是它可能坏掉。坏掉时最省事的写法是
    "当作空的重来"，那等于把用户仅有的那十条记录悄悄扔掉——所以这里让它响，而且文件原样还在。
    """
    trace.parent.mkdir(parents=True)
    trace.write_text("这不是一段 JSON", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        make_client(trace).get("/api/recent")
    assert trace.read_text(encoding="utf-8") == "这不是一段 JSON"


def test_a_wrong_shape_in_the_trace_is_also_an_error(trace: Path) -> None:
    """一段合法 JSON、但栏位不对，同样不放过：清空不是"容错"，是把坏数据藏起来。"""
    trace.parent.mkdir(parents=True)
    trace.write_text('[{"code": "600519"}]', encoding="utf-8")
    with pytest.raises(ValidationError):
        make_client(trace).post("/api/recent", json={"code": "300750"})


def test_the_trace_is_plain_readable_json(trace: Path) -> None:
    """盘上是 UTF-8 明文数组，汉字不转义：代价四认下来的形状，写清楚比修好更值钱。

    `ensure_ascii=False` 漏掉的话文件照样能读，只是变成一串 `\\u8d35`——人就没法直接看了，而
    "人能直接看"是这一格选 JSON 而不是选二进制的全部理由。
    """
    make_client(trace).post("/api/recent", json={"code": "600519"})
    raw = trace.read_text(encoding="utf-8")
    assert "贵州茅台" in raw
    assert "\\u" not in raw


def test_the_trace_stamps_an_unambiguous_instant(client: TestClient) -> None:
    """`at` 是带 UTC 偏移的时刻（决定 4：留痕要给人看"多久之前"）。

    断的是"就是刚刚"，不是"就是今天"：本机 CST 比 UTC 快八小时，拿 `date.today()`（本地）去比
    一个 UTC 串，每天 00:00–07:59 之间必红——我第一次跑就是这么红的，而它在 23 点的那次绿里
    完全看不出来。日期这一格判不了"多久之前"，只有"离现在多久"能判。
    """
    entry = client.post("/api/recent", json={"code": "600519"}).json()["entries"][0]
    when = datetime.fromisoformat(entry["at"])
    assert when.tzinfo is UTC
    assert abs(datetime.now(UTC) - when) < timedelta(minutes=5)


# ── 进程入口：从盘上装配 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("host", "port", "want_host", "want_port"),
    [(None, None, "127.0.0.1", 8000), ("0.0.0.0", "8123", "0.0.0.0", 8123)],
)
def test_the_entry_point_assembles_from_the_published_data(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    trace: Path,
    host: str | None,
    port: str | None,
    want_host: str,
    want_port: int,
) -> None:
    """`zx-site` 把零件从盘上接起来，并且默认只绑本机（ADR-0006：公网在反代那侧）。

    判法还是走请求：装配错的写法有一半能"起来"，而起得来的那一半未必读到了东西。三样零件各有
    一条断言——字典来自主数据快照（决定 1：不联网抓）、模板表来自仓库里那份 YAML（06 §八-2
    "改配置即生效"的落点）、留痕落在数据根下（不是仓库、不是 cwd）。日历那条 fail-closed 判
    `until`：没截止日期，"覆盖不到今天"就会变成静默判空（ADR-0012 决定 2）。
    """
    served: dict[str, object] = {}
    untils: list[date] = []

    def fake_run(app: FastAPI, **kwargs: object) -> None:
        served["app"] = app
        served.update(kwargs)

    def fake_load_calendar(*, until: date | None = None) -> TradingCalendar:
        assert until is not None, "日历没钉截止日期：覆盖不到今天就会静默判空"
        untils.append(until)
        return CALENDAR

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(api, "load_calendar", fake_load_calendar)
    monkeypatch.setenv(api.TOKEN_ENV, TOKEN)
    for name, value in (("ZX_SITE_HOST", host), ("ZX_SITE_PORT", port)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    api.main([])

    assert (served["host"], served["port"]) == (want_host, want_port)
    assert untils == [date.today()]
    app = served["app"]
    assert isinstance(app, FastAPI)
    served_client = TestClient(app, headers=AUTH)
    assert served_client.get("/api/search", params={"q": "600"}).json()["hits"]
    assert len(served_client.get("/api/templates").json()["templates"]) == 5
    served_client.post("/api/recent", json={"code": "600519"})
    assert trace == tmp_path / "site" / "recent.json"
    assert trace.is_file()


def test_an_unknown_command_line_argument_stops_the_process_before_it_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`zx-site --port 8765` 是"这条命令写错了"，不是"起了个 8000 的服务"（独立审计 M1）。

    这个入口没有可调的 flag（全在环境变量里），所以多出来的一律算笔误。忽略它的代价不在命令行上，
    在 systemd 里：照别的服务的习惯写一句 `--port`，进程报 started，而端口对不上反代，外面看到的
    是 502。判两件事——非零退出，以及 `uvicorn.run` 一次都没被叫到：先判参数再起服务，才有"没起来"。
    """
    served: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **_kwargs: served.append(app))

    with pytest.raises(SystemExit) as gone:
        api.main(["--port", "8765"])

    assert gone.value.code == 2
    assert served == [], "服务照起了：那句 --port 被吃了"


#: 改 YAML 追加的那一条：这个名字在任何 `.py` 里都没出现过，`days: 2` 与真表的 `days: 120`
#: 差一个量级，于是"生效没生效"在响应里看得见。只有日线——`root` 夹具往 tmp 里只落了三天日线。
EXTRA_ROW = """
  - name: 只看日线
    status: ready
    role: 你是一位只看量价的日内交易员
    task: 判断下一个交易日的方向
    data:
      - {dataset: daily, days: 2}
    format: markdown
    fields: [close, volume]
    adjust: backward
    output: |
      一句话结论，价位要能对得上表里某一天。
"""


def test_a_new_yaml_row_reaches_the_prompt_with_no_code_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, trace: Path
) -> None:
    """06 §八-2：改 YAML 即生效，不改代码。判的是"站点只认那份文件"这句话。

    在真表上改，而不是另写一份干净的：那才是用户将来的动作。而且**加一条**比改一条更强——
    `site/` 下没有任何一处认识「只看日线」，它出现在列表里只有一个来路：那份文件被读了。
    """
    shipped = config.prompt_templates_file()
    edited = tmp_path / "prompt_templates.yaml"
    edited.write_text(shipped.read_text(encoding="utf-8") + EXTRA_ROW, encoding="utf-8")

    served: dict[str, object] = {}

    def fake_run(app: FastAPI, **_kwargs: object) -> None:
        served["app"] = app

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(api, "load_calendar", lambda **_kwargs: CALENDAR)
    monkeypatch.setattr(config, "prompt_templates_file", lambda **_kwargs: edited)
    monkeypatch.setenv(api.TOKEN_ENV, TOKEN)

    api.main([])
    app = served["app"]
    assert isinstance(app, FastAPI)
    shell = TestClient(app, headers=AUTH)

    names = [t["name"] for t in shell.get("/api/templates").json()["templates"]]
    want = [t.name for t in templates.load(shipped).templates]
    assert names == [*want, "只看日线"], "列表不是「真表 + 追加那条」：站点读的不是这份文件"

    body = shell.post(
        "/api/prompt",
        json={"code": "600519", "template": "只看日线", "as_of": "2024-01-04"},
    ).json()
    text = body["text"]
    assert "你是一位只看量价的日内交易员" in text, "角色不是新表里那句：模板没从文件来"
    assert "### 日K（2 个交易日）" in text
    assert "| 88.00 |" in text and "| 96.00 |" in text
    assert "80.00" not in text, "days: 2 没起作用：三天的数全进了表"
    assert body["warn"] is None
    shell.post("/api/recent", json={"code": "600519"})
    assert [e["code"] for e in json.loads(trace.read_text(encoding="utf-8"))] == ["600519"]
