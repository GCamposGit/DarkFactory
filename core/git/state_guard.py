"""Pre-commit protection for versioned factory state files."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from core.git.safe_show import GitRunner, safe_show


DEFAULT_PROTECTED_STATE_PATHS = (
    ".factory/demands/demands.json",
    ".factory/roadmap/darkfac.json",
    ".factory/projects.json",
)


def check_staged_state_files(
    cwd: Path,
    runner: GitRunner,
    protected_state_paths: Sequence[str] = DEFAULT_PROTECTED_STATE_PATHS,
) -> None:
    """Reject a commit that deletes or removes over half of a state file's lines."""
    if not protected_state_paths:
        return
    numstat = runner(
        ["--literal-pathspecs", "diff", "--cached", "--numstat", "--no-renames", "--", *protected_state_paths],
        cwd,
    )
    if numstat.returncode != 0:
        raise RuntimeError(f"Cannot inspect staged state diff: {numstat.stderr.strip()}")
    for row in numstat.stdout.splitlines():
        try:
            added, deleted, path = row.split("\t", 2)
            removed_lines = int(deleted)
        except ValueError as exc:
            raise RuntimeError(f"Cannot parse staged state diff: {row!r}") from exc
        if path not in protected_state_paths:
            continue
        previous = safe_show("HEAD", path, cwd=cwd, runner=runner)
        if previous.returncode != 0:
            if removed_lines:
                raise RuntimeError(f"Cannot read HEAD version of protected state file {path}")
            continue  # A newly added state file has no lines to remove.
        old_lines = len(previous.stdout.splitlines())
        staged = safe_show("", path, cwd=cwd, runner=runner)
        if staged.returncode != 0 and added != "0":
            raise RuntimeError(f"Cannot read staged version of protected state file {path}")
        if old_lines and (removed_lines * 2 > old_lines or not staged.stdout):
            raise RuntimeError(
                f"Refusing to commit protected state file {path}: "
                f"{removed_lines} of {old_lines} lines removed or file emptied"
            )
