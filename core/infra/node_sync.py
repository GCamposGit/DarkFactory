"""Verify and synchronize the three DarkFac execution nodes."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field


REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_CHECKOUT = REPO_ROOT.parents[1] if REPO_ROOT.parent.name == ".worktrees" else REPO_ROOT
DESKTOP_URL = "http://100.78.181.90:8080"
VPS_URL = "https://darkhub.ggcampos.com/health"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class NodeStatus(BaseModel):
    name: str
    state: Literal["converged", "divergent", "offline", "unknown"]
    git_sha: str | None = None
    commit_date: str | None = None
    last_contact: datetime | None = None
    reason: str | None = None
    busy: bool | None = None
    active_runs: int | None = None
    is_legacy: bool | None = None


class SyncReport(BaseModel):
    expected_sha: str | None = None
    nodes: list[NodeStatus] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.expected_sha) and len(self.nodes) == 3 and all(
            node.state == "converged" for node in self.nodes
        )


GitRunner = Callable[[Path, list[str]], str]
HttpClient = Callable[[str, str, dict[str, Any] | None, str | None], dict[str, Any]]
VpsRedeploy = Callable[[], bool]
TaskRecovery = Callable[[str], bool]


def run_git(root: Path, args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout.strip()


def request_json(method: str, url: str, payload: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("expected JSON object")
    return result


def redeploy_vps() -> bool:
    """Use the existing Dokploy workflow for VPS convergence."""
    result = subprocess.run(
        [
            sys.executable, str(REPO_ROOT / "scripts" / "dokploy_redeploy.py"),
            "--skip-backup", "--skip-node-sync",  # never re-enter node verification (no loop)
        ],
        cwd=REPO_ROOT, check=False, timeout=3600,
    )
    return result.returncode == 0


def recover_scheduled_task(task_name: str = "DarkFac Test Worker") -> bool:
    """Safely terminate and restart the worker via Windows Scheduled Task (USR-71)."""
    if sys.platform != "win32":
        return False
    try:
        subprocess.run(["schtasks", "/End", "/TN", task_name], capture_output=True, check=False, timeout=15)
        res = subprocess.run(["schtasks", "/Run", "/TN", task_name], capture_output=True, check=False, timeout=15)
        return res.returncode == 0
    except Exception:
        return False


def _sha(value: Any) -> str | None:
    candidate = str(value or "").lower()
    return candidate if SHA_PATTERN.fullmatch(candidate) else None


def _probe(
    name: str,
    url: str,
    expected: str,
    http: HttpClient,
    *,
    unknown_without_sha: bool = False,
    retries: int = 3,
    retry_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> NodeStatus:
    data: dict[str, Any] | None = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            data = http("GET", url, None, None)
            break
        except Exception:
            if attempt < retries:
                sleep(retry_delay * attempt)
    if data is None:
        return NodeStatus(
            name=name,
            state="offline",
            reason=f"health endpoint unavailable (offline apos {retries} tentativas); last contact unknown",
        )
    observed = _sha(data.get("git_sha"))
    now = datetime.now(timezone.utc)
    busy = bool(data.get("busy")) if "busy" in data else None
    active_runs = int(data.get("active_runs")) if "active_runs" in data and data.get("active_runs") is not None else None
    restart_safe = bool(data.get("restart_safe")) if "restart_safe" in data else False
    commit_date = str(data.get("commit_date")) if data.get("commit_date") else None
    if observed is None:
        if unknown_without_sha:
            return NodeStatus(
                name=name, state="unknown", last_contact=now,
                reason="health reports no git_sha (DARKFAC_GIT_SHA not provided at deploy); cannot verify",
                busy=busy, active_runs=active_runs, is_legacy=False, commit_date=commit_date,
            )
        is_legacy = not restart_safe
        if is_legacy:
            return NodeStatus(
                name=name, state="divergent", last_contact=now,
                reason="legacy test worker on :8080 (no git_sha / restart_safe)",
                busy=busy, active_runs=active_runs, is_legacy=True, commit_date=commit_date,
            )
        return NodeStatus(
            name=name, state="divergent", last_contact=now,
            reason="health response has no full git_sha",
            busy=busy, active_runs=active_runs, is_legacy=False, commit_date=commit_date,
        )
    return NodeStatus(
        name=name, state="converged" if observed == expected else "divergent",
        git_sha=observed, last_contact=now,
        busy=busy, active_runs=active_runs, is_legacy=False, commit_date=commit_date,
    )


def _notebook_blocker(root: Path, git: GitRunner) -> str | None:
    """Why the Notebook checkout must never be touched, or ``None`` when it may be synced."""
    git_dir = git(root, ["rev-parse", "--git-dir"])
    common_dir = git(root, ["rev-parse", "--git-common-dir"])
    if Path(git_dir) != Path(common_dir):
        return "checkout is a git worktree, not the main checkout; no git operation attempted"
    branch = git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
    if branch != "main":
        return f"main checkout is on branch {branch!r}, not 'main'; no git operation attempted"
    return None


def verify(
    *, root: Path = MAIN_CHECKOUT, git: GitRunner = run_git,
    http: HttpClient = request_json, desktop_url: str = DESKTOP_URL,
    vps_url: str = VPS_URL,
    retries: int = 3, retry_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> SyncReport:
    try:
        expected = _sha(git(root, ["rev-parse", "origin/main"]))
    except Exception:
        expected = None
    if expected is None:
        return SyncReport(nodes=[NodeStatus(name=n, state="unknown", reason="origin/main unavailable") for n in ("Notebook", "Desktop", "VPS")])
    try:
        local = _sha(git(root, ["rev-parse", "HEAD"]))
        blocker = _notebook_blocker(root, git)
        state = "converged" if local == expected and blocker is None else "divergent"
        try:
            notebook_commit_date = git(root, ["show", "-s", "--format=%cI", local]) if local else None
        except Exception:
            notebook_commit_date = None
        notebook = NodeStatus(name="Notebook", state=state, git_sha=local, reason=blocker, commit_date=notebook_commit_date)
    except Exception:
        notebook = NodeStatus(name="Notebook", state="unknown", reason="checkout unavailable")
    return SyncReport(expected_sha=expected, nodes=[
        notebook,
        _probe(
            "Desktop", f"{desktop_url.rstrip('/')}/health", expected, http,
            retries=retries, retry_delay=retry_delay, sleep=sleep,
        ),
        _probe(
            "VPS", vps_url, expected, http, unknown_without_sha=True,
            retries=retries, retry_delay=retry_delay, sleep=sleep,
        ),
    ])


def sync(
    *, root: Path = MAIN_CHECKOUT, git: GitRunner = run_git,
    http: HttpClient = request_json, desktop_url: str = DESKTOP_URL,
    vps_url: str = VPS_URL, token: str | None = None,
    timeout: float = 120, interval: float = 2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    vps_redeploy: VpsRedeploy = redeploy_vps,
    task_recovery: TaskRecovery = recover_scheduled_task,
    retries: int = 3, retry_delay: float = 0.5,
) -> SyncReport:
    initial = verify(
        root=root, git=git, http=http, desktop_url=desktop_url, vps_url=vps_url,
        retries=retries, retry_delay=retry_delay, sleep=sleep,
    )
    expected = initial.expected_sha
    if expected is None:
        return initial
    notebook = initial.nodes[0]
    if notebook.state != "converged":
        try:
            blocker = _notebook_blocker(root, git)
            if blocker:
                notebook.reason = blocker
            elif git(root, ["status", "--porcelain"]):
                notebook.reason = "dirty worktree; no git operation attempted"
            else:
                git(root, ["fetch", "origin", "main"])
                expected = _sha(git(root, ["rev-parse", "origin/main"]))
                if expected is None:
                    raise RuntimeError("origin/main SHA invalid")
                git(root, ["merge", "--ff-only", "origin/main"])
        except Exception as exc:
            notebook.reason = str(exc)
    desktop = initial.nodes[1]
    observed_divergent_sha = desktop.git_sha if desktop.state == "divergent" else None
    if desktop.state != "converged":
        deadline = clock() + timeout
        while desktop.state != "converged" and clock() < deadline:
            sleep(min(interval, max(0.0, deadline - clock())))
            desktop = _probe(
                "Desktop", f"{desktop_url.rstrip('/')}/health", expected, http,
                retries=retries, retry_delay=retry_delay, sleep=sleep,
            )
            if desktop.state == "divergent":
                observed_divergent_sha = desktop.git_sha

    if desktop.state != "converged":
        if observed_divergent_sha is None and not desktop.is_legacy:
            # Nunca observou SHA divergente nem legado (nó permaneceu offline após retries e polling);
            # não dispara update nem restart para nó inacessível (USR-117)
            initial.nodes[1] = desktop
        else:
            def _wait_for_restart() -> NodeStatus:
                restart_deadline = clock() + timeout
                latest_desktop = desktop
                while clock() < restart_deadline:
                    probe = _probe(
                        "Desktop", f"{desktop_url.rstrip('/')}/health", expected, http,
                        retries=1, retry_delay=retry_delay, sleep=sleep,
                    )
                    if probe.state == "converged":
                        return probe
                    elif probe.state == "offline":
                        probe.reason = "restarting"
                    latest_desktop = probe
                    sleep(min(interval, max(0.0, restart_deadline - clock())))
                if latest_desktop.state == "offline":
                    latest_desktop.reason = f"Desktop worker offline apos restart (excedeu timeout de {timeout:.0f}s)"
                elif latest_desktop.state == "divergent" and latest_desktop.git_sha is None:
                    latest_desktop.reason = f"Desktop worker reiniciou mas permaneceu legado/sem git_sha apos {timeout:.0f}s"
                return latest_desktop

            try:
                health = http("GET", f"{desktop_url.rstrip('/')}/health", None, None)
                is_busy = bool(health.get("busy")) or int(health.get("active_runs") or 0) > 0
                if is_busy:
                    desktop.reason = "Desktop is busy (busy=True or active_runs > 0); update and restart skipped"
                    initial.nodes[1] = desktop
                elif desktop.is_legacy or (health.get("restart_safe") is not True and _sha(health.get("git_sha")) is None):
                    # USR-71: Legacy test worker on :8080 without git_sha or restart_safe.
                    # Recover safely via Scheduled Task "DarkFac Test Worker" (schtasks /End + /Run)
                    task_recovery("DarkFac Test Worker")
                    initial.nodes[1] = _wait_for_restart()
                else:
                    # `git_sha` in /health exists only from USR-65, which is after the USR-64 restart fix,
                    # so a worker exposing it restarts itself safely even if it predates `restart_safe`.
                    response = http("POST", f"{desktop_url.rstrip('/')}/system/update", {}, token)
                    if response.get("success") is not True or _sha(response.get("current_commit")) != expected:
                        raise RuntimeError("Desktop update did not reach expected SHA")
                    http("POST", f"{desktop_url.rstrip('/')}/system/restart", {}, token)
                    initial.nodes[1] = _wait_for_restart()
            except Exception as exc:
                desktop.reason = str(exc)
                initial.nodes[1] = desktop
    else:
        initial.nodes[1] = desktop

    if initial.nodes[2].state in ("divergent", "offline"):
        try:
            if not vps_redeploy():
                initial.nodes[2].reason = "Dokploy redeploy failed"
        except Exception as exc:
            initial.nodes[2].reason = f"Dokploy redeploy failed: {type(exc).__name__}"
    final = verify(
        root=root, git=git, http=http, desktop_url=desktop_url, vps_url=vps_url,
        retries=retries, retry_delay=retry_delay, sleep=sleep,
    )
    for old, new in zip(initial.nodes, final.nodes):
        if new.state != "converged":
            if old.reason and (not new.reason or "busy" in old.reason or "offline" in old.reason):
                new.reason = old.reason
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "sync"))
    args = parser.parse_args(argv)
    kwargs = {
        "desktop_url": os.environ.get("DARKFAC_DESKTOP_URL", DESKTOP_URL),
        "vps_url": os.environ.get("DARKFAC_VPS_HEALTH_URL", VPS_URL),
    }
    report = verify(**kwargs) if args.action == "verify" else sync(
        **kwargs, token=os.environ.get("DARKFAC_WORKER_TOKEN"),
    )
    print(report.model_dump_json())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
