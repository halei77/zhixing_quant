"""涨跌停制度：板块档位与新股无限制窗口（04 §二 R004 与 ADR-0010 决定 4 共用的一份）。

放 domain 而不是留在 `quality/rules.py`，是因为回测引擎要的是**同一个数**：这只票这天最多能
涨多少。门禁拿它加 0.5% 容差判"这数据是不是坏的"，引擎拿它不加容差判"这一笔单子成交得了吗"。
共用的是**档位**，不是**判定**——把容差也一起递过去，回测就会把一段测量噪声当成制度边界，
在真实涨停价上判"还能买"。所以这两个函数只回答档位与窗口，容差留在门禁那一侧。

判不出来的形状（代码认不出板块、主数据缺席）由调用方各自处置：门禁拒收整行，回测拒单并记原因。
这里不替任何一方决定"判不了算什么"。
"""

from __future__ import annotations

from collections.abc import Mapping

from zhixing_quant.domain.symbol import Board


def limit_pct(board: Board, *, is_st: bool, limits_pct: Mapping[str, float]) -> float:
    """该票该日的涨跌幅上限（百分点，**未加任何容差**）。

    北交所不看 ST 帽（04 §二 定的口径）：30% 与 5% 来自两套规则，同一只票不会同时要满足，
    按 30% 判是 04 的结论而不是这里的推断。其余板块 ST 一档、板块一档。
    """
    if board is Board.BSE:
        return limits_pct["bse"]
    return limits_pct["st"] if is_st else limits_pct[board.value]


def new_listing_days(board: Board, no_limit_days: Mapping[str, int]) -> int:
    """该板块"上市后不设涨跌幅限制"的交易日数。表里没写 = 0 = 没有这个窗口。

    按板块取而不是写死 5：北交所是"首日不设、此后 30%"，与注册制主板不同口径（04 §二）。
    数出"第几个交易日"这件事在 `SecurityMaster.listing_day_index`，按交易日而不是日历日数。
    """
    return int(no_limit_days.get(board.value, 0))
