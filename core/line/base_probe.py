"""Is a red test the ticket's fault, or is it already red on the base branch? (USR-153)

The `integration` stage answers this from CI (`core.git.ci_checks.classify_base_red`, USR-86). The
`development` stage has no CI to ask: its validate loop runs locally, and a test that fails identically
on `origin/<default_branch>` used to burn every `run_caps.validate_iterations_per_ticket` iteration
(3 x ~450 s on run-46efca7e11d5) while the agent tried to "fix" something that was not its change.

`probe_base_failures` re-runs ONLY the failing pytest node ids in a throwaway detached worktree of the
remote default branch and reports which of them fail there too. It is conservative on purpose: any
doubt (git or pytest could not run, the test does not exist on the base, it passes there) means "not
proven red on the base", so the caller keeps iterating the developer exactly as before.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional, Sequence

from core.line import diagnostics, workspace
from core.line.workspace import RunWorkspace
from core.projects.models import ProjectDescriptor
from core.projects.registry import normalize_repo_url

logger = logging.getLogger(__name__)

# A pytest node id the probe is willing to pass to `pytest` as an argv item: `<file>.py::<test>`, never an
# option (leading dash) and never anything but a path-like prefix. Other FAILED lines (a jest suite, a
# runner message) cannot be re-run here, so they keep the normal "the ticket broke it" reading.
_NODE_ID = re.compile(r"^[\w][\w./\\-]*\.py::\S+$")

DEFAULT_PROBE_TIMEOUT_S = 900

PytestRunner = Callable[[Sequence[str], Path, int], Optional[str]]
"""`(argv, cwd, timeout_s) -> combined output`, or `None` when pytest could not be run/finish."""


def probeable_ids(test_ids: Sequence[str]) -> list[str]:
    """The node ids of `test_ids` that `probe_base_failures` can re-run (all-or-nothing is up to the caller)."""
    return [node for node in test_ids if _NODE_ID.match(node)]


def run_pytest(argv: Sequence[str], cwd: Path, timeout_s: int) -> Optional[str]:
    """Run `argv` in `cwd` and return stdout + stderr; `None` on timeout or launch failure."""
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            **kwargs,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("base probe pytest did not finish (%s)", type(exc).__name__)
        return None
    return f"{proc.stdout or ''}\n{proc.stderr or ''}"


def probe_base_failures(
    ws: RunWorkspace,
    project: ProjectDescriptor,
    test_ids: Sequence[str],
    *,
    timeout_s: int = DEFAULT_PROBE_TIMEOUT_S,
    pytest_runner: PytestRunner = run_pytest,
) -> Optional[set[str]]:
    """The subset of `test_ids` that ALSO fail on `origin/<default_branch>`; `None` if it cannot be told.

    Never raises. The base is checked out detached in a temp directory taken from the run's own git
    object store (`ws.path` is a worktree of the project mirror), so nothing is cloned and the run
    worktree is not touched.
    """
    ids = probeable_ids(test_ids)
    if not ids or len(ids) != len(list(test_ids)):
        return None  # a failure we cannot re-run on the base is, conservatively, the ticket's
    base_ref = f"origin/{project.default_branch or 'main'}"
    repo_url = normalize_repo_url(project.repo_url) if project.repo_url else None
    tmp_root = Path(tempfile.mkdtemp(prefix="darkfac-base-probe-"))
    base_dir = tmp_root / "base"
    added = False
    try:
        # Best-effort refresh: the mirror was fetched at checkout, but a long run may be behind a fix.
        workspace._run_git(
            ["fetch", "origin", project.default_branch or "main"],
            cwd=ws.path, repo_url=repo_url, check=False, timeout=300,
        )
        workspace._run_git(["worktree", "add", "--detach", str(base_dir), base_ref], cwd=ws.path, repo_url=repo_url)
        added = True
        output = pytest_runner(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", *ids],
            base_dir,
            timeout_s,
        )
        if output is None:
            return None
        failing_on_base = set(diagnostics.failed_test_ids(output))
        return {node for node in ids if node in failing_on_base}
    except Exception as exc:  # noqa: BLE001 - the probe must never change a stage outcome
        logger.warning("base probe for run %s failed: %s", ws.run_id, type(exc).__name__)
        return None
    finally:
        if added:
            workspace._run_git(["worktree", "remove", "--force", str(base_dir)], cwd=ws.path, check=False)
        shutil.rmtree(tmp_root, ignore_errors=True)


def split_by_base(test_ids: Sequence[str], base_failures: Optional[set[str]]) -> tuple[list[str], list[str]]:
    """`(failing on the base too, failing only on the ticket)`, each in the original order."""
    base_failures = base_failures or set()
    return (
        [node for node in test_ids if node in base_failures],
        [node for node in test_ids if node not in base_failures],
    )
