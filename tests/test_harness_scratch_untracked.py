"""USR-429: harness scratch under the repository-root ``.tmp/`` stays untracked.

The rule is anchored (``/.tmp/``) in ``.gitignore`` and repeated in
``LOCAL_EXCLUDE_ENTRIES`` so a clone that has not picked up the ignore file yet
still hides a new ``.tmp/`` once ``ensure_local_excludes`` runs. The allowlist of
tracked-and-ignored paths does not grow to excuse these files.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from core.git.ticket_workspace import (
    LOCAL_EXCLUDE_ENTRIES,
    drop_delivery_scratch_commands,
    is_delivery_scratch,
    without_delivery_scratch,
)

REPO = Path(__file__).resolve().parents[1]
SCRATCH = (
    ".tmp/harness-state-usr-423/run_timings.jsonl",
    ".tmp/harness-state-usr-423/suite-0.lock",
)
ALLOWLIST = REPO / "tests" / "data" / "tracked_ignored_allowlist.txt"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        ["git", "-c", "core.excludesFile=", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def test_tmp_is_absent_from_the_index() -> None:
    listed = _git("ls-files", "--", ".tmp")
    assert listed.returncode == 0, listed.stderr
    assert listed.stdout.strip() == ""


def test_check_ignore_no_index_covers_harness_scratch() -> None:
    result = _git("check-ignore", "--no-index", "--", *SCRATCH)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == list(SCRATCH)


def test_check_ignore_no_index_leaves_nested_tmp_visible() -> None:
    result = _git("check-ignore", "--no-index", "--", "src/.tmp/keep.txt")
    assert result.returncode == 1, result.stdout
    assert result.stdout.strip() == ""


def test_gitignore_and_local_exclude_anchor_tmp_and_the_allowlist_does_not() -> None:
    gitignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/.tmp/" in gitignore
    assert "/.tmp/" in LOCAL_EXCLUDE_ENTRIES
    allowlist = ALLOWLIST.read_text(encoding="utf-8")
    assert ".tmp" not in allowlist
    for path in SCRATCH:
        assert path not in allowlist


@pytest.mark.parametrize(
    "path",
    [
        ".tmp",
        ".tmp/",
        ".tmp/harness-state-usr-423/run_timings.jsonl",
        ".tmp/harness-state-usr-423/suite-0.lock",
        ".tmp/foo",
        ".tmp\\harness-state-usr-423\\suite-0.lock",
    ],
)
def test_is_delivery_scratch_is_root_tmp_only(path: str) -> None:
    assert is_delivery_scratch(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "",
        "tmp",
        ".tmpfile",
        ".tmp2/foo",
        "src/.tmp/keep.txt",
        "foo/.tmp",
        "/.tmp",
        "/.tmp/foo",
        ".tmp/../secret",
        "../.tmp/foo",
        "C:/repo/.tmp/foo",
        "C:\\repo\\.tmp\\foo",
        ".TMP/foo",
        "./.tmp/foo",
        "product.txt",
    ],
)
def test_is_delivery_scratch_rejects_every_other_path(path: str) -> None:
    assert is_delivery_scratch(path) is False


def test_drop_commands_and_filter_keep_nested_tmp() -> None:
    blob = "\0".join(
        [
            ".tmp/harness-state-usr-423/run_timings.jsonl",
            "src/.tmp/keep.txt",
            ".tmpfile",
            ".tmp/harness-state-usr-423/suite-0.lock",
        ]
    )
    assert drop_delivery_scratch_commands(blob) == [
        [
            "update-index",
            "--force-remove",
            "--",
            ".tmp/harness-state-usr-423/run_timings.jsonl",
            ".tmp/harness-state-usr-423/suite-0.lock",
        ]
    ]
    assert without_delivery_scratch(
        ["product.txt", ".tmp/harness-state-usr-423/run_timings.jsonl", "src/.tmp/keep.txt"]
    ) == ["product.txt", "src/.tmp/keep.txt"]
