"""`tools/bench_storage.py` 的最小冒烟测试（坑 #38 的免疫，不是基准本身）。

基准本身**不进 CI**：01 §Step 3 验收 1 要的是"跟生产同量级的目录宽度 + 原生 ext4 的随机读"，
CI 里跑要么拖成几分钟、要么缩水成"三个文件里查一行很快"——后者测的是 Python 循环。

但烂掉过一次的原因恰恰是"没人跑所以没人红"：`--dataset bench_daily` 那个不存在的名字躺在文件里
好几个 Step。所以这里跑的是一格最小尺寸的基准（2 票 × 5 根 × 1 次），它测的不是性能，是三件
会安静烂掉的事：脚本还导得进来、dataset 名还认、那条"不许落在真数据根里"的护栏还在挡。
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import bench_storage as bs  # noqa: E402  # tools/ 不在包内，按脚本路径导入


def test_a_one_cell_benchmark_runs_end_to_end(tmp_path: Path) -> None:
    """最小尺寸跑通：合成、落盘、读回、报数、退出 0。dataset 名一错就会在这里炸而不是没人知道。"""
    assert (
        bs.main(["--root", str(tmp_path / "bench"), "--symbols", "2", "--bars", "5", "--runs", "1"])
        == 0
    )
    assert list((tmp_path / "bench" / "daily").rglob("*.parquet"))


def test_it_refuses_to_write_inside_the_real_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """护栏本身：几十万个合成分区混进真 `daily/` 一旦清不干净，下次查询就读到假K线。

    真数据根在这里用环境变量模拟——它判的是"两个路径套叠"，与被模拟的是不是家里那份无关。
    """
    monkeypatch.setenv("ZX_DATA_ROOT", str(tmp_path))
    inside = tmp_path / "data" / "nested"
    assert bs.main(["--root", str(inside)]) == 2
    assert not inside.exists()  # 拒绝得干净：一个字节都没落


def test_the_acceptance_number_lives_in_the_tool_not_in_a_comment() -> None:
    """01 §Step 3 验收 1 的"< 100ms"只许有一个出处：改它得先改文档，所以它是个具名常量。"""
    assert bs.TARGET_MS == 100.0
