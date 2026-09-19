"""`backup/state.py`：备份的账（ADR-0005 补充决定一、二、三）。

这里判的全是**字典与文件形状**，不碰"备份成功了吗"那种问题：扫描扫出来的是什么、比对出来的
变化是什么、那本账写出去读回来还是不是同一本。之所以值得单独一层，是因为备份工具最贵的一类错
不写在字节上——"把没变的判成变了"（增量退化成全量，没人报错）、"把缺目录判成空备份"（退出码 0
而什么都没备），两条都只在这一层能判。

`scan` 与 `diff` 都是纯函数（只读盘），所以 tmp 里几 KB 就能判完；真拷的那一段在
`tests/test_backup_run.py`。
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from zhixing_quant.backup.state import (
    DELTA,
    FULL,
    BackupLayoutError,
    Manifest,
    Stat,
    State,
    check_layout,
    diff,
    generation_dir,
    generations_dir,
    load_manifest,
    load_state,
    manifest_file,
    save_manifest,
    save_state,
    scan,
    state_file,
)

D0 = date(2026, 9, 20)


def touch(root: Path, name: str, text: str = "x") -> Path:
    """在 `root` 下写一个文件（带父目录）。测试里的"数据根"就是几个这样的文件。"""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def data(tmp_path: Path) -> Path:
    root = tmp_path / "zhixing_data"
    touch(root, "data/daily/year=2026/symbol=600519.parquet", "日线")
    touch(root, "reports/2026-09-19.md", "日报")
    return root


# ── scan：数据根现在有什么 ────────────────────────────────────────────────────


def test_scan_lists_relative_paths_with_their_shape(data: Path) -> None:
    got = scan(data)
    assert set(got) == {"data/daily/year=2026/symbol=600519.parquet", "reports/2026-09-19.md"}
    stat = got["reports/2026-09-19.md"]
    assert stat.size == len("日报".encode())
    assert stat.mtime_ns > 0
    assert stat.sha256 is None, "扫描不算哈希（补充决定三），哈希只在拷贝那一刻算"


def test_a_missing_data_root_is_not_an_empty_backup(tmp_path: Path) -> None:
    """缺目录扫出 `{}` 就是"没有要备份的文件"——那句话听起来像备份成功了。

    这是"一切正常"形状的失败，与 ADR-0005 补充决定一拒绝的是同一种：盘没挂上必须响。
    """
    with pytest.raises(FileNotFoundError, match="数据根读不到目录"):
        scan(tmp_path / "没这个东西")


def test_scan_skips_symlinks_in_both_directions(tmp_path: Path, data: Path) -> None:
    """文件与目录两种软链都跳。

    临时数据根常用 `data -> /真/数据根/data` 这种写法，跟着链走就是把另一个目录当成这次备份的
    内容；而那条链若指向备份根自己，两棵树就成环了——`os.walk(followlinks=False)` 只挡住了
    目录，文件这一侧要自己判。
    """
    outside = tmp_path / "别人家的数据"
    touch(outside, "secret.parquet", "不该进备份")
    (data / "linked.parquet").symlink_to(outside / "secret.parquet")
    (data / "linked-dir").symlink_to(outside, target_is_directory=True)
    got = scan(data)
    assert "linked.parquet" not in got
    assert not any(path.startswith("linked-dir") for path in got)
    assert "secret.parquet" not in got


def test_scan_ignores_directories_as_leaves(data: Path) -> None:
    assert all(not path.endswith("/") for path in scan(data))


# ── diff：跟上次比谁变了 ─────────────────────────────────────────────────────


def test_new_gone_and_touched_files_are_three_different_answers(data: Path) -> None:
    before = scan(data)
    touch(data, "site/recent.json", "{}")
    (data / "reports/2026-09-19.md").unlink()
    (data / "data/daily/year=2026/symbol=600519.parquet").write_text("变了更多字", encoding="utf-8")
    changes = diff(before, scan(data))
    assert changes.added == ("site/recent.json",)
    assert changes.changed == ("data/daily/year=2026/symbol=600519.parquet",)
    assert changes.deleted == ("reports/2026-09-19.md",)
    assert changes.to_copy == (*changes.added, *changes.changed)
    assert not changes.empty


def test_no_change_at_all_is_reported_as_such(data: Path) -> None:
    once = scan(data)
    assert diff(once, once).empty


def test_a_saved_hash_does_not_make_every_file_look_changed(data: Path) -> None:
    """这条钉的是这个模块最容易被改坏的一行。

    `state.json` 读回来的 `Stat` 带着 sha256，而 `scan` 出来的永远是 None——直接拿 `!=` 比整个
    `Stat` 会让**每个文件每次都被判成变了**：增量退化成全量，备份"成功"，只是每天拷一遍全树。
    """
    current = scan(data)
    with_hash = {path: Stat(stat.size, stat.mtime_ns, "ab" * 32) for path, stat in current.items()}
    assert diff(with_hash, current).changed == ()
    assert diff(current, with_hash).added == ()


def test_a_same_length_rewrite_is_caught_by_the_mtime(data: Path) -> None:
    """字节数没动、只有 mtime 动了一次改写——判据要接住的就是这一类。

    写成"同长度"是故意的：如果只测"文件变大了"，把判据写成 `size` 单项也能过，而真实数据根里
    最常见的是原地重写（同一天再采一次、行数没变）。时间戳用 `utime` 钉死，不靠文件系统粒度。
    """
    path = "reports/2026-09-19.md"
    before = scan(data)
    (data / path).write_text("晚报", encoding="utf-8")  # 与"日报"同字节数
    os.utime(data / path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    current = scan(data)
    assert current[path].size == before[path].size, "样本没做到同长度：那测的就不是 mtime 了"
    assert current[path].mtime_ns != before[path].mtime_ns
    changes = diff(before, current)
    assert changes.changed == (path,)
    assert not changes.added and not changes.deleted


def test_the_same_shape_with_different_bytes_is_the_accepted_blind_spot() -> None:
    """同大小、同 mtime 的改写判不出来。这是 ADR-0005 补充决定三明写接受的代价，钉在这儿当边界。

    不修它：修它等于回到"每次全盘哈希"，而那正是这条决定要换掉的东西。写一条会失败的测试去要求
    全盘哈希，反而是把 ADR 推翻一遍。
    """
    same_shape = {"x.parquet": Stat(size=6, mtime_ns=1_000)}
    assert diff(same_shape, dict(same_shape)).empty


# ── 两棵树的位置关系（补充决定一）────────────────────────────────────────────


@pytest.mark.parametrize(
    "pick",
    [
        lambda tmp: (tmp / "root", tmp / "root" / "backup"),
        lambda tmp: (tmp / "root" / "data", tmp / "root"),
        lambda tmp: (tmp / "root", tmp / "root"),
    ],
    ids=["备份套在数据根里", "数据根套在备份根里", "两个根是同一个目录"],
)
def test_nested_roots_are_refused(
    tmp_path: Path, pick: Callable[[Path], tuple[Path, Path]]
) -> None:
    """两个根套在一起时的表现是"备份成功"，而它拷的是自己（补充决定一）。"""
    data_path, backup_path = pick(tmp_path)
    data_path.mkdir(parents=True, exist_ok=True)
    with pytest.raises(BackupLayoutError, match="互相包含"):
        check_layout(data_path, backup_path)


def test_sibling_roots_are_fine(tmp_path: Path) -> None:
    check_layout(tmp_path / "zhixing_data", tmp_path / "网盘/zhixing_backup")


# ── 账与清单的往返 ───────────────────────────────────────────────────────────


def test_the_state_round_trips(tmp_path: Path) -> None:
    files = {"a/b.parquet": Stat(3, 1_000, "cd" * 32), "c.txt": Stat(1, 2_000)}
    save_state(tmp_path, State(base=D0, files=files))
    back = load_state(tmp_path)
    assert back.base == D0
    assert back.files == files


def test_the_state_is_plain_readable_json(tmp_path: Path) -> None:
    """人得能直接 cat 它：备份工具的账如果是二进制，坏了就只能靠猜。"""
    save_state(tmp_path, State(base=D0, files={"x": Stat(1, 2, "ab" * 32)}))
    raw = json.loads(state_file(tmp_path).read_text(encoding="utf-8"))
    assert raw["base"] == "2026-09-20"
    assert raw["files"]["x"] == [1, 2, "ab" * 32]


def test_saving_state_leaves_no_temporary_file(tmp_path: Path) -> None:
    save_state(tmp_path, State(base=D0, files={}))
    assert not list(tmp_path.glob("*.tmp"))


def test_a_missing_state_is_refused_not_treated_as_empty(tmp_path: Path) -> None:
    """当成"还没有账"会让一次增量悄悄改出新基线，而旧基线与它之间的增量全成了孤儿。

    建立基线是 `full` 的动作，不是"读不到账"的副作用。
    """
    with pytest.raises(FileNotFoundError):
        load_state(tmp_path)


@pytest.mark.parametrize(
    "payload",
    ['{"base": "2026-09-20"}', '{"files": {}}', '"不是字典"', '{"base": 5, "files": {}}'],
    ids=["缺 files", "缺 base", "整个不是字典", "base 不是字符串"],
)
def test_a_half_written_ledger_is_refused(tmp_path: Path, payload: str) -> None:
    state_file(tmp_path).write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="base"):
        load_state(tmp_path)


@pytest.mark.parametrize(
    "entry",
    [
        "[1, 2]",
        '["不是整数", 2, ""]',
        "[1, 2, 3]",
        '{"size": 1}',
    ],
    ids=["少一格", "大小不是整数", "哈希不是字符串", "整个不是数组"],
)
def test_a_bad_row_in_the_ledger_names_itself(tmp_path: Path, entry: str) -> None:
    state_file(tmp_path).write_text(
        '{"base": "2026-09-20", "files": {"x": ' + entry + "}}", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="账目"):
        load_state(tmp_path)


def test_files_has_to_be_a_mapping(tmp_path: Path) -> None:
    state_file(tmp_path).write_text('{"base": "2026-09-20", "files": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="字典"):
        load_state(tmp_path)


def test_a_generation_dir_needs_to_be_written_to_be_read(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_manifest(tmp_path, FULL, D0)


def test_manifest_round_trips_both_kinds(tmp_path: Path) -> None:
    full = Manifest(kind=FULL, when=D0, base=None, files={"x": Stat(1, 2, "ab" * 32)})
    delta = Manifest(
        kind=DELTA, when=D0, base=D0, files={"y": Stat(3, 4, "cd" * 32)}, deleted=("gone",)
    )
    for manifest in (full, delta):
        assert save_manifest(tmp_path, manifest) == manifest_file(tmp_path, manifest.kind, D0)
    assert load_manifest(tmp_path, FULL, D0) == full, "全量的 base 是空的，读回来也得是 None"
    assert load_manifest(tmp_path, DELTA, D0) == delta


def test_a_manifest_without_its_essential_keys_is_refused(tmp_path: Path) -> None:
    path = manifest_file(tmp_path, FULL, D0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"kind": "full"}', encoding="utf-8")
    with pytest.raises(ValueError, match="缺"):
        load_manifest(tmp_path, FULL, D0)


def test_a_manifest_that_is_not_a_mapping_is_refused(tmp_path: Path) -> None:
    path = manifest_file(tmp_path, FULL, D0)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="字典"):
        load_manifest(tmp_path, FULL, D0)


# ── 目录形状的出处 ───────────────────────────────────────────────────────────


def test_generation_paths_follow_the_adr_layout(tmp_path: Path) -> None:
    assert generation_dir(tmp_path, FULL, D0) == tmp_path / "full" / "2026-09-20"
    assert generations_dir(tmp_path, DELTA) == tmp_path / "delta"


def test_an_unknown_generation_kind_is_named(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="没听过这一代备份"):
        generations_dir(tmp_path, "weekly")


def test_the_empty_sha_field_reads_back_as_none(tmp_path: Path) -> None:
    """`_entry` 把 None 写成 ""：JSON 里没有 None 与"空串"的区分，读回来必须还原成 None。"""
    files = {"x": Stat(1, 2)}
    save_state(tmp_path, State(base=D0, files=files))
    assert load_state(tmp_path).files == files


def test_scan_keys_are_relative_so_the_ledger_survives_a_machine_change(tmp_path: Path) -> None:
    """账里的键不许带绝对路径：那等于把这台机器的挂载点写进备份，换机即废。"""
    root = tmp_path / "zhixing_data"
    touch(root, "data/daily/year=2026/symbol=000001.parquet", "一")
    touch(root, "deep/nested/inner.parquet", "二")
    got = scan(root)
    assert set(got) == {
        "data/daily/year=2026/symbol=000001.parquet",
        "deep/nested/inner.parquet",
    }
    assert all(not Path(key).is_absolute() for key in got)


def test_an_empty_root_scans_empty_rather_than_raising(tmp_path: Path) -> None:
    """「目录在、里面还没落盘」与「目录读不到」是两件事，`scan` 只说盘上有什么。

    把"空"在这里判成错，等于让一个纯扫描函数替调用方决定"空树能不能建基线"——那条判定写在
    `run.py`，因为它才知道"备份"这个动作要的是什么。
    """
    root = tmp_path / "空树"
    root.mkdir()
    assert scan(root) == {}
