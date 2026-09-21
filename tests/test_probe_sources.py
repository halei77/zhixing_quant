"""probe_sources 的离线冒烟：钉"工具还跑得起来"与结果记录语义，不钉网络——
真探测不进 CI（坑 #38 的免疫写法，同 tests/test_bench_storage.py 的理由）。"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import probe_sources as probe  # noqa: E402  # tools/ 不在包内，按脚本路径导入

from zhixing_quant import config  # noqa: E402


def test_list_mode_prints_every_probe(capsys: pytest.CaptureFixture[str]) -> None:
    assert probe.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "profit_forecast_em" in out
    assert "rds.daily_anchor" in out
    assert "promax.cyq_perf" in out


def test_an_unknown_only_refuses_to_start(capsys: pytest.CaptureFixture[str]) -> None:
    assert probe.main(["--only", "不存在的东西"]) == 2
    assert "没有匹配" in capsys.readouterr().err


def test_first_working_variant_wins_and_failures_are_kept() -> None:
    probe_ = probe.Probe(
        name="x",
        bucket="A-akshare",
        note="t",
        attempts=(
            probe.Attempt("坏变体", "nope", {}),
            probe.Attempt("好变体", "yep", {"k": 1}),
        ),
    )
    calls: list[str] = []

    def invoke(label: str, target: str, _kwargs: Mapping[str, object]) -> dict[str, Any]:
        calls.append(label)
        if target == "nope":
            raise ConnectionError("源挂了")
        return {"rows": 3, "columns": ["a"], "sample": {"a": "1"}}

    result = probe.run_probe(probe_, invoke)
    assert result.ok and result.winner == "好变体" and result.rows == 3
    assert calls == ["坏变体", "好变体"], "坏变体失败后应继续试下一个"
    assert any("ConnectionError" in e for e in result.errors), "错话要留档，不许只留成功格"


def test_a_probe_where_every_variant_fails_is_a_result_not_a_crash() -> None:
    probe_ = probe.Probe(
        name="全灭",
        bucket="B-rds",
        note="t",
        attempts=(probe.Attempt("唯一", "nope", {}),),
    )

    def invoke(_label: str, _target: str, _kwargs: Mapping[str, object]) -> dict[str, Any]:
        raise TimeoutError("掐表")

    result = probe.run_probe(probe_, invoke)
    assert not result.ok and result.rows is None
    assert any("TimeoutError" in e for e in result.errors)


def test_report_lands_one_json_per_result(tmp_path: Path) -> None:
    ok = probe.Result("ok", "A-akshare", "", True, "默认", 2)
    bad = probe.Result("bad", "B-rds", "", False, None, None, errors=[" boom"])
    summary = probe.write_report([ok, bad], tmp_path, as_of=date(2026, 9, 21))
    assert summary.is_file()
    text = summary.read_text(encoding="utf-8")
    assert "| ok | A-akshare | ✅ | 2 |" in text
    assert "| bad | B-rds | ❌ | — |" in text
    assert json.loads((tmp_path / "ok.json").read_text(encoding="utf-8"))["winner"] == "默认"


def test_relay_key_file_must_end_with_key() -> None:
    with pytest.raises(ValueError, match=r"\.key"):
        config.relay_key_file("rds")


def test_relay_key_dir_defaults_under_home() -> None:
    assert config.relay_key_dir({}) == Path("~/.zhixing_secrets").expanduser()
    assert config.relay_key_dir({"ZX_RELAY_KEY_DIR": "/tmp/x"}) == Path("/tmp/x")
