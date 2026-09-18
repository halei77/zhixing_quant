"""CLI 层（09 §七）：状态机之外的第二道防线是退出码。

0/2/3 的语义写进 02 §三 与 09 §八——agent 判断"这一步有没有被接受"只看退出码，
所以这里测的是契约，不是打印好不好看。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from zhixing_quant.tasks import cli

ADVANCE_ARGS = ["advance", "1", "--evidence", "CI #123 全绿"]


@pytest.fixture
def db_file(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "taskdb" / "tasks.duckdb"
    assert cli.main(["new", "做T引擎骨架", "--type", "feat", "--step", "6"], db_path=path) == 0
    yield path
    assert not list(tmp_path.glob("*.duckdb.wal")), "不应留下未合并的 WAL"


def _out(capsys: pytest.CaptureFixture[str]) -> str:
    return capsys.readouterr().out


def test_new_then_show_prints_chinese_labels(
    db_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["show", "1"], db_path=db_file) == cli.EXIT_OK
    text = _out(capsys)
    assert "#1 [feat] 做T引擎骨架" in text
    assert "开发" in text and "进行中" in text
    assert "登记" in text, "时间线要能看到登记事件"


def test_full_pipeline_through_cli(db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    for _ in range(3):
        assert cli.main(ADVANCE_ARGS, db_path=db_file) == cli.EXIT_OK
    assert (
        cli.main(
            ["conclude", "1", "--verdict", "通过", "--evidence", "01 Step6 对照表"],
            db_path=db_file,
        )
        == cli.EXIT_OK
    )
    cli.main(["show", "1"], db_path=db_file)
    text = _out(capsys)
    assert "结论" in text and "已结" in text
    assert text.count("测试") >= 1, "阶段名应出现在时间线里"


def test_advance_without_evidence_is_rejected_with_exit_2(
    db_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["advance", "1"], db_path=db_file) == cli.EXIT_REJECTED
    assert "拒绝" in capsys.readouterr().err
    cli.main(["show", "1"], db_path=db_file)
    assert "开发" in _out(capsys), "被拒绝的迁移不能改变阶段"


def test_na_reason_path(db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["advance", "1", "--evidence", "单测"], db_path=db_file)
    assert cli.main(["advance", "1", "--na-reason", "纯文档任务"], db_path=db_file) == cli.EXIT_OK
    cli.main(["show", "1"], db_path=db_file)
    text = _out(capsys)
    assert "数据验证" in text
    assert "阶段不适用 测试→数据验证 纯文档任务" in text


def test_unknown_id_exits_3(db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["show", "77"], db_path=db_file) == cli.EXIT_NOT_FOUND
    assert "不存在" in capsys.readouterr().err
    assert cli.main(["backtest", "show", "77"], db_path=db_file) == cli.EXIT_NOT_FOUND


def test_suspend_and_user_decisions_via_cli(
    db_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for _ in range(3):
        cli.main(["advance", "1", "--evidence", "e"], db_path=db_file)
        cli.main(
            ["reject", "1", "--to", "开发", "--reason", "实现缺陷", "--note", "少了停牌分支"],
            db_path=db_file,
        )
    cli.main(["show", "1"], db_path=db_file)
    out = _out(capsys)
    assert "挂起" in out and "自动挂起" in out
    assert "实现缺陷：少了停牌分支" in out, "打回原因要能读回来"

    assert (
        cli.main(["advance", "1", "--evidence", "偷偷继续"], db_path=db_file) == cli.EXIT_REJECTED
    )
    assert cli.main(["resume", "1", "--note", "根因已定位"], db_path=db_file) == cli.EXIT_OK
    assert cli.main(["advance", "1", "--evidence", "补了测试"], db_path=db_file) == cli.EXIT_OK
    assert (
        cli.main(
            ["reject", "1", "--to", "开发", "--reason", "其他", "--note", "第四次"],
            db_path=db_file,
        )
        == cli.EXIT_OK
    ), "计数不清零，再打回继续挂起"
    assert cli.main(["abandon", "1", "--note", "拆分重做"], db_path=db_file) == cli.EXIT_OK
    cli.main(["board"], db_path=db_file)
    assert "已结" in _out(capsys)


def test_board_filter_by_status(db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["new", "数据源修复", "--type", "fix", "--step", "2"], db_path=db_file)
    capsys.readouterr()
    capsys.readouterr()
    assert cli.main(["board", "--status", "open"], db_path=db_file) == cli.EXIT_OK
    text = _out(capsys)
    assert "#1" in text and "#2" in text
    cli.main(["show", "3"], db_path=db_file)  # 不存在，仅清空缓冲
    capsys.readouterr()
    assert cli.main(["board", "--status", "closed"], db_path=db_file) == cli.EXIT_OK
    assert _out(capsys).strip() == "", "还没有已结任务"


def test_pitfall_commands(db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        cli.main(
            [
                "pitfall",
                "add",
                "--symptom",
                "分钟K 有未来数据",
                "--root-cause",
                "按抓取时间而非 bar 时间对齐",
                "--workaround",
                "截断一致性测试打底",
                "--related",
                "4,6",
            ],
            db_path=db_file,
        )
        == cli.EXIT_OK
    )
    assert "坑 #1 已登记" in _out(capsys)
    cli.main(["pitfall", "search", "分钟"], db_path=db_file)
    out = _out(capsys)
    assert "现象：" in out and "规避：" in out
    cli.main(["pitfall", "search", "不存在的词"], db_path=db_file)
    assert "坑表无匹配" in _out(capsys)


def test_backtest_commands(db_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = [
        "backtest",
        "add",
        "--strategy",
        "ma_cross",
        "--version",
        "v3",
        "--params-hash",
        "9f2c",
        "--start",
        "2021-01-04",
        "--end",
        "2024-12-31",
        "--cost",
        "双边0.13%",
        "--report",
        "backtests/ma_cross-v3.md",
        "--status",
        "通过",
        "--task",
        "1",
        "--metrics-in",
        "夏普1.8",
        "--metrics-out",
        "夏普1.1",
    ]
    assert cli.main(args, db_path=db_file) == cli.EXIT_OK
    first = _out(capsys)
    assert "第1次" in first and "#1 ma_cross v3" in first
    assert cli.main(args, db_path=db_file) == cli.EXIT_OK
    assert "第2次" in _out(capsys), "同策略同参数哈希要续编号（03 L4.5）"
    assert cli.main(["backtest", "list", "--strategy", "ma_cross"], db_path=db_file) == cli.EXIT_OK
    listed = _out(capsys)
    assert len(listed.strip().splitlines()) == 2, "两条同策略记录"
    assert "#1 ma_cross" in listed and "#2 ma_cross" in listed
    assert cli.main(["backtest", "show", "1"], db_path=db_file) == cli.EXIT_OK
    shown = _out(capsys)
    assert "backtests/ma_cross-v3.md" in shown
    assert "夏普1.8" in shown and "夏普1.1" in shown, "样本内外指标只在 show 里出现（09 §六）"
    assert "[通过]" in shown, "状态用中文标签，与任务看板口径一致"
    bad = list(args)
    bad[bad.index("--status") + 1] = "极好"
    assert cli.main(bad, db_path=db_file) == cli.EXIT_REJECTED


def test_db_flag_is_honoured(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--db 指向哪里就在哪里建库：数据根配置之外的唯一入口，用于测试与临时库。"""
    path = tmp_path / "elsewhere.duckdb"
    assert cli.main(["--db", str(path), "new", "临时", "--type", "docs", "--step", "0d"]) == 0
    assert path.is_file()
    assert cli.main(["--db", str(path), "board"], db_path=path) == cli.EXIT_OK
    assert "临时" in _out(capsys)


def test_parser_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])
