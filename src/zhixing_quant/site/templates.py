"""提示词模板：`config/prompt_templates.yaml` 是唯一真源，代码不兜底（ADR-0011 决定 2）。

为什么配置驱动而不是写死在代码里：06 §五 的原话是"改配置即生效，禁止改代码"，而 06 §八-2
把这句话变成了验收项。更深一层是——模板的"默认组合"是草案值（06 §五 底下那句），草案值改一次
就走一次代码评审，评审的人会以为组装的口径变了，而其实只是"日K从 120 天改成 250 天"。

fail-closed 的粒度：少一个键、多一个没听过的字段、名字重复，全部在装载时响。带着半份模板
生成提示词是最坏的一种"能跑"——站点会把一个没人核对过的东西交给大模型，而 06 §一 立的正是
"可信数据打包机"这块牌子。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import yaml

#: 价格口径，与 `storage.query.Adjust` 同三个词（那里是查询层的入参，这里是模板的声明）。
Adjust = Literal["raw", "backward", "forward"]
#: 表格形状。`csv` 就是 06 §六 说的"token 紧张时启用"的紧凑模式。
Format = Literal["markdown", "csv"]
#: `pending` = 模板已经登记，但它要的数据组件还没落地（一期只有K线与基本信息）。
Status = Literal["ready", "pending"]

ADJUSTS: tuple[str, ...] = ("raw", "backward", "forward")
FORMATS: tuple[str, ...] = ("markdown", "csv")
STATUSES: tuple[str, ...] = ("ready", "pending")
#: 06 §四 K线组件的可选字段。`turnover` 的分子在K线上、分母（流通股本）不在，所以"请求了
#: 它却没给分母"是拒绝而不是悄悄少一列（ADR-0011 决定 5）。
FIELDS: tuple[str, ...] = ("open", "high", "low", "close", "volume", "amount", "turnover")
#: 固定头的 dataset 名与字段清单（ADR-0022 决定 1）。它不是盘上的表——快照由
#: `prompt.build` 在生成时现拼（各序列在 as_of 的最新可见行），落盘会造第二个「最新」事实源。
#: 清单同时是 `TABLE_DATASETS` 的可选字段与 `ALWAYS_DATA` 的默认勾选，两处指同一份元组。
FUNDAMENTAL_HEAD = "fundamental_head"
FUNDAMENTAL_HEAD_FIELDS: tuple[str, ...] = (
    "close",
    "total_mv",
    "circ_mv",
    "pe_ttm",
    "pb",
    "ps_ttm",
    "dv_ttm",
    "turnover_rate",
)

#: `forward_pe` 的字段四列（ADR-0022 决定 2 定死）：close=不复权收盘、fwd_pe=现算远期 PE、
#: est_period=分母的预测报告期（模型要知道 22.4 倍是对哪个盈利说的）、est_asof=该预测的
#: 发布日（把点时可见性印在表里）。清单同时是模板装载的可选字段——四列都是规格的一部分。
FORWARD_PE_FIELDS: tuple[str, ...] = ("close", "fwd_pe", "est_period", "est_asof")

#: `fina_trend` 的字段五列（ADR-0022 同手法，06 §十-5 的 ROE/营收与净利增速）：前三列是
#: 源指标**原名**（roe_waa=加权 ROE、tr_yoy=营业总收入同比、netprofit_yoy=净利润同比——
#: 原名端到端可追溯，不改名不改义）；fina_period/fina_asof 把点时可见性印在表里（所用
#: 报告期与它的首披日，必不晚于行日期）。清单同时是模板装载的可选字段——五列都是规格。
FINA_TREND_FIELDS: tuple[str, ...] = (
    "roe_waa",
    "tr_yoy",
    "netprofit_yoy",
    "fina_period",
    "fina_asof",
)

#: 参考表类 dataset（ADR-0016）：字段清单按表各自声明，装载时按 dataset 类别校验。
TABLE_DATASETS: dict[str, tuple[str, ...]] = {
    "fundamental_head": FUNDAMENTAL_HEAD_FIELDS,
    "forward_pe": FORWARD_PE_FIELDS,
    "fina_trend": FINA_TREND_FIELDS,
    "daily_basic": (
        "close",
        "pe",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "dv_ttm",
        "total_mv",
        "circ_mv",
        "turnover_rate",
    ),
    # forecast 的 ann/end 两列每行都印（行身份），不进可选清单
    "forecast": (
        "type",
        "p_change_min",
        "p_change_max",
        "net_profit_min",
        "net_profit_max",
        "summary",
    ),
    "stk_limit": ("up_limit", "down_limit"),
    "index_daily": ("open", "high", "low", "close", "volume", "amount"),
}


class TemplateConfigError(ValueError):
    """模板不成形状：装载即失败，不许带着一份可疑配置去生成提示词。"""


@dataclass(frozen=True)
class Selection:
    """一段数据选择：哪个 dataset、取几个**交易日**。

    天数按交易日数，与 `storage.query.Cover.days` 同一口径（06 §四 说的"每周期独立选天数"要的
    是"能切出多少根样本"，不是日历跨度）。换算成区间起点是 5b 的事：纯层不感知盘上深度。

    `fields` 是 ADR-0016 加的：参考表条目**必须**自带列清单（估值列与K线列是两套，
    模板级的那份服务不了两种表），Bar 条目为 None（用模板级 fields）。
    """

    dataset: str
    days: int
    fields: tuple[str, ...] | None = None


#: 固定头在**代码侧**的真形（ADR-0022 决定 1）。两条装载路径共用它：
#: `parse` 把 YAML 的 `always_data` 合并进每条模板；`with_always_data` 给不经 YAML 的
#: 直构 `Template`（ADR-0017 自定义组合，`api._custom_template` 就是这么造的）兜底补头。
#: YAML 那份必须与这里逐项相等——漂了由 `test_site_templates` 的漂移测试判红。
ALWAYS_DATA: tuple[Selection, ...] = (
    Selection(dataset=FUNDAMENTAL_HEAD, days=1, fields=FUNDAMENTAL_HEAD_FIELDS),
)


def with_always_data(data: Sequence[Selection]) -> tuple[Selection, ...]:
    """把固定头并进一段数据选择：缺则补在最前，已有则原样（模板与 `always_data` 撞了以在场者为准）。

    「不可取消」的机器形态就是这一条：合并按 dataset 名判，模板侧没有任何语法能把头摘掉
    （YAML 装载同理，见 `parse`）。组装层不硬插正文——补的是**选择**，取数照旧走 `build`
    的逐段循环，空了照旧出 `NO_DATA_NOTE`（ADR-0020），不会把取数失败变成隐式行为。
    """
    present = {selection.dataset for selection in data}
    missing = tuple(item for item in ALWAYS_DATA if item.dataset not in present)
    return (*missing, *data)


@dataclass(frozen=True)
class Template:
    """一条模板：角色 + 任务 + 数据组合 + 输出要求（06 §五 的"一套选择逻辑 + 一整套结构"）。"""

    name: str
    role: str
    task: str
    data: tuple[Selection, ...]
    output: str
    format: Format
    fields: tuple[str, ...]
    adjust: Adjust
    status: Status
    waiting_on: str


@dataclass(frozen=True)
class Config:
    """整张模板表，加上那一个 token 阈值。"""

    token_warn_above: int
    templates: tuple[Template, ...]

    def by_name(self, name: str) -> Template:
        """按名字取模板。名字在装载时已保证唯一，所以找不到就是调用方给错了名字。"""
        for template in self.templates:
            if template.name == name:
                return template
        raise TemplateConfigError(
            f"模板表里没有「{name}」，现有的：{'、'.join(t.name for t in self.templates)}"
        )


def load(path: Path) -> Config:
    """从 YAML 装载。文件不存在直接抛——"没有模板"不等于"有一个通用模板"。"""
    if not path.is_file():
        raise TemplateConfigError(
            f"模板表不存在：{path}。缺它就生成，等于用一份没人认领的结构拼提示词"
        )
    with path.open("rb") as handle:
        return parse(_mapping(yaml.safe_load(handle), where="模板表根"))


def parse(data: Mapping[str, Any]) -> Config:
    limit = _int(_require(data, "token_warn_above", where="模板表根"), "token_warn_above")
    if limit <= 0:
        raise TemplateConfigError(f"token_warn_above 要大于 0，现在是 {limit}")
    always = _always_data(data)
    templates = tuple(_template(row, index, always=always) for index, row in enumerate(_rows(data)))
    _unique_names(templates)
    return Config(token_warn_above=limit, templates=templates)


def _always_data(data: Mapping[str, Any]) -> tuple[Selection, ...]:
    """顶层 `always_data`（ADR-0022 决定 1）：每条模板强制携带的数据选择，装载时合并进去。

    键本身**可缺省**——缺了装载不响，由 06 §八-4 手法的那条 CI 断言（每条 ready 模板生效后
    都含 `fundamental_head`）判红：删掉这个键，站点还起得来，但验收测试立刻红，这正是
    「YAML 声明 + CI 断言」要的失败位置。写歪了（字段选错、天数非正）则照常装载即拒。
    """
    raw = data.get("always_data")
    if raw is None:
        return ()
    return _selections(raw, where="always_data")


def _rows(data: Mapping[str, Any]) -> Sequence[Any]:
    rows = _require(data, "templates", where="模板表根")
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise TemplateConfigError("templates 要是一个列表")
    if not rows:
        raise TemplateConfigError("templates 一条都没有：一张空模板表不是「还没上线」，是配错了")
    return rows


def _template(row: object, index: int, *, always: Sequence[Selection]) -> Template:
    where = f"第 {index} 条模板"
    data = _mapping(row, where)
    status = _word(_require(data, "status", where=where), STATUSES, "status", where=where)
    waiting_on = _text(data.get("waiting_on", ""), "waiting_on", where=where)
    if status == "pending" and not waiting_on:
        raise TemplateConfigError(
            f"{where} 是 pending 却没写 waiting_on："
            "一条等着的组件要说清在等什么，否则半年后没人知道"
        )
    format = _word(_require(data, "format", where=where), FORMATS, "format", where=where)
    adjust = _word(_require(data, "adjust", where=where), ADJUSTS, "adjust", where=where)
    own = _selections(_require(data, "data", where=where), where=where)
    present = {selection.dataset for selection in own}
    # 固定头合并进**每条**模板（ready 与 pending 都合，pending 反正生成不了）：模板自己
    # 已声明同名 dataset 时以在场者为准，不重复——两条同名选择会渲染出两张同名表。
    merged = (*(item for item in always if item.dataset not in present), *own)
    template = Template(
        name=_text(_require(data, "name", where=where), "name", where=where),
        role=_text(_require(data, "role", where=where), "role", where=where),
        task=_text(_require(data, "task", where=where), "task", where=where),
        data=merged,
        output=_text(_require(data, "output", where=where), "output", where=where),
        format=cast("Format", format),
        fields=_fields(_require(data, "fields", where=where), where=where),
        adjust=cast("Adjust", adjust),
        status=cast("Status", status),
        waiting_on=waiting_on,
    )
    for key in ("name", "role", "task", "output"):
        # 只判 `.strip()` 不判真假：YAML 里 `role: >` 换行留空格是笔误，不是"这条模板没有角色"。
        if not getattr(template, key).strip():
            raise TemplateConfigError(
                f"{where} 的 {key} 是空的（或只有空白）：空的{key}拼出来的提示词没有意义"
            )
    if not own:
        # 判模板**自己**声明的 data，不看合并后的：固定头是站点硬给的，一条自己什么都不选、
        # 靠 always_data 撑成"有一段数据"的模板是笔误，不是「只要最新基本面」。
        raise TemplateConfigError(f"{where} 一条数据选择都没有：那它取什么进提示词？")
    return template


def _selections(rows: object, *, where: str) -> tuple[Selection, ...]:
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise TemplateConfigError(f"{where} 的 data 要是一个列表")
    out: list[Selection] = []
    for row in rows:
        item = _mapping(row, f"{where} 的一段数据")
        dataset = _text(_require(item, "dataset", where=where), "dataset", where=where)
        days = _int(_require(item, "days", where=where), "days")
        if days <= 0:
            raise TemplateConfigError(f"{where} 要 {dataset} 的 {days} 天：天数得是正数")
        fields: tuple[str, ...] | None = None
        if dataset in TABLE_DATASETS:
            # 参考表条目（ADR-0016）：列清单必须自带，且只许选这张表自己的列。
            given = item.get("fields")
            if given is None:
                raise TemplateConfigError(
                    f"{where} 的参考表 {dataset} 要自带 fields：估值列与K线列是两套，"
                    "模板级那份服务不了它"
                )
            names = tuple(
                _text(x, "字段名", where=where)
                for x in _fields(given, where=where, allowed=TABLE_DATASETS[dataset])
            )
            fields = names
        if any(selection.dataset == dataset for selection in out):
            raise TemplateConfigError(
                f"{where} 的 {dataset} 出现了两次：同一周期给两条深度不是组合，是笔误"
            )
        out.append(Selection(dataset=dataset, days=days, fields=fields))
    return tuple(out)


def _fields(value: object, *, where: str, allowed: tuple[str, ...] = FIELDS) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TemplateConfigError(f"{where} 的 fields 要是一个列表")
    names = tuple(_text(item, "字段名", where=where) for item in value)
    if not names:
        raise TemplateConfigError(f"{where} 的 fields 是空的：一张没有列的表不是表")
    for name in names:
        if name not in allowed:
            raise TemplateConfigError(
                f"{where} 请求了没有的字段 {name!r}，可选：{'、'.join(allowed)}"
            )
    if len(set(names)) != len(names):
        raise TemplateConfigError(f"{where} 的 fields 有重复：同一列印两遍只会让 token 翻倍")
    return names


def _unique_names(templates: Sequence[Template]) -> None:
    seen: set[str] = set()
    for template in templates:
        if template.name in seen:
            raise TemplateConfigError(
                f"模板名字重复：「{template.name}」。站点按名字取模板，重名会让一次点击对应两条东西"
            )
        seen.add(template.name)


def _mapping(value: object, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TemplateConfigError(f"{where} 读不出一个键值对，拿到的是 {type(value).__name__}")
    return {str(key): item for key, item in value.items()}


def _require(data: Mapping[str, Any], key: str, *, where: str) -> Any:
    if key not in data:
        raise TemplateConfigError(f"{where} 少了 {key}：缺哪一项就补哪一项，代码不替你猜")
    return data[key]


def _text(value: Any, key: str, *, where: str) -> str:
    if not isinstance(value, str):
        raise TemplateConfigError(f"{where} 的 {key} 要是一段文字，拿到 {type(value).__name__}")
    return value


def _word(value: Any, allowed: tuple[str, ...], key: str, *, where: str) -> str:
    text = _text(value, key, where=where)
    if text not in allowed:
        raise TemplateConfigError(f"{where} 的 {key} 只认 {'、'.join(allowed)}，给的是 {text!r}")
    return text


def _int(value: Any, key: str) -> int:
    # bool 是 int 的子类：`token_warn_above: true` 会被当成 1 一路走下去，所以在这里挡掉。
    if isinstance(value, bool) or not isinstance(value, int):
        raise TemplateConfigError(f"{key} 要是一个整数，拿到 {value!r}")
    return int(value)
