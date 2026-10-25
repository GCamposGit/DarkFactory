"""Daily snapshot of the control-store queue into the git-tracked ledgers (USR-343 S4b).

The control store is the source of truth for tickets and owner actions. Once a day this script
writes ``.factory/demands/demands.json`` and ``.factory/owner_actions/owner_actions.json`` from it in
ONE commit pushed straight to ``main`` with the GitHub App token (``core.git.app_token``). It works in a
throwaway detached worktree of ``origin/main`` so the caller's checkout is never touched.

Idempotent: a marker in the state root skips the rest of the day; an unchanged ledger creates no commit.

    python C:\\dev\\DarkFac\\scripts\\snapshot_queue.py [--force]
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("darkfac.snapshot_queue")

DEMANDS_RELATIVE = ".factory/demands/demands.json"
OWNER_ACTIONS_RELATIVE = ".factory/owner_actions/owner_actions.json"
COMMIT_TITLE = "chore(ledger): snapshot diario da fila do control store [USR-343]"
MARKER_NAME = "queue_snapshot.json"


@dataclass
class SnapshotResult:
    ok: bool
    action: str  # skipped | no_changes | pushed | error
    message: str = ""
    commit: Optional[str] = None


def _git(args: list[str], cwd: Path, *, token: str | None = None) -> subprocess.CompletedProcess[str]:
    prefix: list[str] = []
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        prefix = ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]
    return subprocess.run(
        ["git", *prefix, *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120, check=False,
    )


def _redact(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


def render_ledgers() -> dict[str, str]:
    """File contents (relative path -> text) rendered from the control store plus the current files."""
    from core.demands.store import DemandsStore
    from core.owner_actions.store import SCHEMA_VERSION, OwnerActionStore

    tickets = [json.loads(t.model_dump_json()) for t in DemandsStore().list_tickets()]
    actions = [a.to_record() for a in OwnerActionStore(control=True).list_actions()]
    return {
        DEMANDS_RELATIVE: json.dumps(tickets, indent=2, ensure_ascii=False) + "\n",
        OWNER_ACTIONS_RELATIVE: json.dumps(
            {"schema_version": SCHEMA_VERSION, "actions": actions}, indent=2, ensure_ascii=False
        ) + "\n",
    }


def _marker_path() -> Path:
    from core.paths import state_root

    return state_root() / MARKER_NAME


def already_done_today(today: str) -> bool:
    try:
        return json.loads(_marker_path().read_text(encoding="utf-8")).get("date") == today
    except (OSError, ValueError):
        return False


def run_snapshot(
    repo_root: Path = REPO_ROOT,
    *,
    files: dict[str, str] | None = None,
    token: str | None = None,
    force: bool = False,
    today: str | None = None,
    use_marker: bool = True,
) -> SnapshotResult:
    """Write ``files`` (default: rendered from the store) as ONE commit on ``origin/main``. Never raises."""
    day = today or datetime.now(UTC).strftime("%Y-%m-%d")
    if use_marker and not force and already_done_today(day):
        return SnapshotResult(True, "skipped", f"snapshot de {day} ja feito")
    work = Path(tempfile.mkdtemp(prefix="darkfac-snapshot-"))
    attached = False
    try:
        if token is None:
            from core.git import app_token

            token = app_token.get_app_token()
        content = files if files is not None else render_ledgers()
        fetch = _git(["fetch", "origin", "main"], repo_root, token=token)
        if fetch.returncode != 0:
            return SnapshotResult(False, "error", _redact(f"fetch falhou: {fetch.stderr.strip()}", token))
        add = _git(["worktree", "add", "--detach", str(work / "wt"), "origin/main"], repo_root)
        if add.returncode != 0:
            return SnapshotResult(False, "error", f"worktree falhou: {add.stderr.strip()}")
        attached = True
        wt = work / "wt"
        for rel, text in content.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="\n")
        _git(["add", "--", *content], wt)
        if not _git(["status", "--porcelain"], wt).stdout.strip():
            _write_marker(day)
            return SnapshotResult(True, "no_changes", "ledgers ja iguais ao control store")
        commit = _git(
            ["-c", "user.name=DarkFac", "-c", "user.email=darkfac@users.noreply.github.com",
             "commit", "-m", COMMIT_TITLE],
            wt,
        )
        if commit.returncode != 0:
            return SnapshotResult(False, "error", f"commit falhou: {commit.stderr.strip()}")
        sha = _git(["rev-parse", "HEAD"], wt).stdout.strip()
        push = _git(["push", "origin", "HEAD:main"], wt, token=token)
        if push.returncode != 0:
            return SnapshotResult(False, "error", _redact(f"push falhou: {push.stderr.strip()}", token))
        _write_marker(day)
        return SnapshotResult(True, "pushed", "snapshot publicado em main", commit=sha)
    except Exception as exc:  # noqa: BLE001 - runs inside a long-lived loop
        return SnapshotResult(False, "error", _redact(f"{exc.__class__.__name__}: {exc}", token))
    finally:
        if attached:
            _git(["worktree", "remove", "--force", str(work / "wt")], repo_root)
        shutil.rmtree(work, ignore_errors=True)


def _write_marker(day: str) -> None:
    try:
        path = _marker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"date": day}), encoding="utf-8")
    except OSError as exc:
        logger.warning("snapshot marker not written: %s", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="ignore the once-per-day marker")
    args = parser.parse_args(argv)
    result = run_snapshot(force=args.force)
    print(f"{result.action}: {result.message}")  # noqa: T201
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
