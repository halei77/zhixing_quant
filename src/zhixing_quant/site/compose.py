"""把模板与已渲染的数据拼成一份提示词（06 §六 固定头部、ADR-0011 决定 4）。

顺序是定死的：角色 → 反幻觉指令 → 任务 → 数据 → 输出要求。反幻觉指令排在**所有数据之前**，
因为大模型是顺序读的——先告诉它下面那些数字是唯一允许的出处，再给数字。

固定头部由代码给，模板只能往"输出要求"后面追加。这不是不信任模板作者，是 06 §八-4 那条验收
（"所有模板输出均含反幻觉固定头部"）要能被机器判定：写在 YAML 里的东西，忘了写就没有，而这条
恰恰是唯一一条"漏了就正中要害"的——站点的定位就是防幻觉（06 §一）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from zhixing_quant.site.templates import Template

#: 06 §六 那三条，逐字对着文档写。所有模板共用同一段，没有模板级变体。
ANTI_HALLUCINATION = (
    "【数据纪律】\n"
    "1. 只允许使用本提示词内提供的数据；\n"
    "2. 禁止使用训练记忆中的任何股价、财务数字；\n"
    "3. 判断所需的数字不在下面这些数据里时，明确写出「数据未提供」，不得回忆、不得估算。"
)


class EmptyPrompt(ValueError):
    """一份没有数据的提示词：那正好是 06 §一 要防的那种东西——指令齐全、数字全靠编。"""


@dataclass(frozen=True)
class Section:
    """一段已经渲染好的数据。`title` 是小标题（"日K（120 个交易日）"），`body` 由组件自己出。"""

    title: str
    body: str


def assemble(template: Template, sections: Sequence[Section]) -> str:
    """拼出最终文本。调用方给什么数据就是什么数据，这里不取数、不补数（决定 1 的边界）。"""
    if not sections:
        raise EmptyPrompt(
            f"「{template.name}」一份数据都没拿到就不生成：与其让大模型凭训练记忆作答，"
            "不如让站点把「今天取不到数」说在前面"
        )
    blocks = [template.role, ANTI_HALLUCINATION, f"【任务】{template.task}"]
    for section in sections:
        blocks.append(f"### {section.title}\n{section.body}")
    blocks.append(f"【输出要求】\n{template.output.rstrip()}")
    return "\n\n".join(blocks)
