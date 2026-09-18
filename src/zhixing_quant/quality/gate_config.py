"""门禁规则表的装载与校验（04 §二；ADR-0002 第 1 条"规则配置驱动"）。

引擎只认这张表：加规则 = 写谓词 + 加一节 `[[rule]]`；停用规则、改阈值 = 只改配置。
装载阶段就把所有引用解析完（函数名写错、level 拼错、编号重复都当场抛错），不留到
跑到第 3000 行数据时才炸——那种炸法最坏，前 2999 行已经按错的规则判完了。

`enabled` 是给"某条规则在某个阶段不适用"用的（如 04 §二 R009 标注日线阶段暂不启用），
而不是把配置删掉：删了就没人知道这条规则存在过、为什么停的。
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


@dataclass(frozen=True)
class GateConfig:
    defaults: Mapping[str, Any]
    rules: tuple[RuleSpec, ...]

    def enabled_rules(self, kind: str) -> tuple[RuleSpec, ...]:
        return tuple(r for r in self.rules if r.kind == kind and r.enabled)

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
    return RuleSpec(
        id=str(raw["id"]),
        kind=kind,
        level=level,
        callable_path=str(raw["callable"]),
        predicate=resolve_callable(str(raw["callable"])),
        params=params,
        enabled=bool(raw.get("enabled", True)),
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
