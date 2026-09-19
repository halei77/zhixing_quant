"""门禁规则表的装载与校验（04 §二；ADR-0002 第 1 条"规则配置驱动"）。

引擎只认这张表：加规则 = 写谓词 + 加一节 `[[rule]]`；停用规则、改阈值 = 只改配置。
装载阶段就把所有引用解析完（函数名写错、level 拼错、编号重复都当场抛错），不留到
跑到第 3000 行数据时才炸——那种炸法最坏，前 2999 行已经按错的规则判完了。

`enabled` 与 `grains` 是两种不同的"不跑"，别混用。`enabled = false` 说的是"这个暂缓，回头打开"；
`grains` 说的是"这条规则对这种粒度永远不成立"。日线一行一天、分钟一行一根K线，R004 那种
"相对昨收"的判据喂到分钟批里，它看到的"上一行"是上一根K线——不是同一回事，不是阈值能救的，
所以 R009 走 `grains` 而不是回到 `enabled`（ADR-0009 决定 5）。缺省是全粒度，只有例外规则要写它
（哪些是例外、为什么，04 §二 与 ADR-0009 决定 5）。
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

KINDS = ("row", "batch")
LEVELS = ("reject", "warn", "fatal")
#: 数据粒度。值由**行自己**说出来（`BarDraft.ts` 在不在），不由调用方配置——配错的样子是
#: "分钟数据按日线规则判了一遍，全绿"，那种绿比红糟。
GRAINS = ("daily", "minute")
#: 缺省粒度：一张没写 `grains` 的表 = 所有粒度都跑。
ALL_GRAINS: tuple[str, ...] = GRAINS
#: `[defaults]` 里必须有的项：多条规则共用（R004 与 R007 的容差就是这一项），
#: 少一个键就是运行到第 3000 行才 KeyError，所以装载时先炸。
REQUIRED_DEFAULTS = ("tolerance_pct",)

RuleParams = Mapping[str, Any]


class GateConfigError(ValueError):
    """配置自身不成立：装载即失败，不带病跑批。"""


def resolve_callable(dotted: str) -> Callable[..., object]:
    """`pkg.mod:name` → 函数对象。空模块名/空属性名/纯空白都算写错，不当"没配"放过。"""
    module_name, sep, attr = dotted.partition(":")
    module_name, attr = module_name.strip(), attr.strip()
    if not sep or not module_name or not attr:
        raise GateConfigError(f"callable 要写成 '模块:函数'，收到 {dotted!r}")
    try:
        fn: object = getattr(import_module(module_name), attr)
    except (ImportError, AttributeError) as exc:
        raise GateConfigError(f"callable {dotted!r} 解析失败：{exc}") from exc
    if not callable(fn):
        raise GateConfigError(f"callable {dotted!r} 指向的不是函数")
    return fn


@dataclass(frozen=True)
class RuleSpec:
    id: str
    kind: str
    level: str
    callable_path: str
    predicate: Callable[..., object] = field(repr=False)
    params: RuleParams = field(default_factory=dict)
    enabled: bool = True
    #: 这条规则适用的粒度（ADR-0009 决定 5）。空 = 不跑任何粒度，所以装载时按 `ALL_GRAINS` 兜。
    grains: tuple[str, ...] = ALL_GRAINS

    def applies_to(self, grain: str) -> bool:
        return self.enabled and grain in self.grains


@dataclass(frozen=True)
class GateConfig:
    defaults: Mapping[str, Any]
    rules: tuple[RuleSpec, ...]

    def enabled_rules(self, kind: str, grain: str = "daily") -> tuple[RuleSpec, ...]:
        return tuple(r for r in self.rules if r.kind == kind and r.applies_to(grain))

    def rule(self, rule_id: str) -> RuleSpec:
        """按编号取规则。查不到抛 KeyError，不让 StopIteration 冒出来——
        那玩意从 property 里抛出时，调用方看到的是"值为空"，不是"编号写错"。
        """
        for spec in self.rules:
            if spec.id == rule_id:
                return spec
        raise KeyError(f"门禁规则表无编号 {rule_id}")

    @property
    def enabled_ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.rules if r.enabled)

    def ids_for(self, grain: str) -> tuple[str, ...]:
        """这个粒度上真正会跑的规则编号。报错信息用它：一条被拒收的分钟K线，说"已启用 R001…R010"
        是假的——分钟批根本不跑 R004。
        """
        return tuple(r.id for r in self.rules if r.applies_to(grain))


def _one_rule(raw: Mapping[str, Any], index: int, defaults: Mapping[str, Any]) -> RuleSpec:
    where = f"第 {index + 1} 节 [[rule]]"
    for key in ("id", "kind", "level", "callable"):
        if key not in raw:
            raise GateConfigError(f"{where} 缺字段 {key!r}")
    kind, level = str(raw["kind"]), str(raw["level"])
    if kind not in KINDS:
        raise GateConfigError(f"{where} kind={kind!r} 不在 {KINDS}")
    if level not in LEVELS:
        raise GateConfigError(f"{where} level={level!r} 不在 {LEVELS}")
    # defaults 先铺、规则段覆盖：板块阈值这类多项配置因此只有一处真值（04 §二 R007
    # "与 R004 板块阈值联动"就是这个意思，抄两份迟早抄漂）。
    params: dict[str, Any] = {**defaults, **dict(raw.get("params", {}))}
    grains = raw.get("grains")
    if grains is None:
        grain_set = ALL_GRAINS
    else:
        if isinstance(grains, str) or not isinstance(grains, Sequence):
            raise GateConfigError(f"{where} grains 要写成数组，收到 {grains!r}")
        grain_set = tuple(str(g) for g in grains)
        unknown = sorted(set(grain_set) - set(GRAINS))
        if unknown:
            raise GateConfigError(f"{where} grains 含未知粒度 {unknown}，可选 {GRAINS}")
        if not grain_set:
            raise GateConfigError(f"{where} grains 是空数组：一条哪都不跑的规则应该 enabled=false")
    return RuleSpec(
        id=str(raw["id"]),
        kind=kind,
        level=level,
        callable_path=str(raw["callable"]),
        predicate=resolve_callable(str(raw["callable"])),
        params=params,
        enabled=bool(raw.get("enabled", True)),
        grains=grain_set,
    )


def parse(data: Mapping[str, Any]) -> GateConfig:
    defaults = dict(data.get("defaults", {}))
    missing = [key for key in REQUIRED_DEFAULTS if key not in defaults]
    if missing:
        raise GateConfigError(f"[defaults] 缺 {','.join(missing)}：多项阈值靠它做单一来源")
    raw_rules: Sequence[Any] = data.get("rule", [])
    if not raw_rules:
        raise GateConfigError("一张 [[rule]] 都没有：门禁等于没有门禁，不许静默放行")
    rules = tuple(_one_rule(r, i, defaults) for i, r in enumerate(raw_rules))
    seen: set[str] = set()
    for spec in rules:
        if spec.id in seen:
            raise GateConfigError(f"规则编号重复：{spec.id}")
        seen.add(spec.id)
    if not any(r.level == "fatal" and r.enabled for r in rules):
        raise GateConfigError(
            "没有**启用的** FATAL 规则：把 R006/R010 关掉等于给带病数据开绿灯，不许"
        )
    return GateConfig(defaults=defaults, rules=rules)


def load(path: Path) -> GateConfig:
    """从 TOML 装载。文件不存在直接抛，不返回"空规则表"兜过。"""
    if not path.is_file():
        raise GateConfigError(f"门禁规则表不存在：{path}")
    with path.open("rb") as fh:
        return parse(tomllib.load(fh))
