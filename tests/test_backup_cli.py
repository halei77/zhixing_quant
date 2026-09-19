"""`zx-backup` 判的是"装对了没有"（ADR-0005 决定 4、补充决定一/二/四/五）。

备份的账在 `state.py`、字节在 `run.py`、拼回来在 `restore.py`，都各自判过了；这里判的是把它们
接到 `ZX_BACKUP_ROOT`、数据根与退出码上的那一段。三样东西最容易接错：

- **退出码的三档**（0 跑成了 / 1 跑成了但结论是"不行" / 2 根本没开始）。告警只看这一个数，所以
  每档都有一条：坏备份必须是 1 而不是 2（那是"今天跑了、备份被证明不可用"），没配备份根必须是 2
  而不是 0（那是"今天根本没开始"）。
- **报告落不落盘**：跑过就得有能查的东西，所以每条都去看 `reports/backup/` 里那个文件。
- **口径从哪儿来**：干净区那一段目录名从 `config` 的两个派生路径相减得到，不是这里写死的一个字符串。

数字一律只断言到"能看出接错线"的程度——再细就是在重复纯层的测试。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path, PurePosixPath

import pytest

from tests.fakes import bar
from zhixing_quant import config
from zhixing_quant.backup import cli
from zhixing_quant.storage import layout
from zhixing_quant.storage.write import store_bars

D0 = date(2026, 9, 20)
ONE = timedelta(days=1)
D1 = D0 + ONE
DAILY = f"{layout.DAILY}/year=2026/symbol=600519.parquet"


class FrozenDay(date):
    """`date` 的替身：只钉住 `today()`，`fromisoformat` 继承下来照用。

    不用它就没法判"不写 `--day` 时默认取今天"——那是这个 CLI 唯一一处自己造日期的地方，
    而真实时钟在测试里是不可用的输入。
    """

    @classmethod
    def today(cls) -> FrozenDay:
        return cls.fromisoformat(D1.isoformat())


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """一对兄弟目录当数据根与备份根，两个环境变量都指过去。

    兄弟而不是嵌套：套在一起 `check_layout` 就先响了，那是"没开始"那一档，另有测试专门判它。
    """
    data = tmp_path / "zhixing_data"
    backup = tmp_path / "网盘"
    data.mkdir()
    monkeypatch.setenv(config.ENV_VAR, str(data))
    monkeypatch.setenv(config.BACKUP_ENV, str(backup))
    return data, backup


def seed(data: Path, days: tuple[date, ...] = (D0, D1)) -> Path:
    """干净区落几天**真**日线，外加一份日报：备份的内容与"读得动"的东西就都有了。"""
    store_bars(
        [bar(day, 10.0 + index) for index, day in enumerate(days)],
        dataset=layout.DAILY,
        root=config.parquet_dir(),
    )
    (data / "reports/2026-09-19.md").parent.mkdir(parents=True, exist_ok=True)
    (data / "reports/2026-09-19.md").write_text("日报", encoding="utf-8")
    return data


def archived(when: date, name: str) -> Path:
    return config.reports_dir() / cli.REPORTS / f"{when.isoformat()}-{name}.md"


def backup_file(backup: Path, when: date, path: str = DAILY) -> Path:
    return backup / "full" / when.isoformat() / "data" / path


# ── 没开始：退出码 2 那一档 ───────────────────────────────────────────────────


def test_an_unconfigured_backup_root_stops_before_touching_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """备份根不设默认值（补充决定一）：没配就是没开始，而不是"备到某个谁也猜不出的地方去"。"""
    monkeypatch.setenv(config.ENV_VAR, str(tmp_path / "数据根"))
    monkeypatch.delenv(config.BACKUP_ENV, raising=False)
    assert cli.main(["full"]) == 2
    err = capsys.readouterr().err
    assert "备份中止" in err and config.BACKUP_ENV in err
    assert "Traceback" not in err, "没配备份根是一种用法错误，不是一场崩溃"


def test_a_backup_root_inside_the_data_root_is_refused(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    data, _ = roots
    monkeypatch.setenv(config.BACKUP_ENV, str(data / "backup"))
    assert cli.main(["full", "--day", D0.isoformat()]) == 2
    assert "互相包含" in capsys.readouterr().err


def test_a_missing_baseline_is_reported_as_nothing_to_increment(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """第一次跑请走 `full`：读不到账不许悄悄改出一份新基线。"""
    data, _ = roots
    seed(data)
    assert cli.main(["incr", "--day", D1.isoformat()]) == 2
    assert "state.json" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["full", "--day", "20号"],
        ["backitup"],
        [],
        ["incr", "--day", "2026-13-01"],
    ],
    ids=["日期写错", "没有这个子命令", "什么都没给", "日期不存在"],
)
def test_arguments_that_cannot_be_read_never_reach_the_disk(argv: list[str]) -> None:
    """认不出的子命令与写错的日期是**用法**错误：argparse 自己退 2，不该由备份层去猜。"""
    with pytest.raises(SystemExit) as gone:
        cli.main(argv)
    assert gone.value.code == 2


def test_restore_and_verify_need_a_target(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["restore", "--day", D0.isoformat()])
    assert "--into" in capsys.readouterr().err


# ── 跑成了：备份那两条 ───────────────────────────────────────────────────────


def test_full_writes_a_generation_a_report_and_the_page(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    data, backup = roots
    seed(data)
    assert cli.main(["full", "--day", D0.isoformat()]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# 备份 · 全量 · 2026-09-20")
    assert (backup_file(backup, D0)).is_file()
    # 归档那份与打出来那份是同一个内容，差的只是 `print` 补的行尾
    assert archived(D0, "full").read_text(encoding="utf-8") + "\n" == out


def test_the_default_day_is_today(
    roots: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不写 `--day` 时取今天——那是这个入口唯一一处自己造日期的地方，所以它得能被钉住。"""
    data, backup = roots
    seed(data)
    monkeypatch.setattr(cli, "date", FrozenDay)
    assert cli.main(["full"]) == 0
    assert "# 备份 · 全量 · 2026-09-21" in capsys.readouterr().out
    assert archived(D1, "full").is_file()
    assert (backup / "full" / "2026-09-21").is_dir()


def test_incremental_archives_its_own_page(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    data, backup = roots
    seed(data)
    cli.main(["full", "--day", D0.isoformat()])
    (data / "site/recent.json").parent.mkdir(parents=True, exist_ok=True)
    (data / "site/recent.json").write_text("[]", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["incr", "--day", D1.isoformat()]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# 备份 · 增量 · 2026-09-21")
    assert "新增 2" in out, "新写的那份，加上昨天归档进数据根的报告——报告也在被备份的树里"
    assert (backup / "delta" / "2026-09-21/site/recent.json").is_file()
    assert (backup / "delta" / "2026-09-21" / archived(D0, "full").relative_to(data)).is_file()
    assert archived(D1, "incr").read_text(encoding="utf-8") + "\n" == out


def test_a_quiet_day_still_exits_zero_and_says_what_it_did(
    roots: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """啥也没变不是失败：报告里那句话就是给人看的，退出码仍然是 0（今天确实跑了）。

    报告归档到数据根**外面**是这条测试的前提，不是偷懒：归档那份本身就落在被扫描的树里，所以真
    跑起来的每一天都会看见"昨天那个报告是新增文件"——那是对的（它也确实该被备份），只是会把
    "没有任何变化"那一路挡住。
    """
    data, backup = roots
    seed(data)
    monkeypatch.setattr(config, "reports_dir", lambda **_: tmp_path / "报告在外面")
    cli.main(["full", "--day", D0.isoformat()])
    capsys.readouterr()
    assert cli.main(["incr", "--day", D1.isoformat()]) == 0
    out = capsys.readouterr().out
    assert "没有任何变化" in out and "这一代没写盘" in out
    assert not (backup / "delta").exists()
    assert (tmp_path / "报告在外面/backup/2026-09-21-incr.md").is_file()


# ── restore：拼回来那一档 ─────────────────────────────────────────────────────


def test_restore_prints_the_tree_it_stacked_and_leaves_the_ledger_alone(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    data, backup = roots
    seed(data)
    cli.main(["full", "--day", D0.isoformat()])
    ledger = (backup / "state.json").read_text(encoding="utf-8")
    into = data.parent / "恢复"
    capsys.readouterr()
    assert cli.main(["restore", "--day", D0.isoformat(), "--into", str(into)]) == 0
    out = capsys.readouterr().out
    assert f"恢复 {D0.isoformat()} → `{into}`" in out
    assert "全量 2026-09-20 叠加 0 代增量 → 2 个文件" in out
    assert "字节全部与清单一致" in out
    assert "restore 只读，不写账" in out
    source = config.parquet_dir() / "daily/year=2026/symbol=600519.parquet"
    assert (into / "data" / DAILY).read_bytes() == source.read_bytes()
    assert (backup / "state.json").read_text(encoding="utf-8") == ledger, "那句「不写账」得是真的"


def test_a_backup_whose_bytes_no_longer_match_exits_one(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """1 说的是"跑了，而备份被证明不可用"。判成 2 就把两件事混成一件：告警会以为今天没跑。"""
    data, backup = roots
    seed(data)
    cli.main(["full", "--day", D0.isoformat()])
    backup_file(backup, D0).write_text("被网盘写坏了", encoding="utf-8")
    into = data.parent / "恢复"
    capsys.readouterr()
    assert cli.main(["restore", "--day", D0.isoformat(), "--into", str(into)]) == 1
    out = capsys.readouterr().out
    assert "读坏了" in out and DAILY in out


def test_restoring_into_a_used_directory_is_a_no_start(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    data, _ = roots
    seed(data)
    cli.main(["full", "--day", D0.isoformat()])
    into = data.parent / "恢复"
    (into / "别人家的文件.txt").parent.mkdir(parents=True)
    (into / "别人家的文件.txt").write_text("一", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["restore", "--day", D0.isoformat(), "--into", str(into)]) == 2
    assert "不空" in capsys.readouterr().err
    assert list(into.iterdir()) == [into / "别人家的文件.txt"], "被拒的这次不许动目标目录"


# ── verify：演练那一档 ────────────────────────────────────────────────────────


def test_the_drill_archives_its_page_and_passes_on_real_parquet(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """决定 5 的"没演练过的备份等于没有备份"在这里成立：报告归档了，才算这次演练存在过。"""
    data, backup = roots
    seed(data)
    cli.main(["full", "--day", D0.isoformat()])
    into = data.parent / "演练"
    capsys.readouterr()
    assert cli.main(["verify", "--day", D0.isoformat(), "--into", str(into)]) == 0
    out = capsys.readouterr().out
    assert "恢复演练 · 2026-09-20 · 通过" in out
    assert "有行的交易日 2 天" in out
    assert archived(D0, "verify").read_text(encoding="utf-8") + "\n" == out
    assert sorted(p.name for p in backup.iterdir()) == [
        "full",
        "state.json",
    ], "演练不往备份根里写东西"


def test_a_drill_that_cannot_find_a_dataset_is_a_no_start(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """没有东西可验 ≠ 验了不通：前者是这次演练根本没做成，所以是 2。"""
    data, _ = roots
    seed(data)
    (config.parquet_dir() / "daily").rename(config.parquet_dir() / "未知")
    cli.main(["full", "--day", D0.isoformat()])
    capsys.readouterr()
    assert cli.main(["verify", "--day", D0.isoformat(), "--into", str(data.parent / "演练")]) == 2
    assert "没有东西可验" in capsys.readouterr().err


def test_a_drill_that_names_an_absent_dataset_is_told_so(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`--dataset` 是点名，不是"换一个也行"：点名的那份不在，就报读不出东西，不偷偷替换。"""
    data, _ = roots
    seed(data)
    cli.main(["full", "--day", D0.isoformat()])
    capsys.readouterr()
    code = cli.main(
        [
            "verify",
            "--day",
            D0.isoformat(),
            "--into",
            str(data.parent / "演练"),
            "--dataset",
            layout.MINUTE_5,
        ]
    )
    assert code == 1
    assert "一份 K线都没读到" in capsys.readouterr().out


# ── 口径的出处 ───────────────────────────────────────────────────────────────


def test_the_clean_zone_comes_from_config_not_from_a_string_in_here(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """干净区那段是**算**出来的：`config.parquet_dir()` 改了名，这里得跟着改，而不是静默挑不到。"""
    data, _ = roots
    monkeypatch.setattr(config, "parquet_dir", lambda **_: data / "干净区")
    assert cli.clean_zone() == PurePosixPath("干净区")


def test_a_renamed_clean_zone_still_leads_the_drill_to_the_data(
    roots: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """上一条的对照面：换个名字，演练照样找得到那个 dataset。

    只判 `clean_zone()` 的返回值证明不了这件事——真正被保护的是"`verify` 能不能读到盘上的日线"。
    """
    data, backup = roots
    moved = data / "干净区"
    store_bars([bar(D0, 10.0)], dataset=layout.DAILY, root=moved)
    cli.main(["full", "--day", D0.isoformat()])
    monkeypatch.setattr(config, "parquet_dir", lambda **_: moved)
    capsys.readouterr()
    assert cli.main(["verify", "--day", D0.isoformat(), "--into", str(data.parent / "演练")]) == 0
    assert "恢复演练 · 2026-09-20 · 通过" in capsys.readouterr().out
    assert (backup / "full" / D0.isoformat() / "干净区/daily/year=2026").is_dir()
