"""USR-231: worktree cleanup must not run with the process cwd INSIDE the worktree.

On Windows a process whose cwd is a directory holds a handle on it, so ``git worktree remove``,
``rmtree`` and the trash rename all fail with WinError 32 against the worktree the caller is
standing in (``run_ticket --resume-delivery`` launched from inside the worktree). ``cleanup``
now moves the cwd to the main checkout first and restores it afterwards when it still exists.
"""

from __future__ import annotations

import os
import subprocess
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


def test_cleanup_runs_every_git_call_with_the_process_cwd_outside_the_worktree(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: F811
    ws = create_workspace("USR-231", cwd=repo.local)
    # Exercise _outside_dir independently from the live-holder preservation gate.
    monkeypatch.setattr(ticket_workspace, "_find_holders", lambda _path: [])
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


def test_cleanup_preserves_worktree_while_a_process_cwd_is_inside_it(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: F811
    ws = create_workspace("USR-231", cwd=repo.local)
    (ws.path / "sub").mkdir()
    (ws.path / "sub" / "work.txt").write_text("w\n", encoding="utf-8")
    os.chdir(ws.path / "sub")

    holders = ["powershell.exe (pid 17192)"]
    monkeypatch.setattr(ticket_workspace, "_find_holders", lambda _path: list(holders))
    git_calls: list[Sequence[str]] = []

    def fail_if_git_mutates(args: Sequence[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
        git_calls.append(args)
        raise AssertionError(f"cleanup must not run git while occupied: {args!r} in {cwd}")

    result = cleanup(ws.path, ws.main_root, sleep_fn=lambda _s: None, run_git=fail_if_git_mutates)

    assert not result.removed and result.method == "preserved"
    assert "powershell.exe (pid 17192)" in result.detail
    assert ws.path.exists() and (ws.path / "sub" / "work.txt").read_text(encoding="utf-8") == "w\n"
    assert _same(os.getcwd(), ws.path / "sub")
    assert any(
        _same(ws.path, entry["worktree"])
        for entry in ticket_workspace.list_worktrees(ws.main_root)
    )
    assert ticket_workspace._git(["branch", "--show-current"], ws.path).stdout.strip() == ws.branch
    assert not git_calls

    # Once the holder exits, the same checkout is eligible for the normal cleanup path.
    holders.clear()
    result = cleanup(ws.path, ws.main_root, sleep_fn=lambda _s: None)
    assert result.removed and result.method in {"git", "rmtree"}, (result.method, result.errors)
    assert not ws.path.exists()


def test_cleanup_failure_warning_names_the_holder_and_the_exact_command(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    ws = create_workspace("USR-231", cwd=repo.local)

    def refuse(path: Path, main_root: Path, **_kw: object) -> Path:
        raise PermissionError("[WinError 32] in use")

    monkeypatch.setattr(ticket_workspace, "trash_directory", refuse)
    holder_checks = iter(([], ["Code.exe (pid 4242)"]))
    monkeypatch.setattr(ticket_workspace, "_find_holders", lambda _p: next(holder_checks))

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
