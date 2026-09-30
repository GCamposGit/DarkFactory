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
    last_contact: datetime | None = None
    reason: str | None = None


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
        [sys.executable, str(REPO_ROOT / "scripts" / "dokploy_redeploy.py"), "--skip-backup"],
        cwd=REPO_ROOT, check=False, timeout=3600,
    )
    return result.returncode == 0


def _sha(value: Any) -> str | None:
    candidate = str(value or "").lower()
    return candidate if SHA_PATTERN.fullmatch(candidate) else None


def _probe(name: str, url: str, expected: str, http: HttpClient) -> NodeStatus:
    try:
        data = http("GET", url, None, None)
    except Exception:
        return NodeStatus(name=name, state="offline", reason="health endpoint unavailable; last contact unknown")
    observed = _sha(data.get("git_sha"))
    state = "converged" if observed == expected else "divergent"
    return NodeStatus(
        name=name, state=state, git_sha=observed,
        last_contact=datetime.now(timezone.utc),
        reason=None if observed else "health response has no full git_sha",
    )


def verify(
    *, root: Path = MAIN_CHECKOUT, git: GitRunner = run_git,
    http: HttpClient = request_json, desktop_url: str = DESKTOP_URL,
    vps_url: str = VPS_URL,
) -> SyncReport:
    try:
        expected = _sha(git(root, ["rev-parse", "origin/main"]))
    except Exception:
        expected = None
    if expected is None:
        return SyncReport(nodes=[NodeStatus(name=n, state="unknown", reason="origin/main unavailable") for n in ("Notebook", "Desktop", "VPS")])
    try:
        local = _sha(git(root, ["rev-parse", "HEAD"]))
        notebook = NodeStatus(name="Notebook", state="converged" if local == expected else "divergent", git_sha=local)
    except Exception:
        notebook = NodeStatus(name="Notebook", state="unknown", reason="checkout unavailable")
    return SyncReport(expected_sha=expected, nodes=[
        notebook,
        _probe("Desktop", f"{desktop_url.rstrip('/')}/health", expected, http),
        _probe("VPS", vps_url, expected, http),
    ])


def sync(
    *, root: Path = MAIN_CHECKOUT, git: GitRunner = run_git,
    http: HttpClient = request_json, desktop_url: str = DESKTOP_URL,
    vps_url: str = VPS_URL, token: str | None = None,
    timeout: float = 120, interval: float = 2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    vps_redeploy: VpsRedeploy = redeploy_vps,
) -> SyncReport:
    initial = verify(root=root, git=git, http=http, desktop_url=desktop_url, vps_url=vps_url)
    expected = initial.expected_sha
    if expected is None:
        return initial
    notebook = initial.nodes[0]
    if notebook.state != "converged":
        try:
            if git(root, ["status", "--porcelain"]):
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
    if desktop.state != "converged":
        try:
            response = http("POST", f"{desktop_url.rstrip('/')}/system/update", {}, token)
            if response.get("success") is not True or _sha(response.get("current_commit")) != expected:
                raise RuntimeError("Desktop update did not reach expected SHA")
            http("POST", f"{desktop_url.rstrip('/')}/system/restart", {}, token)
            deadline = clock() + timeout
            while clock() < deadline:
                probe = _probe("Desktop", f"{desktop_url.rstrip('/')}/health", expected, http)
                if probe.state == "converged":
                    break
                sleep(min(interval, max(0, deadline - clock())))
        except Exception as exc:
            desktop.reason = str(exc)
    if initial.nodes[2].state != "converged":
        try:
            if not vps_redeploy():
                initial.nodes[2].reason = "Dokploy redeploy failed"
        except Exception as exc:
            initial.nodes[2].reason = f"Dokploy redeploy failed: {type(exc).__name__}"
    final = verify(root=root, git=git, http=http, desktop_url=desktop_url, vps_url=vps_url)
    for old, new in zip(initial.nodes, final.nodes):
        if new.state != "converged" and old.reason and not new.reason:
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
