#!/usr/bin/env python3
"""Validate command the production line runs for the `darkfac` project itself.

The official gate (`core/harness/runner.py --quick`) refuses to run on a dirty
worktree ("Candidate worktree is dirty"), because its verdict is bound to a
commit. The line's development stage validates BEFORE it commits the ticket, so
calling the runner directly there would fail every iteration. This wrapper picks
the right mode:

- clean tree (e.g. the ValidationStage's fresh clone of the pushed branch): run
  the official runner, which keeps its verdict cache and remote test-worker
  dispatch (DARKFAC_TEST_WORKERS);
- dirty tree (development loop): build a SNAPSHOT commit of the working tree
  (temporary index + `write-tree` + `commit-tree -p HEAD`; the real index, HEAD
  and branch are never touched), check it out in a detached temporary git
  worktree and run the official runner THERE. The candidate is then a clean
  commit, so the run is identical to the official gate: same verdict cache, same
  dispatch to the remote test worker (DARKFAC_TEST_WORKERS) and the same Desktop
  environment instead of the production container's. The temporary worktree is
  always removed;
- dirty tree and the snapshot cannot be built (no git, broken repository, no
  runner in the snapshot): fall back, loudly, to running the same `quick` steps
  of `harness.config.json` directly in the dirty tree, each with its own
  timeout, stopping at the first failure.

Exit code 0 only when everything passed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.harness import runner  # noqa: E402

# The cloud worker that runs this script carries production credentials
# (Postgres URL, Dokploy key, Telegram bot, GitHub/agent tokens). The test
# suite must never see them: tests would read live config (and fail, or worse,
# touch production). Only the remote test-worker dispatch settings survive.
_SENSITIVE_ENV = re.compile(
    r"(DATABASE_URL|_TOKEN$|_API_KEY$|_SECRET$|PASSWORD|ENCRYPTION_KEY|GITHUB_PAT"
    r"|^DOKPLOY_|^TELEGRAM_|^R2_|^DARKFAC_CANARY_|^DARKFAC_DOGFOOD_)"
)
# Configuration of the cloud worker container that test modules read from the
# ambient environment (slot count, Codex sandbox mode, workspace/data dirs,
# cheap-model routing, remote harness URLs). The official gate runs on a
# workstation where none of these are set, so the suite must not see them.
_AMBIENT_CONFIG_ENV = frozenset(
    {
        "DARKFAC_CODEX_SANDBOX_MODE",
        "DARKFAC_MAX_CONCURRENT_SLOTS",
        "DARKFAC_WORKSPACES",
        "DARKFAC_OPENROUTER_CHEAP_MODEL",
        "DARKFAC_OPERATING_HARNESS",
        "DARKFAC_ONPREM_BACKUP_DIR",
        "DATA_DIR",
        "FACTORY_DIR",
        "OLLAMA_BASE_URL",
        "REMOTE_HARNESS_URL",
        "REMOTE_HARNESS_URLS",
    }
)
# Routing knobs of the cloud worker (`DARKFAC_ROUTING_UNKNOWN_QUOTA=last_resort`, ...): they change what
# `core.line.routing.pick` returns, so a test asserting the workstation behaviour fails when they leak.
_AMBIENT_CONFIG_PREFIXES = ("DARKFAC_ROUTING_",)
# xdist workers a LOCAL run may start when the host is not Windows: `-n auto` in the 1.5-CPU production
# container exhausts its thread/pid limits ("RuntimeError: can't start new thread"). Remote dispatch is
# unaffected (the Desktop test worker uses its own environment).
LOCAL_XDIST_WORKERS = "2"
XDIST_WORKERS_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"
_KEEP_ENV = frozenset({"DARKFAC_WORKER_TOKEN", "DARKFAC_TEST_WORKERS", "DARKFAC_REMOTE_BUSY_WAIT_SEC"})


def sanitized_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Copy of `base` (default os.environ) without production credentials/config."""
    source = dict(os.environ if base is None else base)
    return {
        k: v
        for k, v in source.items()
        if k in _KEEP_ENV
        or (k not in _AMBIENT_CONFIG_ENV and not k.startswith(_AMBIENT_CONFIG_PREFIXES) and not _SENSITIVE_ENV.search(k))
    }


def runner_env(base: dict[str, str] | None = None, *, platform: str | None = None) -> dict[str, str]:
    """`sanitized_env` plus the xdist worker cap for local runs on non-Windows hosts (see above)."""
    env = sanitized_env(base)
    source = os.environ if base is None else base
    if (platform or sys.platform) != "win32" and not source.get(XDIST_WORKERS_ENV, "").strip():
        env[XDIST_WORKERS_ENV] = LOCAL_XDIST_WORKERS
    return env


def is_clean_tree(root: Path) -> bool:
    """True only when `git status` succeeds and reports nothing (fail-closed: unknown -> dirty)."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0 and not proc.stdout.strip()


class SnapshotError(RuntimeError):
    """The working tree could not be turned into a checked-out snapshot commit."""


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Run git in `root`; return stripped stdout or raise SnapshotError."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise SnapshotError(f"git {args[0]} could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or proc.stdout.strip() or "unknown git error")[:500]
        raise SnapshotError(f"git {' '.join(args[:2])} failed ({proc.returncode}): {detail}")
    return proc.stdout.strip()


def build_snapshot_commit(root: Path) -> str:
    """Commit the working tree (tracked changes + untracked, non-ignored files); return the SHA.

    Uses a throwaway copy of the index (`GIT_INDEX_FILE`), so the real index,
    HEAD, branch and reflogs are untouched; the new commit is parented on HEAD
    and stays unreferenced (garbage) once the temporary checkout is gone.
    """
    head = _git(root, "rev-parse", "--verify", "HEAD")
    real_index = Path(_git(root, "rev-parse", "--git-path", "index"))
    if not real_index.is_absolute():
        real_index = root / real_index
    scratch = Path(tempfile.mkdtemp(prefix="line-validate-index-"))
    try:
        tmp_index = scratch / "index"
        if real_index.is_file():
            shutil.copyfile(real_index, tmp_index)
        env = dict(os.environ)
        env.update(
            GIT_INDEX_FILE=str(tmp_index),
            GIT_AUTHOR_NAME="line-validate",
            GIT_AUTHOR_EMAIL="line-validate@darkfac.invalid",
            GIT_COMMITTER_NAME="line-validate",
            GIT_COMMITTER_EMAIL="line-validate@darkfac.invalid",
        )
        _git(root, "add", "-A", env=env)
        tree = _git(root, "write-tree", env=env)
        return _git(root, "commit-tree", tree, "-p", head, "-m", "line_validate snapshot of the working tree", env=env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def report_dirty_checkout(checkout: Path, *, limit: int = 40) -> None:
    """After a failed runner, list what the run left dirty in the checkout (best effort).

    The runner refuses a candidate whose worktree changed while the tests ran ("Candidate worktree is
    dirty") and a green pytest summary then ends in exit 1; naming the paths makes that obvious in the
    log tail the line records, instead of an unexplained "FAILED (N passed)".
    """
    try:
        status = _git(checkout, "status", "--porcelain=v1", "--untracked-files=all")
    except SnapshotError as exc:
        print(f"[line_validate] could not inspect the checkout after the failure: {exc}", flush=True)
        return
    lines = status.splitlines()
    if not lines:
        print("[line_validate] runner failed; the checkout is clean (the failure is not a dirty tree)", flush=True)
        return
    print(f"[line_validate] runner failed and left {len(lines)} dirty path(s) in the checkout:", flush=True)
    for line in lines[:limit]:
        print(f"[line_validate]   {line}", flush=True)
    if len(lines) > limit:
        print(f"[line_validate]   ... {len(lines) - limit} more", flush=True)


def run_official_runner_on_snapshot(root: Path) -> int:
    """Run `runner.py --quick` on a detached temporary checkout of a snapshot commit.

    Raises SnapshotError ONLY when the snapshot could not be prepared (nothing
    ran yet); once the runner started, its exit code is returned as is.
    """
    sha = build_snapshot_commit(root)
    scratch = Path(tempfile.mkdtemp(prefix="line-validate-wt-"))
    checkout = scratch / "tree"
    added = False
    try:
        _git(root, "worktree", "add", "--detach", str(checkout), sha)
        added = True
        runner_path = checkout / "core" / "harness" / "runner.py"
        if not runner_path.is_file():
            raise SnapshotError(f"snapshot {sha[:12]} has no core/harness/runner.py")
        print(
            f"[line_validate] snapshot {sha[:12]} checked out at {checkout}; running the official harness (--quick)",
            flush=True,
        )
        code = subprocess.call([sys.executable, str(runner_path), "--quick"], cwd=checkout, env=runner_env())
        if code != 0:
            report_dirty_checkout(checkout)
        return code
    finally:
        if added:
            try:
                _git(root, "worktree", "remove", "--force", str(checkout))
            except SnapshotError as exc:
                print(f"[line_validate] warning: could not remove the temporary checkout: {exc}", flush=True)
        shutil.rmtree(scratch, ignore_errors=True)
        try:
            _git(root, "worktree", "prune")
        except SnapshotError:
            pass


def run_steps_directly(root: Path, config_path: Path) -> int:
    config, _config_hash = runner.load_config(config_path)
    steps = runner._selected_steps(config, quick=True, include_holdout=False)
    if not steps:
        print("[line_validate] no quick steps selected; refusing to pass on an empty selection")
        return 1
    env = dict(runner_env(), PYTHONPATH=str(root))
    for step in steps:
        print(f"[line_validate] step {step.name}: {step.cmd}", flush=True)
        try:
            proc = subprocess.run(
                runner.resolve_command(step.cmd), cwd=root, env=env, timeout=step.timeout_sec, check=False
            )
        except subprocess.TimeoutExpired:
            print(f"[line_validate] step {step.name} timed out after {step.timeout_sec}s")
            return 124
        except OSError as exc:
            print(f"[line_validate] step {step.name} failed to launch: {exc}")
            return 127
        if proc.returncode != 0:
            print(f"[line_validate] step {step.name} failed with exit code {proc.returncode}")
            return proc.returncode
    print("[line_validate] all quick steps passed")
    return 0


def main(root: Path = REPO_ROOT) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    config_path = root / "harness.config.json"
    if is_clean_tree(root):
        print("[line_validate] clean tree: running the official harness (--quick)", flush=True)
        return subprocess.call(
            [sys.executable, str(root / "core" / "harness" / "runner.py"), "--quick"],
            cwd=root,
            env=runner_env(),
        )
    print("[line_validate] dirty tree: validating a snapshot commit of the working tree", flush=True)
    try:
        return run_official_runner_on_snapshot(root)
    except SnapshotError as exc:
        print(
            f"[line_validate] WARNING: snapshot unavailable ({exc}); "
            "falling back to the harness quick steps run directly in the dirty tree "
            "(no remote dispatch, no verdict cache)",
            flush=True,
        )
    return run_steps_directly(root, config_path)


if __name__ == "__main__":
    sys.exit(main())
