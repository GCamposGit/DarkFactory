"""USR-231: worktree cleanup must not run with the process cwd INSIDE the worktree.

On Windows a process whose cwd is a directory holds a handle on it, so ``git worktree remove``,
``rmtree`` and the trash rename all fail with WinError 32 against the worktree the caller is
standing in (``run_ticket --resume-delivery`` launched from inside the worktree). ``cleanup``
now moves the cwd to the main checkout first and restores it afterwards when it still exists.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Sequence

import pytest

from core.git import ticket_workspace
from core.git.ticket_workspace import (
    _outside_dir,
    cleanup,
    create_workspace,
    removal_hint,
)
from tests.test_ticket_workspace import FlakyGit, _locked_rmtree, repo  # noqa: F401  (fixture re-export)


@pytest.fixture(autouse=True)
def _restore_cwd() -> Iterator[None]:
    before = os.getcwd()
    yield
    os.chdir(before)


def _same(a: Path | str, b: Path | str) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def test_outside_dir_moves_cwd_out_and_restores_it_when_it_still_exists(tmp_path: Path) -> None:
    inside, fallback = tmp_path / "wt" / "sub", tmp_path / "main"
    inside.mkdir(parents=True)
    fallback.mkdir()
    os.chdir(inside)

    with _outside_dir(tmp_path / "wt", fallback):
        assert _same(os.getcwd(), fallback)

    assert _same(os.getcwd(), inside)  # still exists: restored


def test_outside_dir_leaves_an_unrelated_cwd_alone(tmp_path: Path) -> None:
    elsewhere, wt, fallback = tmp_path / "elsewhere", tmp_path / "wt", tmp_path / "main"
    for d in (elsewhere, wt, fallback):
        d.mkdir()
    os.chdir(elsewhere)
    with _outside_dir(wt, fallback):
        assert _same(os.getcwd(), elsewhere)
    assert _same(os.getcwd(), elsewhere)


def test_outside_dir_does_not_mistake_a_sibling_with_the_same_prefix_for_inside(tmp_path: Path) -> None:
    wt, sibling, fallback = tmp_path / "usr-1", tmp_path / "usr-10", tmp_path / "main"
    for d in (wt, sibling, fallback):
        d.mkdir()
    os.chdir(sibling)
    with _outside_dir(wt, fallback):
        assert _same(os.getcwd(), sibling)


def test_outside_dir_restores_cwd_even_when_the_body_raises(tmp_path: Path) -> None:
    inside, fallback = tmp_path / "wt", tmp_path / "main"
    inside.mkdir()
    fallback.mkdir()
    os.chdir(inside)

    with pytest.raises(RuntimeError, match="boom"):
        with _outside_dir(inside, fallback):
            assert _same(os.getcwd(), fallback)
            raise RuntimeError("boom")

    assert _same(os.getcwd(), inside)


def test_outside_dir_stays_out_when_the_previous_cwd_was_deleted(tmp_path: Path) -> None:
    inside, fallback = tmp_path / "wt", tmp_path / "main"
    inside.mkdir()
    fallback.mkdir()
    os.chdir(inside)

    with _outside_dir(inside, fallback):
        os.rmdir(inside)  # cwd is no longer inside the body: the directory is gone

    assert _same(os.getcwd(), fallback)


def test_cleanup_runs_every_git_call_with_the_process_cwd_outside_the_worktree(repo: SimpleNamespace) -> None:  # noqa: F811
    ws = create_workspace("USR-231", cwd=repo.local)
    seen: list[str] = []

    def spy(args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        seen.append(os.getcwd())
        return ticket_workspace._git(args, cwd)

    os.chdir(ws.path)
    result = cleanup(ws.path, ws.main_root, sleep_fn=lambda _s: None, run_git=spy)

    assert seen, "git was never invoked"
    assert all(not ticket_workspace._is_within(cwd, ws.path) for cwd in seen)  # Linux and Windows
    assert result.removed
    assert not ws.path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="the cwd handle only blocks removal on Windows")
def test_cleanup_with_cwd_inside_the_worktree_removes_it_without_winerror_32(repo: SimpleNamespace) -> None:  # noqa: F811
    ws = create_workspace("USR-231", cwd=repo.local)
    (ws.path / "sub").mkdir()
    (ws.path / "sub" / "work.txt").write_text("w\n", encoding="utf-8")
    os.chdir(ws.path / "sub")  # exactly the `--resume-delivery` launched from inside the worktree

    result = cleanup(ws.path, ws.main_root, sleep_fn=lambda _s: None)

    assert result.removed and result.method in {"git", "rmtree"}, (result.method, result.errors)
    assert not ws.path.exists()
    assert not any("WinError 32" in e or "Permission denied" in e for e in result.errors)
    assert _same(os.getcwd(), ws.main_root)  # the old cwd is gone, so we stay at the main checkout


def test_cleanup_failure_warning_names_the_holder_and_the_exact_command(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    ws = create_workspace("USR-231", cwd=repo.local)

    def refuse(path: Path, main_root: Path, **_kw: object) -> Path:
        raise PermissionError("[WinError 32] in use")

    monkeypatch.setattr(ticket_workspace, "trash_directory", refuse)
    monkeypatch.setattr(ticket_workspace, "_find_holders", lambda _p: ["Code.exe (pid 4242)"])

    result = cleanup(
        ws.path, ws.main_root, sleep_fn=lambda _s: None, run_git=FlakyGit(always=True), rmtree_fn=_locked_rmtree
    )

    assert not result.removed
    assert "Code.exe (pid 4242)" in result.detail
    assert f'Remove-Item -LiteralPath "{ws.path}" -Recurse -Force' in result.detail
    assert f'worktree remove --force "{ws.path}"' in result.detail


def test_removal_hint_without_a_known_holder_still_gives_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ticket_workspace, "_find_holders", lambda _p: [])
    hint = removal_hint(tmp_path / "wt", tmp_path)
    assert "handle or cwd" in hint and "Remove-Item" in hint and str(tmp_path / "wt") in hint
