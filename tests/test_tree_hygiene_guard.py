"""Contract of the tree-hygiene guard wired into tests/conftest.py.

Every scenario runs against a throwaway git repository under ``tmp_path`` so
nothing here can touch (or depend on) the real checkout.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests import _tree_hygiene


def _git(repo: Path, *args: str) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A committed repo with one tracked file and an ignored ``cache/`` directory."""

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Tree Hygiene Test")
    _git(root, "config", "user.email", "tree-hygiene@example.invalid")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitignore").write_text("cache/\n", encoding="utf-8", newline="\n")
    (root / "tracked.txt").write_text("original\n", encoding="utf-8", newline="\n")
    _git(root, "add", ".gitignore", "tracked.txt")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _session(root: Path) -> _tree_hygiene.Fingerprint:
    fingerprint = _tree_hygiene.take_fingerprint(root)
    assert fingerprint is not None
    return fingerprint


def _violations(root: Path, before: _tree_hygiene.Fingerprint) -> list[tuple[str, str, str]]:
    found = _tree_hygiene.diff_fingerprints(before, _session(root))
    return [(item.kind, item.status, item.path) for item in found]


def test_clean_tree_untouched_reports_nothing(repo: Path) -> None:
    before = _session(repo)

    assert before == {}
    assert _violations(repo, before) == []


def test_modified_tracked_file_is_reported(repo: Path) -> None:
    before = _session(repo)

    (repo / "tracked.txt").write_text("edited by a test\n", encoding="utf-8", newline="\n")

    assert _violations(repo, before) == [("new", " M", "tracked.txt")]


def test_new_unignored_file_is_reported(repo: Path) -> None:
    before = _session(repo)

    nested = repo / "state" / "deep"
    nested.mkdir(parents=True)
    (nested / "leak.json").write_text("{}", encoding="utf-8", newline="\n")

    assert _violations(repo, before) == [("new", "??", "state/deep/leak.json")]


def test_new_file_in_ignored_directory_is_not_reported(repo: Path) -> None:
    before = _session(repo)

    (repo / "cache").mkdir()
    (repo / "cache" / "artifact.bin").write_bytes(b"\x00\x01")

    assert _violations(repo, before) == []


def test_preexisting_dirt_left_unchanged_is_not_reported(repo: Path) -> None:
    (repo / "tracked.txt").write_text("dirty before the session\n", encoding="utf-8", newline="\n")
    (repo / "scratch.txt").write_text("untracked before the session\n", encoding="utf-8", newline="\n")
    before = _session(repo)

    assert set(before) == {"tracked.txt", "scratch.txt"}
    assert _violations(repo, before) == []


def test_preexisting_dirt_rewritten_during_session_is_reported(repo: Path) -> None:
    (repo / "tracked.txt").write_text("dirty before the session\n", encoding="utf-8", newline="\n")
    before = _session(repo)

    (repo / "tracked.txt").write_text("rewritten by a test\n", encoding="utf-8", newline="\n")

    assert _violations(repo, before) == [("changed", " M", "tracked.txt")]


def test_removed_tracked_file_is_reported(repo: Path) -> None:
    before = _session(repo)

    (repo / "tracked.txt").unlink()

    assert _violations(repo, before) == [("new", " D", "tracked.txt")]


def test_preexisting_untracked_file_removed_during_session_is_reported(repo: Path) -> None:
    scratch = repo / "scratch.txt"
    scratch.write_text("untracked before the session\n", encoding="utf-8", newline="\n")
    before = _session(repo)

    scratch.unlink()

    assert _violations(repo, before) == [("restored", "--", "scratch.txt")]


def test_oversized_file_change_is_detected_by_size_and_head(repo: Path) -> None:
    big = repo / "big.bin"
    big.write_bytes(b"a" * 64)
    before = _tree_hygiene.take_fingerprint(repo, max_hash_bytes=16)
    assert before is not None

    big.write_bytes(b"a" * 65)

    after = _tree_hygiene.take_fingerprint(repo, max_hash_bytes=16)
    assert after is not None
    assert [item.path for item in _tree_hygiene.diff_fingerprints(before, after)] == ["big.bin"]


def test_renamed_file_is_tracked_under_its_destination(repo: Path) -> None:
    _git(repo, "mv", "tracked.txt", "moved.txt")

    fingerprint = _session(repo)

    assert list(fingerprint) == ["moved.txt"]
    assert fingerprint["moved.txt"][0].startswith("R")


def test_outside_a_repository_the_guard_is_disabled(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()

    assert _tree_hygiene.take_fingerprint(plain) is None


def test_missing_git_binary_disables_the_guard(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(_tree_hygiene.subprocess, "run", no_git)

    assert _tree_hygiene.take_fingerprint(repo) is None


def test_git_timeout_disables_the_guard(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(_tree_hygiene.subprocess, "run", hang)

    assert _tree_hygiene.take_fingerprint(repo) is None


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "no"])
def test_env_switch_disables_the_guard(value: str) -> None:
    assert _tree_hygiene.is_disabled({_tree_hygiene.ENV_SWITCH: value})


@pytest.mark.parametrize("environ", [{}, {_tree_hygiene.ENV_SWITCH: "on"}, {_tree_hygiene.ENV_SWITCH: ""}])
def test_guard_is_enabled_by_default(environ: dict[str, str]) -> None:
    assert not _tree_hygiene.is_disabled(environ)


def test_nested_session_is_detected_from_the_active_marker() -> None:
    assert not _tree_hygiene.is_nested_session({})
    assert _tree_hygiene.is_nested_session({_tree_hygiene.ENV_ACTIVE: "1"})


def test_report_has_one_greppable_line_per_path_and_the_remedy() -> None:
    lines = _tree_hygiene.format_report(
        [
            _tree_hygiene.Violation("new", "??", "a/b.json"),
            _tree_hygiene.Violation("changed", " M", "c.txt"),
        ]
    )

    assert lines[0] == "[TREE-HYGIENE] new ?? a/b.json"
    assert lines[1] == "[TREE-HYGIENE] changed  M c.txt"
    assert "tmp_path" in lines[-1]
    assert all(line.startswith("[TREE-HYGIENE]") for line in lines)
