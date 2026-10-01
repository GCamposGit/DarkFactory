"""Read a file from a Git revision without invoking a shell."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Callable, Sequence


GitRunner = Callable[[Sequence[str], Path], subprocess.CompletedProcess[str]]


def safe_show(
    rev: str,
    path: str,
    *,
    cwd: Path,
    runner: GitRunner | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git show`` with a single revision/path argv item.

    An empty ``rev`` addresses the index (``:path``). The optional runner lets
    callers retain their existing timeout and authentication handling.
    """
    if not path or path.startswith(("/", "-")) or "\\" in path or ":" in path:
        raise ValueError(f"Invalid repository-relative Git path: {path!r}")
    if not rev or (":" not in rev and not rev.startswith("-")):
        args = ["show", f"{rev}:{path}"]
    else:
        raise ValueError(f"Invalid Git revision: {rev!r}")
    if runner is not None:
        return runner(args, cwd)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
