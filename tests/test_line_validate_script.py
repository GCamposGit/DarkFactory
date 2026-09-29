"""scripts/line_validate.py: official runner on a clean tree, quick steps directly on a dirty one."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def line_validate():
    spec = importlib.util.spec_from_file_location("line_validate_under_test", REPO_ROOT / "scripts" / "line_validate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repo(tmp_path: Path, steps: list[dict]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "harness.config.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.test")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_is_clean_tree_detects_untracked_and_modified_files(line_validate, tmp_path: Path) -> None:
    root = _repo(tmp_path, [{"name": "s", "cmd": "python -c pass", "quick": True}])
    assert line_validate.is_clean_tree(root) is True
    (root / "new.txt").write_text("x", encoding="utf-8")
    assert line_validate.is_clean_tree(root) is False


def test_is_clean_tree_fails_closed_outside_a_repository(line_validate, tmp_path: Path) -> None:
    assert line_validate.is_clean_tree(tmp_path / "nowhere") is False


def test_dirty_tree_runs_quick_steps_directly_and_stops_at_first_failure(line_validate, tmp_path: Path, capsys) -> None:
    steps = [
        {"name": "ok", "cmd": "python -c \"open('ran_ok', 'w').close()\"", "quick": True},
        {"name": "skipped_not_quick", "cmd": "python -c \"open('ran_slow', 'w').close()\"", "quick": False},
        {"name": "boom", "cmd": "python -c \"import sys; sys.exit(3)\"", "quick": True},
        {"name": "never", "cmd": "python -c \"open('ran_never', 'w').close()\"", "quick": True},
    ]
    root = _repo(tmp_path, steps)
    (root / "dirty.txt").write_text("x", encoding="utf-8")

    assert line_validate.main(root) == 3
    assert (root / "ran_ok").exists()
    assert not (root / "ran_slow").exists()
    assert not (root / "ran_never").exists()
    assert "boom" in capsys.readouterr().out


def test_dirty_tree_passes_when_every_quick_step_passes(line_validate, tmp_path: Path) -> None:
    root = _repo(tmp_path, [{"name": "ok", "cmd": "python -c pass", "quick": True}])
    (root / "dirty.txt").write_text("x", encoding="utf-8")
    assert line_validate.main(root) == 0


def test_clean_tree_delegates_to_the_official_runner(line_validate, tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path, [{"name": "ok", "cmd": "python -c pass", "quick": True}])
    calls: list[list[str]] = []
    monkeypatch.setattr(line_validate.subprocess, "call", lambda argv, **kw: calls.append(list(argv)) or 0)

    assert line_validate.main(root) == 0
    assert len(calls) == 1
    assert calls[0][0] == sys.executable
    assert calls[0][1].endswith("runner.py") and calls[0][-1] == "--quick"
