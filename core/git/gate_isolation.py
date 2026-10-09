"""Official gate on an immutable candidate in an isolated worktree (USR-175).

Incident (USR-134, 2026-10-08): the gate was run by hand in the SHARED checkout; ``main`` advanced
(0d71e59 -> 72c40a4, PR #214) while the suite ran and the harness correctly refused to emit
evidence ("Candidate HEAD changed during harness execution"). That refusal stays: this module
removes the *cause* instead of relaxing the check.

:func:`run_isolated_gate` resolves the candidate to a SHA, creates a **detached** worktree pinned
to it (``git worktree add --detach``; no branch exists for a concurrent session to move), runs
the official gate THERE and removes the worktree afterwards. The shared checkout is never
touched: not its HEAD, index or working files.

Success requires, in addition to the gate's own exit code, that the candidate is unchanged after
the run (HEAD still equals the pinned SHA and the worktree is clean). A candidate that moves or
is dirtied therefore never yields a success result, mirroring the harness's fail-closed refusal.

Stdlib-only; reuses :mod:`core.git.ticket_workspace` for worktree plumbing and reliable cleanup.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from core.git import ticket_workspace as tw

logger = logging.getLogger("darkfac.git.gate_isolation")

GATE_WORKTREE_PREFIX = "gate"
EXIT_SETUP_ERROR = 2
EXIT_CANDIDATE_MOVED = 3
# Environment variables that would make the runner inspect another checkout than the isolated one.
SCRUBBED_ENV = ("DARKFAC_PROJECT_ROOT", "DARKFAC_STATE_ROOT", "DARKFAC_HARNESS_STATE_DIR")

CommandBuilder = Callable[[Path], list[str]]


@dataclass
class IsolatedGateResult:
    """Outcome of one isolated gate run. ``ok`` is True only for a trustworthy success."""

    ok: bool
    returncode: int
    candidate_sha: str
    head_after: str
    worktree: Path
    cleaned: bool
    reason: str = ""


def default_gate_command(worktree: Path) -> list[str]:
    """The single official gate, executed from the isolated worktree's own copy of the runner."""
    return [sys.executable, str(worktree / "core" / "harness" / "runner.py"), "--quick"]


def _setup_error(reason: str, worktree: Path, sha: str = "") -> IsolatedGateResult:
    return IsolatedGateResult(False, EXIT_SETUP_ERROR, sha, "", worktree, False, reason)


def _scrubbed_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in SCRUBBED_ENV}


def _verify_candidate_intact(worktree: Path, sha: str, run_git: tw.GitRunner) -> tuple[str, str]:
    """Return ``(head_after, problem)``; ``problem`` is empty when the candidate is untouched."""
    head = run_git(["rev-parse", "HEAD"], worktree)
    head_after = head.stdout.strip().lower() if head.returncode == 0 else ""
    if head_after != sha:
        return head_after, (
            f"Candidate HEAD changed during gate execution ({sha[:12]} -> {head_after[:12] or 'unknown'}); "
            "refusing to report success"
        )
    status = run_git(["status", "--porcelain", "--untracked-files=all"], worktree)
    if status.returncode != 0 or status.stdout.strip():
        return head_after, "Candidate worktree is dirty after gate execution; refusing to report success"
    return head_after, ""


def run_isolated_gate(
    *,
    cwd: Path,
    ref: str = "HEAD",
    command_builder: Optional[CommandBuilder] = None,
    timeout_s: Optional[float] = None,
    run_git: Optional[tw.GitRunner] = None,
) -> IsolatedGateResult:
    """Run the official gate on ``ref`` in a detached, throwaway worktree.

    ``cwd`` is any directory of the repository (primary checkout or a linked worktree).
    Never raises for expected failures; inspect :attr:`IsolatedGateResult.ok` / ``reason``.
    """
    runner = run_git or tw._git
    try:
        main_root = tw.main_checkout_root(cwd, runner)
    except tw.WorkspaceError as exc:
        return _setup_error(str(exc), cwd)

    resolved = runner(["rev-parse", "--verify", f"{ref}^{{commit}}"], main_root)
    if resolved.returncode != 0:
        return _setup_error(f"cannot resolve ref {ref!r}: {resolved.stderr.strip()}", main_root)
    sha = resolved.stdout.strip().lower()

    tw.ensure_local_excludes(main_root, runner)
    root_dir = main_root / tw.WORKTREES_DIRNAME
    root_dir.mkdir(parents=True, exist_ok=True)
    stamp = tw._stamp(tw.utc_now())
    name, suffix = f"{GATE_WORKTREE_PREFIX}-{sha[:12]}-{stamp}", 1
    while (root_dir / name).exists():
        suffix += 1
        name = f"{GATE_WORKTREE_PREFIX}-{sha[:12]}-{stamp}-{suffix}"
    worktree = root_dir / name

    added = runner(["worktree", "add", "--detach", str(worktree), sha], main_root)
    if added.returncode != 0:
        if worktree.exists():
            tw.cleanup(worktree, main_root, sleep_fn=lambda _s: None, run_git=run_git, retries=1)
        return _setup_error(f"git worktree add --detach failed: {added.stderr.strip()}", worktree, sha)

    head_after = ""
    result: IsolatedGateResult
    try:
        command = (command_builder or default_gate_command)(worktree)
        logger.info("Isolated gate on %s in %s: %s", sha[:12], worktree, command)
        try:
            completed = subprocess.run(
                command, cwd=str(worktree), env=_scrubbed_env(), check=False, timeout=timeout_s
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            returncode = 124
        except OSError as exc:
            returncode = EXIT_SETUP_ERROR
            logger.error("Could not start gate command: %s", exc)

        head_after, problem = _verify_candidate_intact(worktree, sha, runner)
        if problem:
            # Even a gate exit of 0 is discarded: the evidence no longer binds to the candidate.
            result = IsolatedGateResult(False, EXIT_CANDIDATE_MOVED, sha, head_after, worktree, False, problem)
        elif returncode != 0:
            result = IsolatedGateResult(
                False, returncode, sha, head_after, worktree, False, f"gate exited with code {returncode}"
            )
        else:
            result = IsolatedGateResult(True, 0, sha, head_after, worktree, False)
    finally:
        cleaned = tw.cleanup(worktree, main_root, run_git=run_git).removed
    result.cleaned = cleaned
    return result


def format_summary(result: IsolatedGateResult) -> str:
    verdict = "ISOLATED_GATE_PASS" if result.ok else "ISOLATED_GATE_FAIL"
    lines = [
        f"[{verdict}] candidate={result.candidate_sha[:12]} head_after={result.head_after[:12] or '-'} "
        f"exit={result.returncode} worktree_removed={result.cleaned}"
    ]
    if result.reason:
        lines.append(f"[ISOLATED_GATE] {result.reason}")
    return "\n".join(lines)
