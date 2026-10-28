"""USR-343 S4b: daily ledger snapshot (one commit) and node_sync with a locally modified ledger (USR-374)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.infra.node_sync import GENERATED_LEDGER_FILES, remediate_dirty_checkout, reset_generated_ledgers, run_git
from scripts.snapshot_queue import DEMANDS_RELATIVE, OWNER_ACTIONS_RELATIVE, render_ledgers, run_snapshot


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def clone(tmp_path: Path) -> tuple[Path, Path]:
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(bare), str(seed))
    for cfg in (("user.name", "T"), ("user.email", "t@darkfac.local")):
        _git(seed, "config", *cfg)
    for rel in GENERATED_LEDGER_FILES:
        (seed / rel).parent.mkdir(parents=True, exist_ok=True)
        (seed / rel).write_text("[]\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-qm", "seed")
    _git(seed, "push", "-q", "origin", "HEAD:main")
    return seed, bare


def test_snapshot_produces_exactly_one_commit_and_is_idempotent(
    clone: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, bare = clone
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    files = {DEMANDS_RELATIVE: '[{"id": "USR-1"}]\n', OWNER_ACTIONS_RELATIVE: '{"actions": []}\n'}
    before = _git(bare, "rev-list", "--count", "main")

    first = run_snapshot(seed, files=files, token="", today="2030-10-24")
    assert first.ok and first.action == "pushed", first.message
    assert int(_git(bare, "rev-list", "--count", "main")) == int(before) + 1
    assert _git(bare, "show", f"main:{DEMANDS_RELATIVE}") == '[{"id": "USR-1"}]'
    assert "snapshot diario" in _git(bare, "log", "-1", "--format=%s", "main")

    same_day = run_snapshot(seed, files=files, token="", today="2030-10-24")
    assert same_day.action == "skipped"
    next_day = run_snapshot(seed, files=files, token="", today="2030-10-25")
    assert next_day.action == "no_changes"
    assert int(_git(bare, "rev-list", "--count", "main")) == int(before) + 1


def test_render_ledgers_leaves_the_calling_checkout_untouched(
    clone: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, _ = clone
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    (seed / DEMANDS_RELATIVE).unlink()
    _git(seed, "commit", "-qam", "drop ledger")
    wt = tmp_path / "wt"
    (wt / DEMANDS_RELATIVE).parent.mkdir(parents=True)
    (wt / DEMANDS_RELATIVE).write_text("[]" + chr(10), encoding="utf-8")
    monkeypatch.chdir(seed)

    assert DEMANDS_RELATIVE in render_ledgers(wt)
    assert _git(seed, "status", "--porcelain") == ""
    assert not (seed / DEMANDS_RELATIVE).exists()


def test_node_sync_converges_with_a_dirty_owner_actions_ledger(
    clone: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed, bare = clone
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    node = tmp_path / "node"
    _git(tmp_path, "clone", "-q", str(bare), str(node))
    ledger = node / OWNER_ACTIONS_RELATIVE
    ledger.write_text('{"local": "edit"}\n', encoding="utf-8")  # the USR-374 situation
    (seed / OWNER_ACTIONS_RELATIVE).write_text('{"actions": [1]}\n', encoding="utf-8")
    _git(seed, "commit", "-qam", "snapshot")
    _git(seed, "push", "-q", "origin", "HEAD:main")

    dirty = {"tracked_modified": [OWNER_ACTIONS_RELATIVE], "untracked": []}
    assert remediate_dirty_checkout(node, run_git, dirty) is True
    backups = list((tmp_path / "state" / "ledger_backups").glob("owner_actions.json.*.bak"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == '{"local": "edit"}\n'
    assert _git(node, "status", "--porcelain") == ""
    _git(node, "pull", "-q", "--ff-only")
    assert ledger.read_text(encoding="utf-8") == '{"actions": [1]}\n'
    assert reset_generated_ledgers(node, backup_dir=tmp_path / "bk") == []
