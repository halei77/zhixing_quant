"""全量重扫工具 `tools/reconcile_history.py`。

它存在的理由是 ADR-0009 补充决定（2026-09-20）那句"改了判据或换了源，全量重扫是必须的一步"——
两份文档里那批实测数字现在都指着它。所以这里测的不是"能不能跑"，而是三件更容易坏的事：
首日为什么不算、"盘上没数据"为什么不等于"干净"、默认票池从哪来。

一个没有测试的 `tools/` 脚本烂过一次（坑 #38：`bench_storage.py` 里写着个不存在的 dataset 名，
没人跑所以没人红）。文档里那批"全量实测"若没人能重跑，它们就跟 agent 自述同级——那是坑 #39 的
形状。
"""

import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import reconcile_history as rh  # noqa: E402  # tools/ 不在包内，按脚本路径导入

from tests.fakes import daily_span, minute_span, snapshot_root  # noqa: E402
from zhixing_quant.storage import layout  # noqa: E402
from zhixing_quant.storage.write import store_bars  # noqa: E402

D1, D2, D3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
#: 一天只落一根 5 分钟K线：合成的开盘取自最早那根、收盘取自最晚那根，一根同时是两者。
TS = "09:35"


def one_bar(day: date, **override: Any) -> Any:
    when = datetime.fromisoformat(f"{day.isoformat()}T{TS}")
    base = minute_span(when, open_=10.0, high=10.2, low=9.9, close=10.0, volume=100.0)
    return base.model_copy(update=override)


def land(symbol: str = "600519", *, volume_d3: float = 100.0, open_d3: float = 10.0) -> None:
    """三天日线 + 三天分钟线，落进 `ZX_DATA_ROOT` 下的干净区。

    D1/D2 两边完全对得上；D3 默认也对得上。两种坏法是参数：`volume_d3` 改日线那一侧的量
    （分钟侧恒为 100），`open_d3` 改日线那一侧的开盘（高低区间跟着放宽，所以它不会顺带把
    `extremes` 也点了——两条判据混在一条断言里就分不出谁是谁）。首日不进 `combos`。
    """
    store_bars(
        [
            daily_span(
                D1,
                symbol=symbol,
                open_=10.0,
                high=10.2,
                low=9.9,
                close=10.0,
                volume=100.0,
                amount=1000.0,
            ),
            daily_span(
                D2,
                symbol=symbol,
                open_=10.0,
                high=10.2,
                low=9.9,
                close=10.0,
                volume=100.0,
                amount=1000.0,
            ),
            daily_span(
                D3,
                symbol=symbol,
                open_=open_d3,
                high=max(10.6, open_d3),
                low=min(9.9, open_d3),
                close=10.0,
                volume=volume_d3,
                amount=1000.0,
            ),
        ],
        dataset=layout.DAILY,
    )
    store_bars(
        [one_bar(D1, symbol=symbol), one_bar(D2, symbol=symbol), one_bar(D3, symbol=symbol)],
        dataset=layout.minute_dataset("5"),
    )


@pytest.fixture(autouse=True)
def isolated_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每个测试一个空的数据根。

    全量重扫读的是"盘上有什么"，所以盘必须是 tmp 里那一份。autouse 而不是让每条测试自己声明
    参数：漏声明的那条会去扫家里 `~/zhixing_data`，然后在真数据上给出一个假绿灯。
    """
    snapshot_root(tmp_path, monkeypatch)


def test_the_first_day_on_disk_is_not_scored() -> None:
    """首日不算：它在盘上可能只有尾巴那一段，拿它跟整天比是探针自己造的偏差。"""
    land()
    assert rh.scan(["5"]).combos == 2


def test_a_volume_shortfall_alone_still_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """只有 `volume` 类时退出 0。

    那类偏差在 2026-09-20 已被判为"源的几个端点彼此对不上"（ADR-0009 补充决定），天天有几十条
    不代表事故，代表的是 #33 那个待裁决的容差问题。让它发非零就等于把一条已知会响的检查混进
    门禁，然后所有人习惯它的红。
    """
    land(volume_d3=125.0)  # 分钟侧 100 vs 日线 125 → -20%
    assert rh.main([]) == 0
    out = capsys.readouterr().out
    assert "volume=1" in out
    assert "最小 -20.00%" in out
    assert "volume 命中：1 个 (票,天)，分布在 1 个独立交易日" in out


def test_a_price_difference_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    """`volume` 以外的任何一种都要人看：发 1，并把那条原文打出来。"""
    land(open_d3=10.5)
    assert rh.main([]) == 1
    out = capsys.readouterr().out
    assert "[price]" in out
    assert "开盘不相等：分钟合成 10 vs 日线 10.5" in out


def test_an_empty_disk_is_not_reported_as_clean(capsys: pytest.CaptureFixture[str]) -> None:
    """盘上没分钟行时发 2 而不是 0：0 会被读成"扫过了，干净"，而它什么都没扫。"""
    assert rh.main([]) == 2
    assert "什么都没查" in capsys.readouterr().out


def test_the_default_pool_is_what_is_actually_on_disk() -> None:
    """不传 `--symbols` 时票池是"该周期在盘上有行的那些票"。

    这条同时钉住 `layout.dataset_symbols`：全量重扫审的是已落盘的东西、一次抓取都不发起，
    所以 ADR-0009 代价四那句"池子待裁决前只用显式 --symbols"管不到它。而默认值若写死成几只票，
    换一天再跑就会安静地少扫一片。
    """
    land()
    land(symbol="300750", volume_d3=125.0)
    result = rh.scan(["5"])
    assert {f.symbol for f in result.findings} == {"300750"}
    assert layout.dataset_symbols(layout.minute_dataset("5")) == ("300750", "600519")


def test_the_gap_keeps_its_sign() -> None:
    """偏差带符号，两个方向都测：实测 3,895 个组合恒为负，"恒为负"是"源少给了"与
    "两边随机噪声"的分界，取绝对值就看不见它。
    """
    land(volume_d3=125.0)
    short = [g[0] for g in rh.scan(["5"]).gaps if g[0] < 0]
    assert short == [-20.0]
    land(symbol="300750", volume_d3=50.0)  # 反过来：分钟侧偏多
    both = {(g[2], g[0]) for g in rh.scan(["5"]).gaps}
    assert ("300750", 100.0) in both
    assert ("600519", -20.0) in both


def test_render_shows_the_bands_and_the_worst_rows() -> None:
    """分档表与"最差 N 个"都在输出里：#33 要换容差时读的是这张表，不是命令行上多一个数。"""
    land(volume_d3=125.0)
    text = rh.render(rh.scan(["5"]))
    for band in rh.BANDS:
        assert f">{band:>4.1f}%" in text
    assert "最差的 5 个：" in text
    assert "minute_5" in text
