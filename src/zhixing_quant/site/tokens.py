"""token 估算：一条写死的字符启发式，不引分词器（ADR-0011 决定 6）。

为什么不引 tokenizer：站点面向"粘到任意大模型"，而词表与目标模型绑定；多数词表还要联网取，
CI 里就多一个网络依赖。06 §八-5 要的是"超阈值警告"，不是计费，所以一个确定、可复算、量级
正确的数就够了——代价是它不等于任何一家的真实 token 数（ADR-0011 代价一）。

方向是**宁可高估**：分段各自向上取整，混合文本会比整段连续算更多。低估会让用户在临近阈值时
以为安全，高估只是提前一句警告。
"""

from __future__ import annotations

#: CJK 码位段：假名、统一表意文字及其扩展、全角标点与字符。一个汉字≈一个 token 是各家
#: 分词器在中文上的通常落点（BPE 偶有两个字合一段的，那是少数，且方向是低估，所以不取）。
CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x2E80, 0x2EFF),  # CJK 部首补充
    (0x3000, 0x303F),  # CJK 符号与标点（「」、。〈〉都在这段）
    (0x3040, 0x30FF),  # 平假名、片假名
    (0x3400, 0x4DBF),  # 扩展 A
    (0x4E00, 0x9FFF),  # 统一表意文字
    (0xF900, 0xFAFF),  # 兼容表意文字
    (0xFF00, 0xFF60),  # 全角字符（全角数字与字母也算一个）
    (0x20000, 0x2A6DF),  # 扩展 B（生僻字、部分退市票的简称里有）
)

#: 非 CJK 每 4 个字符算 1 token：英文实词平均 4–5 字符含空格，这是 BPE 在纯 ASCII 上的常见值。
ASCII_CHARS_PER_TOKEN = 4


def estimate(text: str) -> int:
    """这段文字的 token 量级。空串是 0，不是 1——没有内容就没有 token。"""
    tokens = 0
    pending = 0
    for char in text:
        if _is_cjk(char):
            tokens += _ceil(pending) + 1
            pending = 0
        else:
            pending += 1
    return tokens + _ceil(pending)


def over(text: str, limit: int) -> bool:
    """超没超阈值。**严格大于**：正好等于阈值不算超，06 §八-5 说的是"超阈值警告"。"""
    return estimate(text) > limit


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(start <= code <= end for start, end in CJK_RANGES)


def _ceil(count: int) -> int:
    return -(-count // ASCII_CHARS_PER_TOKEN)
