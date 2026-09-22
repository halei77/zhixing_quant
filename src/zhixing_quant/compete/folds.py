"""Walk-forward 切分（07 §5.3）。

三段切分的形状是协议定死的：

- **测试段只许用一次**——它永远是最末尾 `test_frac` 那一截，选择过程（网格 × 验证折）
  碰不到它，`test_days()` 与 `selection_days()` 的不相交就是这条纪律的机器形态。
- 验证折**扩张窗**：第 i 折的训练 = 头部前 i+1 块、验证 = 第 i+1 块。每折的训练都比
  上一折长，"用一段历史定终身"被折数摊掉；段是交易日序列上的连续切片，不许交叉。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

TEST_FRAC = 0.2
MIN_SELECTION_DAYS = 40
MIN_TEST_DAYS = 10


@dataclass(frozen=True)
class Fold:
    """一折：训练日 + 验证日。训练总在验证之前（扩张窗的含义）。"""

    index: int
    train: tuple[date, ...]
    valid: tuple[date, ...]


@dataclass(frozen=True)
class Selection:
    """一次竞争的完整切分。`folds` 供参数选择，`test` 只许最后评一次。"""

    folds: tuple[Fold, ...]
    test: tuple[date, ...]


def _chunks(days: tuple[date, ...], parts: int) -> tuple[tuple[date, ...], ...]:
    """尽量均分成 `parts` 份（前面的段多一天），不许有空段。"""
    size, extra = divmod(len(days), parts)
    out = []
    at = 0
    for i in range(parts):
        take = size + (1 if i < extra else 0)
        out.append(days[at : at + take])
        at += take
    return tuple(out)


def split(days: tuple[date, ...], *, folds: int = 4) -> Selection:
    """切。天数不足时响，不切出一个折里只有两天的"统计"，那不是样本是装饰。"""
    if len(days) < MIN_SELECTION_DAYS + MIN_TEST_DAYS:
        raise ValueError(
            f"盘上只有 {len(days)} 个交易日：选择段至少 {MIN_SELECTION_DAYS} + "
            f"测试段至少 {MIN_TEST_DAYS}，先补数据再谈竞争"
        )
    if folds < 2:
        raise ValueError(f"folds = {folds}：一折的 walk-forward 就是单段定终身，§5.3 禁止")
    cut = int(len(days) * (1 - TEST_FRAC))
    cut = max(MIN_SELECTION_DAYS, min(cut, len(days) - MIN_TEST_DAYS))
    head, test = days[:cut], days[cut:]
    chunks = _chunks(head, folds + 1)
    folds_out = tuple(
        Fold(index=i, train=tuple(d for c in chunks[: i + 1] for d in c), valid=chunks[i + 1])
        for i in range(folds)
    )
    return Selection(folds=folds_out, test=test)


def extended_test(days: tuple[date, ...]) -> tuple[date, ...]:
    """5.4-4 的"向前扩一折"：测试段从末尾 20% 扩到末尾 40%（扩幅 ≈ 一折宽）。

    只许扩一次——再扩就该回到"补数据"而不是继续烧选择段。扩过的测试段在报告里必须
    标注：它吃掉了一段本属选择面的历史，同轮其他候选的测试段没有跟着扩，横向不可比。
    """
    cut = max(int(len(days) * (1 - 2 * TEST_FRAC)), MIN_SELECTION_DAYS)
    return days[cut:]


def selection_days(selection: Selection) -> tuple[date, ...]:
    """选择段 = 所有折的训练 ∪ 验证（并集即头部全部；给报告核"测试段没被碰过"用）。"""
    days: set[date] = set()
    for fold in selection.folds:
        days.update(fold.train)
        days.update(fold.valid)
    return tuple(sorted(days))
