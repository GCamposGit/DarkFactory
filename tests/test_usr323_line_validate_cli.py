"""USR-323: line_validate --help / unknown args must not snapshot or run the gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def lv(monkeypatch):
    spec = importlib.util.spec_from_file_location("lv_usr323", REPO_ROOT / "scripts" / "line_validate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls: list[str] = []

    def boom(name):
        def _f(*a, **k):
            calls.append(name)
            return 0

        return _f

    monkeypatch.setattr(module, "build_snapshot_commit", boom("snapshot"))
    monkeypatch.setattr(module, "run_official_runner_on_snapshot", boom("snapshot_run"))
    monkeypatch.setattr(module, "run_steps_directly", boom("direct"))
    monkeypatch.setattr(module, "is_clean_tree", boom("status"))
    monkeypatch.setattr(module.subprocess, "call", boom("runner"))
    monkeypatch.setattr(module.subprocess, "run", boom("subprocess"))
    module.calls = calls
    return module


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_exits_zero_without_side_effects(lv, flag, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        lv.main(REPO_ROOT, [flag])
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()
    assert lv.calls == []


def test_unknown_argument_exits_two_without_side_effects(lv) -> None:
    with pytest.raises(SystemExit) as exc:
        lv.main(REPO_ROOT, ["--bogus"])
    assert exc.value.code == 2
    assert lv.calls == []


def test_no_arguments_keeps_official_validation(lv) -> None:
    lv.is_clean_tree = lambda root: True
    assert lv.main(REPO_ROOT, []) == 0
    assert lv.calls == ["runner"]
