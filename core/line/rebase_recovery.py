"""Limited technical recovery for a rebase left in progress (USR-475).

Ledger paths stay with the caller. A technical file with a common base is repaired
without a model when every conflict hunk is an insertion or one side already
contains the other. Protected paths are never rewritten here. The fingerprint of
HEAD, the candidate and the conflict content is what the admission store uses to
allow at most one inference.
"""
from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from core.line.admission_store import AdmissionFingerprint, AdmissionStore, make_fingerprint

logger = logging.getLogger("darkfac.rebase_recovery")

# Factory-owned ledgers: origin/main wins. Kept here so the repair cannot union them.
LEDGER_CONFLICT_FILES = frozenset({
    ".factory/demands/demands.json",
    ".factory/owner_actions/owner_actions.json",
})

_HUNK_RE = re.compile(
    r"<<<<<<< [^\n]*\n"
    r"(?P<ours>.*?)"
    r"\|\|\|\|\|\|\| [^\n]*\n"
    r"(?P<base>.*?)"
    r"=======\n"
    r"(?P<theirs>.*?)"
    r">>>>>>> [^\n]*\n",
    re.DOTALL,
)
_CONFLICT_MARKER_RE = re.compile(r"^(?:<{7} .+|\|{7} .+|={7}|>{7} .+)$")


def recovery_store() -> AdmissionStore:
    """Admission database that records one rebase inference per fingerprint."""
    from core.paths import state_root

    return AdmissionStore(state_root() / "admission.sqlite3")


def repair_compatible_conflicts(cwd: Path) -> list[str]:
    """Stage technical files whose conflict hunks can keep both sides.

    Returns the paths that are still unmerged. Never edits a protected path or a
    ledger, and never raises: a file that cannot be repaired stays conflicted.
    """
    from core.git.autonomy import _run_git

    for path in _unmerged(cwd):
        if path in LEDGER_CONFLICT_FILES or _protected(path):
            continue
        try:
            merged = _compatible_text(cwd, path)
        except OSError as exc:
            logger.warning("rebase repair skipped %s: %s", path, exc)
            continue
        if merged is None:
            continue
        target = cwd / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(merged, encoding="utf-8", newline="\n")
        if "<<<<<<<" in merged:
            continue
        _run_git(["add", "--", path], cwd=cwd)
    return _unmerged(cwd)


def conflict_marker_baseline(cwd: Path, paths: list[str]) -> dict[str, frozenset[str]]:
    """Record marker-like lines already present in either side of a conflict.

    Documentation can legitimately discuss Git markers. Preserve those lines while
    still detecting markers introduced by an incomplete harness resolution.
    """
    baseline: dict[str, frozenset[str]] = {}
    for path in paths:
        lines: set[str] = set()
        for stage in ("1", "2", "3"):
            content = _stage_text(cwd, stage, path) or ""
            lines.update(line for line in content.splitlines() if _CONFLICT_MARKER_RE.fullmatch(line))
        baseline[path] = frozenset(lines)
    return baseline


def unresolved_conflict_markers(
    cwd: Path,
    paths: list[str],
    baseline: Mapping[str, frozenset[str]],
) -> list[str]:
    """List resolved paths that still contain conflict markers absent from both sides."""
    unresolved: list[str] = []
    for path in paths:
        target = cwd / path
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            unresolved.append(path)
            continue
        prior = baseline.get(path, frozenset())
        if any(
            _CONFLICT_MARKER_RE.fullmatch(line) and line not in prior
            for line in content.splitlines()
        ):
            unresolved.append(path)
    return unresolved


def conflict_fingerprint(
    ticket: Any,
    cwd: Path,
    paths: list[str],
    *,
    evidence: Mapping[str, Any] | None = None,
) -> AdmissionFingerprint:
    """Identity of all inputs that can change a bounded rebase recovery decision.

    The persisted row contains only digests. The context digest binds the conflict to
    its worktree, ticket prompt, branch, rebase commits, file stages, and caller evidence.
    """
    head, candidate, upstream = _rebase_shas(cwd)
    files = [_file_snapshot(cwd, path) for path in sorted(paths)]
    from core.git.autonomy import _run_git

    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd).stdout.strip()
    ticket_context = {
        "title": str(getattr(ticket, "title", "") or ""),
        "problem_statement": str(getattr(ticket, "problem_statement", "") or ""),
        "acceptance_criteria": list(getattr(ticket, "acceptance_criteria", []) or []),
        "tags": list(getattr(ticket, "tags", []) or []),
    }
    return make_fingerprint(
        project_id=str(getattr(ticket, "project_id", "") or "darkfac"),
        ticket_id=str(getattr(ticket, "id", "") or ""),
        candidate_digest=candidate,
        context_digest={
            "head": head,
            "candidate": candidate,
            "upstream": upstream,
            "branch": branch,
            "worktree": str(cwd.resolve()),
            "ticket": ticket_context,
            "paths": sorted(paths),
            "files": files,
            "evidence": dict(evidence or {}),
        },
        error="rebase_conflict",
        resource_version="rebase-recovery",
        main_sha=upstream or head,
    )


def _unmerged(cwd: Path) -> list[str]:
    from core.git.autonomy import _run_git

    out = _run_git(["diff", "--name-only", "--diff-filter=U"], cwd=cwd).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _protected(path: str) -> bool:
    from core.orchestrator.guard import audit_paths

    return bool(audit_paths([path]))


def _stage_text(cwd: Path, stage: str, path: str) -> str | None:
    from core.git.autonomy import _run_git

    shown = _run_git(["cat-file", "blob", f":{stage}:{path}"], cwd=cwd)
    if shown.returncode != 0:
        return None
    return shown.stdout


def _compatible_text(cwd: Path, path: str) -> str | None:
    """Merged text when every hunk is compatible; None when the file must stay conflicted.

    A compatible file is returned without conflict markers. A mixed file is returned
    with only the incompatible hunks still marked, so the agent sees the real conflict
    and the compatible additions are already in the worktree. The caller stages the
    file only when no marker remains.
    """
    from core.git.autonomy import _run_git

    base = _stage_text(cwd, "1", path)
    ours = _stage_text(cwd, "2", path)
    theirs = _stage_text(cwd, "3", path)
    if base is None or ours is None or theirs is None:
        return None
    if "\0" in base or "\0" in ours or "\0" in theirs:
        return None
    with tempfile.TemporaryDirectory(prefix="rebase-repair-") as tmp:
        root = Path(tmp)
        base_path = root / "base"
        ours_path = root / "ours"
        theirs_path = root / "theirs"
        base_path.write_text(base, encoding="utf-8", newline="\n")
        ours_path.write_text(ours, encoding="utf-8", newline="\n")
        theirs_path.write_text(theirs, encoding="utf-8", newline="\n")
        merged = _run_git(
            ["merge-file", "-p", "--diff3", "-L", "HEAD", "-L", "base", "-L", "ticket",
             str(ours_path), str(base_path), str(theirs_path)],
            cwd=cwd,
        )
    text = merged.stdout
    if merged.returncode == 0 and "<<<<<<<" not in text:
        return text if text.endswith("\n") or not text else text + "\n"
    hunks = list(_HUNK_RE.finditer(text))
    if not hunks:
        return None
    pieces: list[str] = []
    cursor = 0
    compatible_hunks = 0
    for match in hunks:
        pieces.append(text[cursor:match.start()])
        if _hunk_compatible(match.group("base"), match.group("ours"), match.group("theirs")):
            compatible_hunks += 1
            pieces.append(_join_both(match.group("ours"), match.group("theirs")))
        else:
            pieces.append(
                "<<<<<<< HEAD\n"
                f"{match.group('ours')}=======\n"
                f"{match.group('theirs')}>>>>>>> ticket\n"
            )
        cursor = match.end()
    pieces.append(text[cursor:])
    if compatible_hunks == 0:
        return None
    return "".join(pieces)


def _hunk_compatible(base: str, ours: str, theirs: str) -> bool:
    """True when the hunk adds both sides or only one side moved."""
    left = ours.strip()
    right = theirs.strip()
    anchor = base.strip()
    if left == right:
        return True
    if not anchor:
        return True
    if left == anchor or right == anchor:
        return True
    if left and right and (_contains_lines(left, right) or _contains_lines(right, left)):
        return True
    return False


def _contains_lines(container: str, contained: str) -> bool:
    """Avoid substring matches such as treating ``a = 1`` as contained in ``a = 10``."""
    container_lines = container.splitlines()
    contained_lines = contained.splitlines()
    if not contained_lines:
        return True
    width = len(contained_lines)
    return any(container_lines[start:start + width] == contained_lines
               for start in range(len(container_lines) - width + 1))


def _join_both(ours: str, theirs: str) -> str:
    left = ours.strip("\n")
    right = theirs.strip("\n")
    if not left:
        body = right
    elif not right or left == right or _contains_lines(left, right):
        body = left
    elif _contains_lines(right, left):
        body = right
    else:
        body = f"{left}\n\n{right}"
    prefix = "\n" if ours.startswith("\n") or theirs.startswith("\n") else ""
    return f"{prefix}{body}\n"


def _rebase_shas(cwd: Path) -> tuple[str, str, str]:
    """Return ``(HEAD, candidate orig-head, upstream onto)`` during a rebase."""
    from core.git.autonomy import _run_git

    head = _run_git(["rev-parse", "HEAD"], cwd=cwd).stdout.strip()
    git_dir = Path(_run_git(["rev-parse", "--git-dir"], cwd=cwd).stdout.strip())
    git_dir = git_dir if git_dir.is_absolute() else cwd / git_dir
    rebase_dir = git_dir / "rebase-merge"
    if not rebase_dir.is_dir():
        rebase_dir = git_dir / "rebase-apply"

    def _read(name: str) -> str:
        path = rebase_dir / name
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace").strip()

    candidate = _read("orig-head") or head
    upstream = _read("onto")
    return head, candidate, upstream


def _file_snapshot(cwd: Path, path: str) -> dict[str, str]:
    worktree = ""
    file_path = cwd / path
    if file_path.is_file():
        worktree = file_path.read_text(encoding="utf-8", errors="replace")
    return {
        "path": path,
        "worktree": worktree,
        "base": _stage_text(cwd, "1", path) or "",
        "ours": _stage_text(cwd, "2", path) or "",
        "theirs": _stage_text(cwd, "3", path) or "",
    }
