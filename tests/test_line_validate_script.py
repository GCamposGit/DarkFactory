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
    monkeypatch.setenv("DOKPLOY_API_KEY", "prod-key")
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(line_validate.subprocess, "call", lambda argv, **kw: calls.append((list(argv), kw)) or 0)

    assert line_validate.main(root) == 0
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == sys.executable
    assert argv[1].endswith("runner.py") and argv[-1] == "--quick"
    assert "DOKPLOY_API_KEY" not in kwargs["env"]


def test_sanitized_env_drops_production_credentials_but_keeps_test_worker_dispatch(line_validate) -> None:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/darkfac",
        "DARKFAC_HF02_DATABASE_URL": "postgresql://u:p@db:5432/x",
        "DARKHUB_CONTROL_DATABASE_URL": "postgresql://u:p@db:5432/x",
        "DOKPLOY_API_URL": "https://dokploy.example",
        "DOKPLOY_API_KEY": "k",
        "TELEGRAM_OWNER_BOT_TOKEN": "t",
        "GITHUB_TOKEN": "g",
        "GITHUB_PAT": "g",
        "CLAUDE_CODE_OAUTH_TOKEN": "c",
        "OPENAI_API_KEY": "o",
        "R2_ACCESS_KEY_ID": "r",
        "DARKFAC_BACKUP_ENCRYPTION_KEY": "e",
        "DARKFAC_CANARY_BASE_URL": "https://canary.example",
        "DARKFAC_WORKER_TOKEN": "w",
        "DARKFAC_TEST_WORKERS": "http://100.78.181.90:8080",
        "DARKFAC_REMOTE_BUSY_WAIT_SEC": "300",
    }
    env = line_validate.sanitized_env(base)
    assert set(env) == {
        "PATH",
        "HOME",
        "DARKFAC_WORKER_TOKEN",
        "DARKFAC_TEST_WORKERS",
        "DARKFAC_REMOTE_BUSY_WAIT_SEC",
    }


def test_dirty_tree_steps_do_not_see_production_credentials(line_validate, tmp_path: Path, monkeypatch) -> None:
    cmd = "python -c \"import os; open('seen', 'w').write(os.environ.get('DARKFAC_HF02_DATABASE_URL', 'absent'))\""
    root = _repo(tmp_path, [{"name": "probe", "cmd": cmd, "quick": True}])
    (root / "dirty.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://u:p@db:5432/x")

    assert line_validate.main(root) == 0
    assert (root / "seen").read_text(encoding="utf-8") == "absent"
