"""`backup/restore.py`：把某天的树拼回来，并证明它**读得动**（ADR-0005 决定 5、补充决定四）。

`plan` 只读目录名与清单，`restore` 才碰字节，`verify` 在拷完之后用查询层真读一段。三条测试也就
分三段：拼得对不对（日期与代的选择）、拷得全不全（叠代语义与哈希）、读得出来吗（真 Parquet）。

最贵的一类失败在这儿都是"看起来成功"的：拼出一棵哪一天都不存在的树、把损坏的字节当成恢复完成、
演练挑了一个空数据集然后报 0 天。所以每条断言都去看目标目录里**真的有哪几个文件**，而不只看返回值。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path, PurePosixPath

import pytest

from tests.fakes import bar
from zhixing_quant.backup import state
from zhixing_quant.backup.restore import (
    NON_DATASET,
    NoGeneration,
    NothingToVerify,
    _dataset_of,
    pick_dataset,
    plan,
    restore,
    surfaces,
    verify,
)
from zhixing_quant.backup.run import full, incremental
from zhixing_quant.storage import layout
from zhixing_quant.storage.write import store_bars

D0 = date(2026, 9, 20)
ONE = timedelta(days=1)
D1 = D0 + ONE
D2 = D1 + ONE
D3 = D2 + ONE
ZONE = "data"  # 干净区在数据根里的那一段，与 `config.parquet_dir()` 相对 `data_root()` 同形

DAILY = {
    f"{ZONE}/daily/year=2026/symbol=600519.parquet": "日线",
    f"{ZONE}/master/master.parquet": "主数据",
    "reports/2026-09-19.md": "日报",
}


def write_tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def data(tmp_path: Path) -> Path:
    return write_tree(tmp_path / "zhixing_data", DAILY)


@pytest.fixture
def backup(tmp_path: Path) -> Path:
    return tmp_path / "网盘"


def corrupt(path: Path, text: str = "被网盘写坏了") -> None:
    """改备份里那份字节，清单上的哈希没跟着改——这正是补充决定四说的那一种坏法。"""
    path.write_text(text, encoding="utf-8")


def real_root(root: Path, days: tuple[date, ...]) -> Path:
    """一个**真**数据根：`store_bars` 把日线落到 `<root>/data/daily/…`。

    `verify` 那几条非用不可——演练的结论是"查询层读得出多少天"，假的 `.parquet` 文本读到的是
    duckdb 的异常。上面的 `DAILY` 那棵树只用来判叠代与清单的形状。
    """
    store_bars(
        [bar(day, 10.0 + index) for index, day in enumerate(days)],
        dataset=layout.DAILY,
        root=root / ZONE,
    )
    return root


# ── plan：哪天要哪些代 ───────────────────────────────────────────────────────


def test_the_baseline_is_the_newest_full_not_later_than_that_day(data: Path, backup: Path) -> None:
    full(D0, data=data, backup=backup)
    full(D2, data=data, backup=backup)
    assert plan(backup, D1).base == D0
    assert plan(backup, D2).base == D2
    assert plan(backup, D3).base == D2, "那天之后没有新全量，就用最新的那一代当基线"


def test_a_day_before_the_first_baseline_is_refused(data: Path, backup: Path) -> None:
    full(D1, data=data, backup=backup)
    with pytest.raises(NoGeneration, match="拼不出来"):
        plan(backup, D0)


def test_an_empty_backup_root_says_what_to_do_next(backup: Path) -> None:
    with pytest.raises(NoGeneration, match="先跑一次 zx-backup full"):
        plan(backup, D0)


def test_deltas_are_stacked_in_order_and_stop_at_the_day(data: Path, backup: Path) -> None:
    full(D0, data=data, backup=backup)
    for day in (D1, D2, D3):
        write_tree(data, {**DAILY, f"新{day.isoformat()}.txt": "一"})
        incremental(day, data=data, backup=backup)
    assert plan(backup, D2).deltas == (D1, D2)
    assert plan(backup, D2).generations == (("full", D0), ("delta", D1), ("delta", D2))
    assert plan(backup, D1).deltas == (D1,)


def test_an_orphan_delta_is_not_stacked_onto_a_newer_baseline(data: Path, backup: Path) -> None:
    """基线换代之后的旧增量属于上一代。把它们叠进新基线会拼出一棵哪一天都不存在的树。

    `prune` 本应删掉它们，但网盘同步中途失败就可能没删干净——所以 `plan` 不假设裁过。
    """
    full(D0, data=data, backup=backup)
    write_tree(data, {"孤儿.txt": "一"})
    incremental(D1, data=data, backup=backup)
    full(D2, data=data, backup=backup)
    assert plan(backup, D2).deltas == ()
    assert plan(backup, D3).deltas == ()


# ── restore：叠出来的那棵树 ──────────────────────────────────────────────────


def test_restoring_a_day_rebuilds_the_tree_as_it_stood_that_day(data: Path, backup: Path) -> None:
    full(D0, data=data, backup=backup)
    (data / "reports/2026-09-19.md").unlink()
    write_tree(data, {"新增.txt": "一"})
    incremental(D1, data=data, backup=backup)
    into = backup.parent / "恢复"
    result = restore(backup, into, plan(backup, D1))
    assert result.intact and result.bad == ()
    assert set(result.files) == set(DAILY) - {"reports/2026-09-19.md"} | {"新增.txt"}
    assert (into / "新增.txt").read_text(encoding="utf-8") == "一"
    assert not (into / "reports/2026-09-19.md").exists(), "删项要在新增之后应用"
    assert result.total_bytes == sum(len(t.encode()) for t in DAILY.values() if t != "日报") + len(
        "一".encode()
    )


def test_an_older_day_restores_the_state_it_had_then_not_the_latest(
    data: Path, backup: Path
) -> None:
    """时间旅行：恢复 D0 不该带上 D1 才新增的文件，也不该带上 D1 才发生的那条删除。"""
    full(D0, data=data, backup=backup)
    (data / "reports/2026-09-19.md").unlink()
    write_tree(data, {"新增.txt": "一"})
    incremental(D1, data=data, backup=backup)
    into = backup.parent / "回到D0"
    result = restore(backup, into, plan(backup, D0))
    assert set(result.files) == set(DAILY)
    assert (into / "reports/2026-09-19.md").is_file()
    assert not (into / "新增.txt").exists()


def test_a_silently_corrupted_file_is_caught_before_it_is_trusted(data: Path, backup: Path) -> None:
    """字节大小看着正常、内容已经不是当年那份：只有把哈希对一遍才知道（补充决定四）。"""
    full(D0, data=data, backup=backup)
    corrupt(backup / "full" / D0.isoformat() / "data/daily/year=2026/symbol=600519.parquet")
    into = backup.parent / "恢复"
    result = restore(backup, into, plan(backup, D0))
    assert not result.intact
    assert result.bad == ("data/daily/year=2026/symbol=600519.parquet",)
    assert result.total_bytes == sum(
        len(t.encode()) for k, t in DAILY.items() if "daily" not in k
    ), "对不上哈希的那些不算恢复出来的字节"
    assert (into / "data/daily/year=2026/symbol=600519.parquet").is_file(), "文件照拷，但要说它坏了"


def test_the_prefix_filter_follows_the_clean_zone_not_the_dataset_name(
    data: Path, backup: Path
) -> None:
    """`only` 是"从干净区那段起算的前缀"，因为要圈的正是某个 dataset 目录。"""
    full(D0, data=data, backup=backup)
    into = backup.parent / "恢复"
    result = restore(backup, into, plan(backup, D0), only=f"{ZONE}/daily/")
    assert set(result.files) == {"data/daily/year=2026/symbol=600519.parquet"}
    assert not (into / "data/master/master.parquet").exists()
    assert not (into / "reports/2026-09-19.md").exists()


def test_restoring_into_a_directory_that_is_not_empty_is_refused(data: Path, backup: Path) -> None:
    """不空的目录里那些"没人写它"的文件会被当成恢复结果的一部分——那是一棵假树，且看起来完全正常。"""
    full(D0, data=data, backup=backup)
    into = backup.parent / "恢复"
    write_tree(into, {"别人家的文件.txt": "一"})
    with pytest.raises(ValueError, match="不空"):
        restore(backup, into, plan(backup, D0))


def test_restoring_inside_the_backup_root_is_refused(data: Path, backup: Path) -> None:
    """往备份里面恢复，等于把恢复结果当成又一代备份：下一次 `scan` 会把它拷进备份。"""
    full(D0, data=data, backup=backup)
    with pytest.raises(ValueError, match="套在备份根"):
        restore(backup, backup / "full" / "恢复", plan(backup, D0))
    with pytest.raises(ValueError, match="套在备份根"):
        restore(backup, backup, plan(backup, D0))


# ── 挑哪个数据集来演练 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("data/daily/year=2026/symbol=600519.parquet", layout.DAILY),
        (f"data/{layout.MINUTE_5}/x.parquet", layout.MINUTE_5),
        ("data", None),
        ("data/quarantine/2026-09-19.parquet", None),
        ("reports/2026-09-19.md", None),
        ("data/未知dataset/x.parquet", None),
    ],
    ids=["日线分区文件", "没有年份分区也算", "干净区本身", "隔离区不算", "干净区外", "不是已知的"],
)
def test_a_path_belongs_to_a_dataset_only_if_it_is_a_known_one(path: str, want: str | None) -> None:
    """名单从 `layout.SPECS` 来：备份不自己记一份"什么算一个数据集"（补充决定代价三）。"""
    assert _dataset_of(path, PurePosixPath(ZONE)) == want


def test_the_drill_picks_the_fullest_dataset_not_a_random_one(data: Path, backup: Path) -> None:
    full(D0, data=data, backup=backup)
    assert pick_dataset(backup, clean_zone=ZONE, as_of=D0) == layout.DAILY


def test_the_coverage_face_counts_a_file_once_across_generations(data: Path, backup: Path) -> None:
    """覆盖面那格数的是"树上的文件"，不是各代清单相加：全量里有、增量又改过的只算一次。

    它是报告里唯一一处**不来自恢复结果**的数（`verify` 只恢复一个 dataset，其余那些只能从清单数），
    所以这一条判的是叠代语义在数数上也成立：删掉的不算，改过的算一份。
    """
    full(D0, data=data, backup=backup)
    write_tree(data, {"reports/2026-09-19.md": "日报改过一笔"})
    incremental(D1, data=data, backup=backup)
    assert surfaces(backup, clean_zone=ZONE, as_of=D1) == {layout.DAILY: 1, NON_DATASET: 2}
    (data / "data/master/master.parquet").unlink()
    incremental(D2, data=data, backup=backup)
    assert surfaces(backup, clean_zone=ZONE, as_of=D2) == {layout.DAILY: 1, NON_DATASET: 1}


def test_the_drill_prefers_the_dataset_with_more_partitions(data: Path, backup: Path) -> None:
    """两个 dataset 都在时挑文件多的那个：演练要挑的是"最可能读出东西"的那一侧。"""
    write_tree(
        data,
        {
            "data/daily/year=2026/symbol=600519.parquet": "一",
            "data/minute_5/year=2026/symbol=600519.parquet": "二",
            "data/minute_5/year=2026/symbol=000001.parquet": "三",
        },
    )
    full(D0, data=data, backup=backup)
    assert pick_dataset(backup, clean_zone=ZONE, as_of=D0) == layout.MINUTE_5


def test_a_backup_with_nothing_verifiable_picks_nothing(tmp_path: Path) -> None:
    """账上有东西、但没有一个已知 dataset → 挑不出对象，而不是挑一个空的来糊弄。"""
    root = tmp_path / "zhixing_data"
    write_tree(root, {"reports/2026-09-19.md": "日报", "data/quarantine/坏行.parquet": "隔离区"})
    where = tmp_path / "网盘"
    full(D0, data=root, backup=where)
    assert pick_dataset(where, clean_zone=ZONE, as_of=D0) is None


# ── verify：演练的结论 ───────────────────────────────────────────────────────


def test_the_drill_reads_the_restored_parquet_with_the_query_layer(tmp_path: Path) -> None:
    """真 Parquet 走真路径：`store_bars` 落盘 → 备份 → 恢复 → `depth()` 读出一段覆盖范围。

    这条是决定 5 那句"没演练过的备份等于没有备份"的机器版本：它过的意思是"备份里那份字节能被
    查询层读出 3 个交易日"，不是"备份目录里有 3 个文件"。
    """
    root = real_root(tmp_path / "zhixing_data", (D0, D1, D2))
    where = tmp_path / "网盘"
    full(D0, data=root, backup=where)
    result = verify(where, clean_zone=ZONE, into=tmp_path / "演练", as_of=D0)
    assert result.ok
    assert result.dataset == layout.DAILY
    assert result.cover is not None
    assert (result.cover.days, result.cover.symbols) == (3, 1)
    assert result.cover.first == D0 and result.cover.last == D2
    assert "恢复演练 · 2026-09-20 · 通过" in result.markdown
    assert f"用查询层读 `{layout.DAILY}`" in result.markdown
    assert "有行的交易日 3 天" in result.markdown


def test_the_page_says_which_face_of_the_backup_the_drill_actually_verified(
    tmp_path: Path,
) -> None:
    """O2：一次演练只验一个 dataset，而报告页上"通过"两个字的射程只有那一个。

    真数据根上那次就是这么被读成全量的：`daily` 302 个文件验过了，minute 与任务库/产物/快照
    一个没读，报告却只有一句"恢复演练通过"。覆盖面不写出来，读报告的人没有别的办法知道少了
    哪几块——文件数比"验了几个 dataset"更能让人停下手，所以两个都印。
    """
    root = real_root(tmp_path / "zhixing_data", (D0,))
    write_tree(
        root,
        {
            "data/minute_5/year=2026/symbol=600519.parquet": "分钟",
            "taskdb/tasks.duckdb": "任务库",
        },
    )
    where = tmp_path / "网盘"
    full(D0, data=root, backup=where)
    result = verify(where, clean_zone=ZONE, into=tmp_path / "演练", dataset=layout.DAILY, as_of=D0)
    assert result.dataset == layout.DAILY
    assert dict(result.unverified) == {layout.MINUTE_5: 1, NON_DATASET: 1}
    assert "这次只验了 `daily`" in result.markdown
    assert f"`{NON_DATASET}` 1 个文件" in result.markdown


def test_a_corrupted_parquet_fails_the_drill_even_though_the_file_is_there(tmp_path: Path) -> None:
    """备份里那份被写坏了：哈希先 catch 住它，读盘那句也同时说出来——两句话都是给人看的。"""
    root = real_root(tmp_path / "zhixing_data", (D0,))
    where = tmp_path / "网盘"
    full(D0, data=root, backup=where)
    corrupt(next(p for p in (where / "full" / D0.isoformat()).rglob("*.parquet")))
    result = verify(where, clean_zone=ZONE, into=tmp_path / "演练", as_of=D0)
    assert not result.ok
    assert len(result.restored.bad) == 1
    assert "## 备份里那些读坏了的文件" in result.markdown
    assert "哈希对不上 1 个" in result.markdown
    assert "· 不通过" in result.markdown


def test_bytes_that_match_the_ledger_but_are_not_a_parquet_still_fail_the_drill(
    data: Path, backup: Path
) -> None:
    """哈希对得上而查询层读不出行：这是 `depth()` 那一路独有的坏法，演练要把它说成结论。

    夹具里那棵树是**假**的 `.parquet`（几个字的文本），所以它天然就是这个场景：每一个字节都与
    清单一致（`intact`），而 DuckDB 认不出 footer。没有这条，"字节一致而读不出来"的备份会判成通过。
    """
    full(D0, data=data, backup=backup)
    result = verify(backup, clean_zone=ZONE, into=backup.parent / "演练", as_of=D0)
    assert result.restored.intact, "前提：这一次哈希是全对得上的"
    assert not result.ok
    assert result.cover is None
    assert result.read_error
    assert "读不出行" in result.markdown
    assert "· 不通过" in result.markdown


def test_a_drill_with_nothing_to_read_is_not_a_pass(tmp_path: Path) -> None:
    """干净区里一个已知 dataset 都没有 → 这不是"通过得很难看"，是演练根本没做成。"""
    root = tmp_path / "zhixing_data"
    write_tree(root, {"reports/2026-09-19.md": "日报"})
    where = tmp_path / "网盘"
    full(D0, data=root, backup=where)
    with pytest.raises(NothingToVerify, match="没有东西可验"):
        verify(where, clean_zone=ZONE, into=tmp_path / "演练", as_of=D0)


def test_naming_a_dataset_that_was_never_backed_up_fails_the_drill(
    data: Path, backup: Path
) -> None:
    """点名要演练一个备份里没有的 dataset：`depth()` 读不到东西，判不通过——不偷偷换成别的那个。"""
    full(D0, data=data, backup=backup)
    result = verify(
        backup,
        clean_zone=ZONE,
        into=backup.parent / "演练",
        dataset=layout.MINUTE_5,
        as_of=D0,
    )
    assert not result.ok
    assert result.cover is None
    assert "恢复出来的干净区是空的" in result.markdown


def test_the_report_says_which_generations_were_stacked(tmp_path: Path) -> None:
    """报告要说清"拼了哪几代"：只说日期的话，看报告的人不知道基线是哪一代、有没有裁过。"""
    root = real_root(tmp_path / "zhixing_data", (D0, D1))
    where = tmp_path / "网盘"
    full(D0, data=root, backup=where)
    (root / "新.txt").write_text("一", encoding="utf-8")
    incremental(D1, data=root, backup=where)
    result = verify(where, clean_zone=ZONE, into=tmp_path / "演练", as_of=D1)
    assert result.ok
    assert "全量 2026-09-20（其后的增量：2026-09-21）" in result.markdown
    # 演练只圈那一个 dataset，所以报告里的"恢复出几个文件"是它的份数，不是整棵树的
    assert "恢复出 1 个文件" in result.markdown
    assert result.cover is not None and result.cover.days == 2
    assert state.load_state(where).base == D0, "演练不许动备份根的账"
