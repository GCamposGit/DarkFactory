"""Safe garbage collection of stale agent worktrees and merged local branches (USR-159).

Evidence (07-08/10/2026): dozens of worktrees from finished agent sessions piled up
under ``.claude/worktrees`` and ``.worktrees`` (some locked), and
``gh pr merge --delete-branch`` failed to remove them ("Directory not empty" on
Windows, caused by open handles).

This module classifies every worktree of a repository and removes ONLY the ones
that are provably finished:

* ``ACTIVE``       -- kept: open PR, in use by this process, modified within the last
  ``active_minutes``, uncommitted/untracked changes, a run in progress (injectable
  probe), or the state could not be determined (fail closed).
* ``LOCKED``       -- kept and reported: ``git worktree lock`` marks another session.
* ``MERGED``       -- removable: ``HEAD`` is an ancestor of the base ref, OR the
  branch's PR is MERGED/CLOSED and its head commit is exactly the local ``HEAD``
  (so nothing is lost with the local branch). Squash merges are covered by the PR
  path. The ``gh`` lookup is injectable and optional; no answer means "keep".
* ``UNMERGED``     -- kept: work not known to be integrated.
* ``OUT_OF_SCOPE`` -- the main checkout or a worktree outside the configured roots;
  never touched (listed so the operator sees it).

Removal reuses :func:`core.git.ticket_workspace.cleanup` (``git worktree remove`` with
retry/backoff for the transient Windows "Directory not empty"/"Permission denied",
a robust ``rmtree`` and, as a last resort, the recoverable trash), then
``git worktree prune`` and ``git branch -D`` of the merged local branch.

Dry-run is the default; ``--apply`` is required to remove anything.

CLI::

    python -m core.git.worktree_gc [--dry-run | --apply] [--json] [--root PATH ...]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from core.git import ticket_workspace
from core.git.ticket_workspace import GitRunner, SleepFn

logger = logging.getLogger("darkfac.git.worktree_gc")

DEFAULT_BASE_REF = "origin/main"
DEFAULT_ACTIVE_MINUTES = 60.0
DEFAULT_REMOVE_RETRIES = 4
DEFAULT_REMOVE_BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0)
_GH_TIMEOUT_S = 60.0
_PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "HEAD"})

ACTIVE = "ACTIVE"
LOCKED = "LOCKED"
MERGED = "MERGED"
UNMERGED = "UNMERGED"
OUT_OF_SCOPE = "OUT_OF_SCOPE"


@dataclass(frozen=True)
class PrInfo:
    """What the PR lookup knows about one pull request of a branch."""

    number: int
    state: str  # OPEN | MERGED | CLOSED (as reported by gh)
    head_sha: str = ""


# branch name -> PRs of that head branch; ``None`` means "unknown" (fail closed).
PrLookup = Callable[[str, Path], Optional[Sequence[PrInfo]]]
# worktree path -> True when a factory run is in progress there.
RunProbe = Callable[[Path], bool]
NowFn = Callable[[], datetime]


@dataclass
class WorktreeEntry:
    """Classification (and, with ``--apply``, the outcome) of one worktree."""

    path: str
    branch: str
    head: str
    state: str
    reason: str
    action: str = "keep"  # keep | remove
    delete_branch: bool = False  # only when integration of the branch is proven
    removed: bool = False
    branch_deleted: bool = False
    method: str = ""
    attempts: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class GcReport:
    """Structured result of :func:`run_gc`."""

    main_root: str
    base_ref: str
    apply: bool
    roots: list[str]
    entries: list[WorktreeEntry] = field(default_factory=list)
    pruned: bool = False
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for entry in self.entries:
            out[entry.state] = out.get(entry.state, 0) + 1
        return out

    @property
    def failed(self) -> list[WorktreeEntry]:
        return [e for e in self.entries if self.apply and e.action == "remove" and not e.removed]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["counts"] = self.counts()
        payload["removed"] = [e.path for e in self.entries if e.removed]
        payload["failed"] = [e.path for e in self.failed]
        return payload


# ----------------------------------------------------------------------
# Probes (all fail closed)
# ----------------------------------------------------------------------


def gh_pr_lookup(branch: str, cwd: Path) -> Optional[Sequence[PrInfo]]:
    """Look the PRs of ``branch`` up with the ``gh`` CLI; ``None`` when unavailable or failing."""
    if shutil.which("gh") is None:
        return None
    cmd = [
        "gh", "pr", "list", "--head", branch, "--state", "all", "--limit", "10",
        "--json", "number,state,headRefOid",
    ]
    try:
        res = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_GH_TIMEOUT_S, check=False, **ticket_workspace._win_kwargs(),
        )
        if res.returncode != 0:
            logger.warning("gh pr list failed for %s: %s", branch, (res.stderr or "").strip())
            return None
        rows = json.loads(res.stdout or "[]")
        return [
            PrInfo(int(r["number"]), str(r.get("state", "")).upper(), str(r.get("headRefOid") or ""))
            for r in rows
        ]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        logger.warning("gh pr lookup error for %s: %s", branch, exc)
        return None


def _branch_of(entry: dict[str, str]) -> str:
    ref = entry.get("branch", "")
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ""


def _is_under(path: Path, root: Path) -> bool:
    child, parent = ticket_workspace._norm(path), ticket_workspace._norm(root)
    return child != parent and child.startswith(parent.rstrip("\\/") + os.sep)


def _gitdir_mtime(path: Path, runner: GitRunner) -> float:
    """Newest mtime among HEAD/index of the worktree's private git dir (0.0 when unknown)."""
    res = runner(["rev-parse", "--absolute-git-dir"], path)
    if res.returncode != 0 or not res.stdout.strip():
        return 0.0
    gitdir = Path(res.stdout.strip())
    latest = 0.0
    for name in ("HEAD", "index"):
        try:
            latest = max(latest, (gitdir / name).stat().st_mtime)
        except OSError:
            continue
    return latest


def _is_clean(path: Path, runner: GitRunner) -> tuple[bool, str]:
    """Clean = no tracked changes AND no untracked non-ignored files; failure counts as dirty."""
    res = runner(["status", "--porcelain", "--untracked-files=normal"], path)
    if res.returncode != 0:
        return False, f"git status failed: {(res.stderr or res.stdout or '').strip()[:200]}"
    lines = [ln for ln in res.stdout.splitlines() if ln.strip()]
    if lines:
        return False, f"{len(lines)} uncommitted/untracked path(s), e.g. {lines[0].strip()[:120]}"
    return True, ""


def _is_ancestor(head: str, base_ref: str, main_root: Path, runner: GitRunner) -> Optional[bool]:
    """True/False from ``merge-base --is-ancestor``; ``None`` when the base ref is missing."""
    if runner(["rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"], main_root).returncode != 0:
        return None
    res = runner(["merge-base", "--is-ancestor", head, base_ref], main_root)
    if res.returncode == 0:
        return True
    return False if res.returncode == 1 else None


def _inside_cwd(path: Path) -> bool:
    try:
        cwd = ticket_workspace._norm(Path.cwd())
    except OSError:
        return False
    target = ticket_workspace._norm(path)
    return cwd == target or cwd.startswith(target.rstrip("\\/") + os.sep)


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------


def classify_worktree(
    entry: dict[str, str],
    main_root: Path,
    *,
    base_ref: str,
    active_minutes: float,
    now_fn: NowFn,
    runner: GitRunner,
    pr_lookup: Optional[PrLookup],
    run_probe: Optional[RunProbe],
) -> WorktreeEntry:
    """Classify one in-scope worktree (see the module docstring for the states)."""
    path = Path(entry["worktree"])
    branch, head = _branch_of(entry), entry.get("HEAD", "")
    out = WorktreeEntry(path=str(path), branch=branch, head=head, state=ACTIVE, reason="")

    if "locked" in entry:
        out.state = LOCKED
        out.reason = f"locked by another session: {entry.get('locked') or 'no reason given'}"
        return out
    if _inside_cwd(path):
        out.reason = "current working directory of this process"
        return out
    if not path.exists():
        # Registration of a vanished directory: harmless, pruned with the rest.
        out.state, out.reason, out.action = MERGED, "directory missing (stale registration; branch kept)", "remove"
        return out
    if not head:
        out.reason = "HEAD unknown (fail closed)"
        return out

    prs: Optional[Sequence[PrInfo]] = None
    if branch and pr_lookup is not None:
        try:
            prs = pr_lookup(branch, main_root)
        except Exception as exc:  # injected callables must never break the sweep
            logger.warning("PR lookup raised for %s: %s", branch, exc)
            prs = None
    if prs and any(pr.state == "OPEN" for pr in prs):
        number = next(pr.number for pr in prs if pr.state == "OPEN")
        out.reason = f"open PR #{number}"
        return out

    if run_probe is not None:
        try:
            if run_probe(path):
                out.reason = "run in progress"
                return out
        except Exception as exc:
            out.reason = f"run probe failed ({exc}); keeping"
            return out

    newest = max(ticket_workspace.latest_mtime(path), _gitdir_mtime(path, runner))
    age_min = (now_fn().timestamp() - newest) / 60.0
    if age_min < active_minutes:
        out.reason = f"modified {age_min:.0f} min ago (< {active_minutes:.0f} min)"
        return out

    clean, why = _is_clean(path, runner)
    if not clean:
        out.reason = why
        return out

    ancestor = _is_ancestor(head, base_ref, main_root, runner)
    if ancestor:
        out.state, out.action, out.delete_branch = MERGED, "remove", True
        out.reason = f"HEAD is an ancestor of {base_ref}"
        return out

    for pr in prs or ():
        if pr.state in ("MERGED", "CLOSED") and pr.head_sha and pr.head_sha == head:
            out.state, out.action, out.delete_branch = MERGED, "remove", True
            out.reason = f"PR #{pr.number} {pr.state} with head == local HEAD"
            return out

    out.state = UNMERGED
    if ancestor is None:
        out.reason = f"base ref {base_ref} not found; cannot prove integration (fail closed)"
    elif prs is None:
        out.reason = "not an ancestor of base and no PR information (fail closed)"
    else:
        out.reason = "not an ancestor of base and no merged/closed PR matching HEAD"
    return out


# ----------------------------------------------------------------------
# Sweep
# ----------------------------------------------------------------------


def default_roots(main_root: Path) -> list[Path]:
    return [main_root / rel for rel in ticket_workspace.WORKTREE_ROOTS]


def _remove(
    out: WorktreeEntry,
    main_root: Path,
    runner: GitRunner,
    sleep_fn: Optional[SleepFn],
    retries: int,
    backoff_s: Sequence[float],
    rmtree_fn: Optional[Callable[[Path], None]],
) -> None:
    result = ticket_workspace.cleanup(
        Path(out.path), main_root, sleep_fn=sleep_fn, retries=retries, backoff_s=backoff_s,
        run_git=runner, rmtree_fn=rmtree_fn,
    )
    out.removed, out.method, out.attempts = result.removed, result.method, result.attempts
    out.errors = list(result.errors) + ([result.detail] if result.detail and not result.removed else [])
    if not result.removed:
        logger.error("worktree_gc remove failed path=%s errors=%s", out.path, out.errors)
        return
    logger.info("worktree_gc removed path=%s method=%s attempts=%s", out.path, result.method, result.attempts)
    if out.delete_branch and out.branch and out.branch not in _PROTECTED_BRANCHES:
        res = runner(["branch", "-D", out.branch], main_root)
        if res.returncode == 0:
            out.branch_deleted = True
        else:
            out.errors.append(f"branch -D {out.branch}: {(res.stderr or res.stdout or '').strip()[:200]}")


def run_gc(
    cwd: Path,
    *,
    apply: bool = False,
    roots: Optional[Sequence[Path]] = None,
    base_ref: str = DEFAULT_BASE_REF,
    active_minutes: float = DEFAULT_ACTIVE_MINUTES,
    pr_lookup: Optional[PrLookup] = None,
    run_probe: Optional[RunProbe] = None,
    run_git: Optional[GitRunner] = None,
    sleep_fn: Optional[SleepFn] = None,
    retries: int = DEFAULT_REMOVE_RETRIES,
    backoff_s: Sequence[float] = DEFAULT_REMOVE_BACKOFF_S,
    rmtree_fn: Optional[Callable[[Path], None]] = None,
    now_fn: Optional[NowFn] = None,
) -> GcReport:
    """Classify every worktree of the repository containing ``cwd``; remove MERGED ones if ``apply``.

    ``pr_lookup=None`` disables the PR path (only the ancestor test can mark MERGED
    and open PRs are not detected -- callers wanting full safety pass :func:`gh_pr_lookup`).
    Never raises for a single worktree failing.
    """
    runner = run_git or ticket_workspace._git
    clock = now_fn or ticket_workspace.utc_now
    main_root = ticket_workspace.main_checkout_root(cwd, runner)
    root_list = [Path(r) for r in roots] if roots is not None else default_roots(main_root)
    report = GcReport(
        main_root=str(main_root), base_ref=base_ref, apply=apply, roots=[str(r) for r in root_list]
    )
    main_norm = ticket_workspace._norm(main_root)

    for entry in ticket_workspace.list_worktrees(main_root, runner):
        path = Path(entry["worktree"])
        if ticket_workspace._norm(path) == main_norm:
            continue  # the primary checkout is never a candidate (nor listed)
        if not any(_is_under(path, root) for root in root_list):
            report.entries.append(
                WorktreeEntry(
                    path=str(path), branch=_branch_of(entry), head=entry.get("HEAD", ""),
                    state=OUT_OF_SCOPE, reason="outside the configured roots; never touched",
                )
            )
            continue
        report.entries.append(
            classify_worktree(
                entry, main_root, base_ref=base_ref, active_minutes=active_minutes, now_fn=clock,
                runner=runner, pr_lookup=pr_lookup, run_probe=run_probe,
            )
        )

    if apply:
        for out in report.entries:
            if out.action != "remove":
                continue
            try:
                _remove(out, main_root, runner, sleep_fn or time.sleep, retries, backoff_s, rmtree_fn)
            except Exception as exc:  # one bad worktree must not stop the sweep
                out.errors.append(f"unexpected: {exc}")
                logger.exception("worktree_gc unexpected error path=%s", out.path)
        if any(e.removed for e in report.entries):
            runner(["worktree", "prune"], main_root)
            report.pruned = True
    return report


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


def _format_text(report: GcReport) -> str:
    mode = "APPLY" if report.apply else "DRY-RUN"
    lines = [f"worktree_gc [{mode}] main={report.main_root} base={report.base_ref}"]
    for e in report.entries:
        flag = ""
        if report.apply and e.action == "remove":
            flag = " -> removed" if e.removed else " -> FAILED"
            flag += " (branch deleted)" if e.branch_deleted else ""
        elif e.action == "remove":
            flag = " -> would remove"
        lines.append(f"  [{e.state}] {e.path} ({e.branch or 'detached'}): {e.reason}{flag}")
    lines.append("summary: " + ", ".join(f"{k}={v}" for k, v in sorted(report.counts().items())))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m core.git.worktree_gc",
        description="Remove merged, clean, unlocked agent worktrees (dry-run by default).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="only report (default)")
    mode.add_argument("--apply", action="store_true", help="actually remove MERGED worktrees")
    parser.add_argument("--json", action="store_true", help="structured JSON output")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="any path inside the repository")
    parser.add_argument("--root", type=Path, action="append", help="worktree root (repeatable)")
    parser.add_argument("--base-ref", default=DEFAULT_BASE_REF)
    parser.add_argument("--active-minutes", type=float, default=DEFAULT_ACTIVE_MINUTES)
    parser.add_argument("--no-gh", action="store_true", help="do not query GitHub for PR state")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        report = run_gc(
            args.repo,
            apply=bool(args.apply),
            roots=args.root,
            base_ref=args.base_ref,
            active_minutes=args.active_minutes,
            pr_lookup=None if args.no_gh else gh_pr_lookup,
        )
    except ticket_workspace.WorkspaceError as exc:
        print(f"worktree_gc: {exc}", file=sys.stderr)
        return 2
    if args.no_gh:
        report.notes.append("gh disabled: squash-merged branches are not recognised and open PRs are not checked")
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if args.json else _format_text(report))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
