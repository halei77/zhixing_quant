"""`backup/run.py`：真的往备份根拷文件的那一层（ADR-0005 补充决定二、三）。

这一层的测试全部跑在 tmp 里的几 KB 文本上，判的是**盘上发生了什么**：该拷的到了没有、不该动的
有没有被动、账在不在最后一个动作上。所以每条测试收尾都去看目录和 `state.json`，而不只看返回值——
备份工具说谎的方式正是"报告说拷了 5 个文件，盘上只有 4 个"。

`full` 与 `incremental` 共用的那两棵树的形状由夹具一次搭好：数据根与备份根是兄弟，因为套在一起
`check_layout` 就会响（那是 `test_backup_state.py` 判过的），留在这儿只会挡住真正要看的東西。
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path

import pytest

from zhixing_quant.backup import state
from zhixing_quant.backup.run import (
    EmptyDataRoot,
    copy_file,
    delta_base,
    full,
    incremental,
    list_generations,
    prune,
)
from zhixing_quant.backup.state import DELTA, FULL, Manifest, save_manifest

D0 = date(2026, 9, 20)
ONE = timedelta(days=1)
D1 = D0 + ONE
D2 = D1 + ONE
D3 = D2 + ONE

TREE = {
    "data/daily/year=2026/symbol=600519.parquet": "日线",
    "data/master/master.parquet": "主数据",
    "reports/2026-09-19.md": "日报",
}


def write_tree(root: Path, files: dict[str, str] | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in (files or TREE).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def data(tmp_path: Path) -> Path:
    return write_tree(tmp_path / "zhixing_data")


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    return tmp_path / "网盘"


# ── copy_file：一次拷贝的形状 ────────────────────────────────────────────────


def test_copy_records_the_source_shape_and_the_copy_hash(data: Path, tmp_path: Path) -> None:
    src = data / "reports/2026-09-19.md"
    dst = tmp_path / "别处/日报.md"
    stat = copy_file(src, dst)
    assert dst.read_text(encoding="utf-8") == "日报"
    assert stat.size == src.stat().st_size
    assert stat.mtime_ns == src.stat().st_mtime_ns, "账要记的是源文件的时间戳，不是拷过去那份的"
    assert stat.sha256 == hashlib.sha256(src.read_bytes()).hexdigest()


def test_a_partial_copy_never_shows_up_as_a_file(data: Path, tmp_path: Path) -> None:
    """同步客户端看见的是"这个文件存在了"，所以半截文件必须藏在改名之后（补充决定三）。"""
    dst = tmp_path / "out.md"
    copy_file(data / "reports/2026-09-19.md", dst)
    assert not list(dst.parent.glob("*.tmp"))


def test_a_big_file_copies_whole_even_though_the_read_is_chunked(tmp_path: Path) -> None:
    """`CHUNK` 是 1 MiB，跨界的那次读要恰好把最后一个字节也带过去。"""
    src = tmp_path / "big.bin"
    payload = bytes(bytearray(range(256))) * 4096 + b"tail"  # 恰好越过一个整块
    src.write_bytes(payload)
    stat = copy_file(src, tmp_path / "out.bin")
    assert (tmp_path / "out.bin").read_bytes() == payload
    assert stat.sha256 == hashlib.sha256(payload).hexdigest()


# ── full：建基线 ─────────────────────────────────────────────────────────────


def test_a_full_copies_the_whole_tree_and_writes_the_ledger_last(data: Path, backup: Path) -> None:
    report = full(D0, data=data, backup=backup)
    target = backup / "full" / "2026-09-20"
    copied = {
        p.relative_to(target).as_posix()
        for p in target.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    assert copied == set(TREE), "那一代目录里除了自己的清单，装的就该是数据根那棵树的形状"
    assert report.copied == 3 and report.total_bytes == sum(len(t.encode()) for t in TREE.values())
    assert state.load_state(backup).base == D0
    assert set(state.load_state(backup).files) == set(TREE)
    manifest = state.load_manifest(backup, FULL, D0)
    assert manifest.base is None and set(manifest.files) == set(TREE)
    assert all(stat.sha256 for stat in manifest.files.values()), "全量拷过去的每个文件都该带哈希"


def test_a_full_ignores_the_ledger_it_finds(data: Path, backup: Path) -> None:
    """基线的全部理由就是"它不看账"：账写坏了、被删了、还是上个月的，跑一次全量都仍然正确。"""
    save_manifest(backup, Manifest(kind=FULL, when=D0, base=None, files={}))
    state.save_state(backup, state.State(base=D0, files={"不存在的文件": state.Stat(1, 1)}))
    (data / "新文件.txt").write_text("新", encoding="utf-8")
    report = full(D1, data=data, backup=backup)
    assert report.copied == 4
    assert set(state.load_state(backup).files) == set(TREE) | {"新文件.txt"}


def test_a_second_full_on_the_same_day_leaves_no_stray_file(data: Path, backup: Path) -> None:
    """同一天重跑要覆盖那一代，而不是叠加：留着上一代的孤儿，恢复就会拼出一个已经删掉的树。"""
    full(D0, data=data, backup=backup)
    (data / "reports/2026-09-19.md").unlink()
    full(D0, data=data, backup=backup)
    assert set(state.load_manifest(backup, FULL, D0).files) == set(TREE) - {"reports/2026-09-19.md"}
    assert "reports/2026-09-19.md" not in state.load_state(backup).files
    assert not (backup / "full" / "2026-09-20" / "reports/2026-09-19.md").exists()


def test_an_empty_data_root_is_refused_rather_than_becoming_a_baseline(tmp_path: Path) -> None:
    """盘没挂上的样子就是"目录在、里面空"，而它会让全量建出一份空基线并顺手裁掉真基线。"""
    empty = tmp_path / "zhixing_data"
    empty.mkdir()
    with pytest.raises(EmptyDataRoot, match="一个文件都没有"):
        full(D0, data=empty, backup=tmp_path / "网盘")


def test_an_empty_root_cannot_silently_mark_everything_deleted(data: Path, backup: Path) -> None:
    full(D0, data=data, backup=backup)
    for path in data.rglob("*"):
        if path.is_file():
            path.unlink()
    with pytest.raises(EmptyDataRoot):
        incremental(D1, data=data, backup=backup)
    assert state.load_state(backup).base == D0, "被拒的这次运行不许动账"


# ── incremental：只拷变了的那些 ──────────────────────────────────────────────


def test_an_incremental_copies_only_what_moved(data: Path, backup: Path) -> None:
    full(D0, data=data, backup=backup)
    untouched = backup / "full" / "2026-09-20" / "data/master/master.parquet"
    before = untouched.stat().st_mtime_ns
    (data / "reports/2026-09-19.md").write_text("日报重写了更多字", encoding="utf-8")
    write_tree(data, {"site/recent.json": "[]"})
    report = incremental(D1, data=data, backup=backup)
    delta = backup / "delta" / "2026-09-21"
    assert {p.relative_to(delta).as_posix() for p in delta.rglob("*") if p.is_file()} == {
        "manifest.json",
        "reports/2026-09-19.md",
        "site/recent.json",
    }
    assert (report.added, report.changed, report.copied) == (1, 1, 2)
    assert report.base == D0
    assert untouched.stat().st_mtime_ns == before, "没变的文件不许被动"
    assert state.load_state(backup).base == D0, "增量不改基线"


def test_a_deletion_is_written_into_the_ledger_not_onto_the_disk(data: Path, backup: Path) -> None:
    """删除在增量里是一条记录，不是一次抹除——基线那棵树必须原样留着，否则历史就没了。"""
    full(D0, data=data, backup=backup)
    (data / "reports/2026-09-19.md").unlink()
    report = incremental(D1, data=data, backup=backup)
    assert report.deleted == 1
    manifest = state.load_manifest(backup, DELTA, D1)
    assert manifest.deleted == ("reports/2026-09-19.md",)
    assert not (backup / "delta" / "2026-09-21" / "reports/2026-09-19.md").exists()
    assert (backup / "full" / "2026-09-20" / "reports/2026-09-19.md").is_file()
    assert "reports/2026-09-19.md" not in state.load_state(backup).files


def test_a_file_that_came_back_inside_one_generation_is_not_left_as_a_deletion(
    data: Path, backup: Path
) -> None:
    """网盘同步会先把文件抽走再放回来。恢复是"先加后删"，那条删除若留着会抹掉刚拷过去的文件。

    所以要同一天跑两次：第一次记下的删除，第二次必须被"它现在就在盘上"这件事顶掉。跨天的两次
    运行测不到这条——那时的删除属于上一代，本来就该留着。
    """
    path = "reports/2026-09-19.md"
    full(D0, data=data, backup=backup)
    (data / path).unlink()
    first = incremental(D1, data=data, backup=backup)
    assert first.deleted == 1, "前提：这一代确实先记了一条删除"
    write_tree(data, {path: "回来了"})
    incremental(D1, data=data, backup=backup)
    manifest = state.load_manifest(backup, DELTA, D1)
    assert manifest.deleted == ()
    assert path in manifest.files
    assert path in state.load_state(backup).files


def test_a_quiet_day_writes_nothing_and_says_so(data: Path, backup: Path) -> None:
    """ "跑了、什么都没备、退出码 0"是备份工具最坏的失败，所以这句话得在报告里，不在异常里。"""
    full(D0, data=data, backup=backup)
    report = incremental(D1, data=data, backup=backup)
    assert report.copied == 0
    assert "没有任何变化" in report.notes[0]
    assert "这一代没写盘" in report.markdown
    assert not (backup / "delta" / "2026-09-21").exists()
    assert state.load_state(backup).base == D0


def test_a_second_run_on_the_same_day_keeps_what_the_first_one_copied(
    data: Path, backup: Path
) -> None:
    """同一天跑两次：第二次的清单是读回来合并的，否则第一次拷过去的内容会被一份空清单替掉。"""
    full(D0, data=data, backup=backup)
    (data / "新.txt").write_text("一", encoding="utf-8")
    incremental(D1, data=data, backup=backup)
    (data / "新.txt").unlink()
    (data / "又一天.txt").write_text("二", encoding="utf-8")
    report = incremental(D1, data=data, backup=backup)
    manifest = state.load_manifest(backup, DELTA, D1)
    assert set(manifest.files) == {"新.txt", "又一天.txt"}
    assert manifest.deleted == ("新.txt",), "当天先加后删：加记在清单里，删记在 deleted，恢复时删赢"
    assert report.copied == 1, "第二次只拷它自己看见的那一个变化"
    assert "两个数不必相等" in report.markdown, (
        "M2：清单 2 项与拷了 1 个并排印在同一页，得有一句解释"
    )
    kept = backup / "delta" / "2026-09-21" / "新.txt"
    assert kept.read_text(encoding="utf-8") == "一", "第一次拷过去的那份不许被合并写没"


def test_an_incremental_without_a_baseline_is_refused(data: Path, backup: Path) -> None:
    """建立基线是 `full` 的动作，不是"读不到账"的副作用。"""
    with pytest.raises(FileNotFoundError):
        incremental(D1, data=data, backup=backup)


def test_an_incremental_refuses_a_backup_root_inside_the_data_root(tmp_path: Path) -> None:
    data_root = write_tree(tmp_path / "zhixing_data")
    with pytest.raises(state.BackupLayoutError, match="互相包含"):
        incremental(D1, data=data_root, backup=data_root / "backup")


# ── 代的名字与裁剪 ───────────────────────────────────────────────────────────


def test_generations_are_listed_ascending_and_absent_means_none(backup: Path, data: Path) -> None:
    assert list_generations(backup, FULL) == ()
    full(D1, data=data, backup=backup)
    full(D0, data=data, backup=backup)
    assert list_generations(backup, FULL) == (D0, D1)


def test_a_directory_that_is_not_a_generation_is_named_not_skipped(backup: Path) -> None:
    """认不出的目录名要响：静默跳过会把该删的那代留到永远，也会把不该算的算进保留名额。"""
    (backup / "full" / "临时放的东西").mkdir(parents=True)
    with pytest.raises(ValueError, match="不像一代备份的目录名"):
        list_generations(backup, FULL)


def test_a_deltas_baseline_comes_from_its_manifest_not_its_directory_name(
    data: Path, backup: Path
) -> None:
    full(D0, data=data, backup=backup)
    (data / "x.txt").write_text("一", encoding="utf-8")
    incremental(D1, data=data, backup=backup)
    assert delta_base(backup, D1) == D0


def test_prune_keeps_three_fulls_and_drops_the_deltas_that_can_no_longer_be_used(
    data: Path, backup: Path
) -> None:
    """裁旧的判据是"这代还拼得出东西吗"，不是"它老不老"。

    基线已被裁掉的增量留着也拼不出任何一天的树，而它的存在会让 `plan()` 在那一天上选到一个
    拼不出的组合（那是 `test_backup_restore.py` 判的事，这儿只判裁完的形状）。
    """
    for index, day in enumerate((D0, D1, D2, D3)):
        full(day, data=data, backup=backup)
        (data / f"x{index}.txt").write_text("一", encoding="utf-8")
        incremental(day + ONE, data=data, backup=backup)
    assert list_generations(backup, FULL) == (D1, D2, D3)
    assert not (backup / "full" / D0.isoformat()).exists()
    # 挂在被裁掉的那代基线（D0）上的增量一起走；后面三代各自的基线都还在
    assert list_generations(backup, DELTA) == (D2, D3, D3 + ONE)
    assert (backup / "delta" / (D3 + ONE).isoformat()).is_dir()


def test_a_delta_without_a_baseline_is_prunable(backup: Path) -> None:
    """`base is None` 的增量是一份说不清自己从哪开始的账，留着只会拼错树。"""
    for day in (D0, D1, D2, D3):
        save_manifest(backup, Manifest(kind=FULL, when=day, base=None, files={}))
    save_manifest(backup, Manifest(kind=DELTA, when=D1, base=None, files={}))
    gone = prune(backup, keep=3)
    assert D0 in gone and D1 in gone
    assert not (backup / "delta" / "2026-09-21").exists()


# ── 报告 ─────────────────────────────────────────────────────────────────────


def test_the_report_page_carries_the_counts_and_the_landing_spot(data: Path, backup: Path) -> None:
    text = full(D0, data=data, backup=backup).markdown
    assert text.startswith("# 备份 · 全量 · 2026-09-20")
    assert f"落点：`{backup / 'full' / '2026-09-20'}`" in text
    assert "新增 3、改动 0、删除 0" in text
    assert "本次就是基线" in text
    assert "耗时：" in text


def test_the_pruned_generations_appear_in_the_report(data: Path, backup: Path) -> None:
    for day in (D0, D1, D2, D3):
        report = full(day, data=data, backup=backup)
    assert report.pruned and "裁掉旧代" in report.markdown
    assert report.markdown.endswith("\n")
