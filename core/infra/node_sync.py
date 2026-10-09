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
    queue_length: int | None = None
    is_legacy: bool | None = None
    sync_classification: Literal["converged", "behind", "ahead", "diverged", "dirty"] | None = None
    local_commits: list[str] = Field(default_factory=list)
    dirty_files: dict[str, list[str]] = Field(default_factory=dict)
    backup_branch: str | None = None



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
    queue_length = int(data.get("queue_length")) if "queue_length" in data and data.get("queue_length") is not None else None
    commit_date = str(data.get("commit_date")) if data.get("commit_date") else None
    if observed is None:
        if unknown_without_sha:
            return NodeStatus(
                name=name, state="unknown", last_contact=now,
                reason="health reports no git_sha (DARKFAC_GIT_SHA not provided at deploy); cannot verify",
                busy=busy, active_runs=active_runs, queue_length=queue_length, is_legacy=False, commit_date=commit_date,
            )
        is_legacy = not restart_safe
        if is_legacy:
            return NodeStatus(
                name=name, state="divergent", last_contact=now,
                reason="legacy test worker on :8080 (no git_sha / restart_safe)",
                busy=busy, active_runs=active_runs, queue_length=queue_length, is_legacy=True, commit_date=commit_date,
            )
        return NodeStatus(
            name=name, state="divergent", last_contact=now,
            reason="health response has no full git_sha",
            busy=busy, active_runs=active_runs, queue_length=queue_length, is_legacy=False, commit_date=commit_date,
        )
    return NodeStatus(
        name=name, state="converged" if observed == expected else "divergent",
        git_sha=observed, last_contact=now,
        busy=busy, active_runs=active_runs, queue_length=queue_length, is_legacy=False, commit_date=commit_date,
    )


KNOWN_EPHEMERAL_PATTERNS = (
    "__pycache__",
    ".pytest_cache",
    "test_logs",
    "reports/canary",
    "ticket_quota_usage.jsonl",
)


def is_ephemeral_dirty_file(path_str: str) -> bool:
    norm = path_str.replace("\\", "/")
    return any(pattern in norm for pattern in KNOWN_EPHEMERAL_PATTERNS)


def classify_checkout(
    root: Path,
    git: GitRunner,
    expected_sha: str | None,
) -> tuple[
    Literal["converged", "behind", "ahead", "diverged", "dirty"] | None,
    list[str],
    dict[str, list[str]],
    str | None,
]:
    """Classify the git state of a checkout relative to origin/main (USR-78, USR-119)."""
    dirty_files: dict[str, list[str]] = {"tracked_modified": [], "untracked": [], "ignored": []}
    status_porcelain = ""
    try:
        status_porcelain = git(root, ["status", "--porcelain"])
    except Exception:
        status_porcelain = ""

    if status_porcelain:
        try:
            diff_names = [p.strip() for p in git(root, ["diff", "--name-only"]).splitlines() if p.strip()]
            dirty_files["tracked_modified"] = sorted(set(diff_names))
        except Exception:
            lines = [l.strip() for l in status_porcelain.splitlines() if l.strip()]
            dirty_files["tracked_modified"] = [l[2:].strip() for l in lines if not l.startswith("??")]
        try:
            untracked = [p.strip() for p in git(root, ["ls-files", "--others", "--exclude-standard"]).splitlines() if p.strip()]
            dirty_files["untracked"] = sorted(set(untracked))
        except Exception:
            lines = [l.strip() for l in status_porcelain.splitlines() if l.strip()]
            dirty_files["untracked"] = [l[2:].strip() for l in lines if l.startswith("??")]
        try:
            ignored = [p.strip() for p in git(root, ["ls-files", "--others", "-i", "--exclude-standard"]).splitlines() if p.strip()]
            dirty_files["ignored"] = sorted(set(ignored))
        except Exception:
            pass

    has_tracked_or_untracked = bool(dirty_files["tracked_modified"] or dirty_files["untracked"])

    local_commits: list[str] = []
    ahead_count = 0
    behind_count = 0
    if expected_sha:
        try:
            rev_list_ahead = git(root, ["rev-list", "origin/main..HEAD"]).strip()
            if rev_list_ahead:
                local_commits = [c.strip() for c in rev_list_ahead.splitlines() if c.strip()]
                ahead_count = len(local_commits)
        except Exception:
            pass
        try:
            rev_list_behind = git(root, ["rev-list", "HEAD..origin/main"]).strip()
            if rev_list_behind:
                behind_count = len([c.strip() for c in rev_list_behind.splitlines() if c.strip()])
        except Exception:
            pass

    if has_tracked_or_untracked:
        tracked_summary = f"{len(dirty_files['tracked_modified'])} tracked modified ({', '.join(dirty_files['tracked_modified'][:3])})" if dirty_files["tracked_modified"] else "0 tracked"
        untracked_summary = f"{len(dirty_files['untracked'])} untracked ({', '.join(dirty_files['untracked'][:3])})" if dirty_files["untracked"] else "0 untracked"
        reason = f"dirty worktree: {tracked_summary}, {untracked_summary}; no git operation attempted"
        return "dirty", local_commits, dirty_files, reason
    elif ahead_count > 0 and behind_count > 0:
        reason = f"diverged: ahead {ahead_count} commits ({', '.join(local_commits[:3])}), behind {behind_count} commits"
        return "diverged", local_commits, dirty_files, reason
    elif ahead_count > 0:
        reason = f"ahead: {ahead_count} local unpushed commits ({', '.join(local_commits[:3])})"
        return "ahead", local_commits, dirty_files, reason
    elif behind_count > 0:
        reason = f"behind: {behind_count} commits behind origin/main"
        return "behind", local_commits, dirty_files, reason
    elif expected_sha:
        return "converged", [], dirty_files, None
    return None, [], dirty_files, None


from core.paths import state_root

def _divergence_state_file() -> Path:
    return state_root() / "node_sync_divergence.json"

def _pending_convergence_file() -> Path:
    return state_root() / "node_sync_pending.json"


def record_divergence_cycle(
    node_name: str,
    state: str,
    state_path: Path | None = None,
    notifier: Callable[[str], bool] | None = None,
) -> int:
    """Track consecutive divergent cycles per node and notify on > 1 cycle (USR-78)."""
    cycles = 0
    path = state_path or _divergence_state_file()
    try:
        data: dict[str, int] = {}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if state == "divergent":
            cycles = data.get(node_name, 0) + 1
            data[node_name] = cycles
            if cycles > 1 and notifier:
                notifier(f"ALERTA: Nó {node_name} permanece divergente por {cycles} ciclos de deploy consecutivos!")
        else:
            data[node_name] = 0
            cycles = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass
    return cycles


def record_pending_convergence(
    node: str,
    expected_sha: str,
    reason: str,
    path: Path | None = None,
) -> None:
    """Persist pending convergence request for resumable lifecycle (USR-127)."""
    target = path or _pending_convergence_file()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "node": node,
            "expected_sha": expected_sha,
            "reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def load_pending_convergence(path: Path | None = None) -> dict[str, Any] | None:
    """Load pending convergence request if one was stored (USR-127)."""
    target = path or _pending_convergence_file()
    try:
        if target.is_file():
            return json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def clear_pending_convergence(path: Path | None = None) -> None:
    """Clear pending convergence record after successful convergence (USR-127)."""
    target = path or _pending_convergence_file()
    try:
        if target.is_file():
            target.unlink()
    except Exception:
        pass


def find_canonical_main_checkout(root: Path = REPO_ROOT, git: GitRunner = run_git) -> Path:
    """Discover a safe canonical main checkout without touching active worktrees (USR-127)."""
    try:
        common_dir = git(root, ["rev-parse", "--git-common-dir"])
        common_path = Path(common_dir)
        if not common_path.is_absolute():
            common_path = (root / common_path).resolve()
        primary_root = common_path.parent if common_path.name == ".git" else root
    except Exception:
        primary_root = root

    try:
        wt_out = git(primary_root, ["worktree", "list", "--porcelain"])
        current_wt: Path | None = None
        for line in wt_out.splitlines():
            line = line.strip()
            if line.startswith("worktree "):
                current_wt = Path(line.split(" ", 1)[1])
            elif line.startswith("branch refs/heads/main") and current_wt:
                return current_wt
    except Exception:
        pass

    return primary_root


def remediate_dirty_checkout(
    root: Path,
    git: GitRunner,
    dirty_files: dict[str, list[str]],
    store: Any = None,
    notifier: Callable[[str], bool] | None = None,
) -> bool:
    """Safely remediate dirty worktrees if they only contain known ephemeral files (USR-119)."""
    non_ephemeral = []
    for category in ("tracked_modified", "untracked"):
        for f in dirty_files.get(category, []):
            if not is_ephemeral_dirty_file(f):
                non_ephemeral.append(f)
    if non_ephemeral:
        msg = f"Dirty worktree contains non-ephemeral files: {', '.join(non_ephemeral)}"
        if notifier:
            notifier(msg)
        if store is not None:
            try:
                from core.demands.models import DemandOrigin, TAG_USER_DEMAND, UserTicket
                from core.roadmap.models import DeliveryStatus, LifecycleStage, PlanningHorizon, RoadmapItemType
                ticket_id = store.next_ticket_id("darkfac")
                t = UserTicket(
                    id=ticket_id,
                    project_id="darkfac",
                    title=f"Investigar arquivos modificados nao-efemeros no nó ({len(non_ephemeral)} arquivos)",
                    origin=DemandOrigin.AGENT,
                    status=DeliveryStatus.PLANNED,
                    item_type=RoadmapItemType.INFRASTRUCTURE,
                    lifecycle_stage=LifecycleStage.EXECUTION,
                    horizon=PlanningHorizon.NOW,
                    tags=[TAG_USER_DEMAND, "dirty-worktree", "auto-generated"],
                    problem_statement="O nó continha alterações não-efêmeras que impediram a sincronização limpa:\n" + "\n".join(f"- {f}" for f in non_ephemeral),
                    core_journey=["Investigar se alterações devem ser commitadas em branch ou descartadas"],
                    acceptance_criteria=["Arquivos tratados e worktree limpa para convergência"],
                )
                store.save_ticket(t)
            except Exception:
                pass
        return False
    for category in ("tracked_modified", "untracked"):
        for f in dirty_files.get(category, []):
            p = root / f
            if p.exists() and p.is_file():
                try:
                    p.unlink()
                except Exception:
                    pass
    return True


def safe_repair_divergent_checkout(
    root: Path,
    node_name: str,
    git: GitRunner,
    expected_sha: str,
    notifier: Callable[[str], bool] | None = None,
    store: Any = None,
) -> str | None:
    """Safely back up unpushed local commits to a remote branch before hard reset (USR-78)."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_branch = f"backup/{node_name.lower()}-{timestamp}"
    try:
        git(root, ["branch", backup_branch, "HEAD"])
        git(root, ["push", "origin", backup_branch])
    except Exception:
        return None
    try:
        git(root, ["reset", "--hard", "origin/main"])
    except Exception:
        return None
    if store is not None:
        try:
            from core.demands.models import DemandOrigin, TAG_USER_DEMAND, UserTicket
            from core.roadmap.models import DeliveryStatus, LifecycleStage, PlanningHorizon, RoadmapItemType
            ticket_id = store.next_ticket_id("darkfac")
            t = UserTicket(
                id=ticket_id,
                project_id="darkfac",
                title=f"Cherry-pick commits divergentes de {node_name} salvos em {backup_branch}",
                origin=DemandOrigin.AGENT,
                status=DeliveryStatus.PLANNED,
                item_type=RoadmapItemType.INFRASTRUCTURE,
                lifecycle_stage=LifecycleStage.EXECUTION,
                horizon=PlanningHorizon.NOW,
                tags=[TAG_USER_DEMAND, "divergence", "cherry-pick", "auto-generated"],
                problem_statement=f"O nó {node_name} continha commits locais antes da sincronização. Foram salvos na branch remota {backup_branch}.",
                core_journey=[f"Revisar commits na branch {backup_branch} e aplicar cherry-pick via PR se necessário"],
                acceptance_criteria=[f"Commits avaliados na branch {backup_branch}"],
            )
            store.save_ticket(t)
        except Exception:
            pass
    if notifier:
        notifier(f"Node {node_name} was divergent. Backed up to {backup_branch} and reset to origin/main.")
    return backup_branch


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
    notifier: Callable[[str], bool] | None = None,
    track_divergence: bool = False,
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
        if blocker:
            classification, local_commits, dirty_files, classification_reason = None, [], {}, blocker
            reason = blocker
            state = "divergent"
        else:
            classification, local_commits, dirty_files, classification_reason = classify_checkout(root, git, expected)
            reason = classification_reason
            state = "converged" if local == expected and classification == "converged" else "divergent"
        try:
            notebook_commit_date = git(root, ["show", "-s", "--format=%cI", local]) if local else None
        except Exception:
            notebook_commit_date = None
        notebook = NodeStatus(
            name="Notebook", state=state, git_sha=local, reason=reason, commit_date=notebook_commit_date,
            sync_classification=classification, local_commits=local_commits, dirty_files=dirty_files,
        )
    except Exception:
        notebook = NodeStatus(name="Notebook", state="unknown", reason="checkout unavailable")
    desktop = _probe(
        "Desktop", f"{desktop_url.rstrip('/')}/health", expected, http,
        retries=retries, retry_delay=retry_delay, sleep=sleep,
    )
    vps = _probe(
        "VPS", vps_url, expected, http, unknown_without_sha=True,
        retries=retries, retry_delay=retry_delay, sleep=sleep,
    )
    if track_divergence:
        for node in (notebook, desktop, vps):
            record_divergence_cycle(node.name, node.state, notifier=notifier)
    return SyncReport(expected_sha=expected, nodes=[notebook, desktop, vps])


def sync(
    *, root: Path = MAIN_CHECKOUT, git: GitRunner = run_git,
    http: HttpClient = request_json, desktop_url: str = DESKTOP_URL,
    vps_url: str = VPS_URL, token: str | None = None,
    timeout: float = 120, interval: float = 2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    vps_redeploy: VpsRedeploy = redeploy_vps,
    task_recovery: TaskRecovery | None = None,
    retries: int = 3, retry_delay: float = 0.5,
    notifier: Callable[[str], bool] | None = None,
    track_divergence: bool = False,
) -> SyncReport:
    if task_recovery is None:
        task_recovery = recover_scheduled_task
    initial = verify(
        root=root, git=git, http=http, desktop_url=desktop_url, vps_url=vps_url,
        retries=retries, retry_delay=retry_delay, sleep=sleep, notifier=notifier,
        track_divergence=track_divergence,
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
            elif notebook.sync_classification == "dirty":
                remediated = remediate_dirty_checkout(root, git, notebook.dirty_files, notifier=notifier)
                if remediated:
                    git(root, ["fetch", "origin", "main"])
                    expected = _sha(git(root, ["rev-parse", "origin/main"]))
                    if expected is None:
                        raise RuntimeError("origin/main SHA invalid")
                    git(root, ["merge", "--ff-only", "origin/main"])
                    notebook.state = "converged"
                    notebook.sync_classification = "converged"
                    notebook.reason = None
                else:
                    notebook.reason = notebook.reason or "dirty worktree; no git operation attempted"
            elif notebook.sync_classification in ("ahead", "diverged"):
                backup_branch = safe_repair_divergent_checkout(root, "Notebook", git, expected, notifier=notifier)
                if backup_branch:
                    notebook.backup_branch = backup_branch
                    notebook.state = "converged"
                    notebook.sync_classification = "converged"
                    notebook.git_sha = expected
                    notebook.reason = f"converged after backing up local commits to remote branch {backup_branch}"
                else:
                    notebook.reason = "divergent: failed to push backup branch; reset aborted for safety"
            elif git(root, ["status", "--porcelain"]):
                notebook.reason = "dirty worktree; no git operation attempted"
            else:
                git(root, ["fetch", "origin", "main"])
                expected = _sha(git(root, ["rev-parse", "origin/main"]))
                if expected is None:
                    raise RuntimeError("origin/main SHA invalid")
                git(root, ["merge", "--ff-only", "origin/main"])
                notebook.state = "converged"
                notebook.sync_classification = "converged"
                notebook.reason = None
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

            def _recover_if_offline(result: NodeStatus) -> NodeStatus:
                """USR-186: se a nova instancia nao subiu (ex.: porta 8080 ainda presa pela antiga),
                aciona a tarefa agendada (schtasks /End + /Run) antes de declarar divergencia."""
                if result.state != "offline":
                    return result
                task_recovery("DarkFac Test Worker")
                recovered = _wait_for_restart()
                if recovered.state == "offline":
                    recovered.reason = (
                        "Desktop worker offline apos restart e apos recuperacao pela tarefa agendada "
                        f"(excedeu timeout de {timeout:.0f}s)"
                    )
                return recovered

            try:
                health = http("GET", f"{desktop_url.rstrip('/')}/health", None, None)
                # USR-182: um job `queued` aparece como busy=False/active_runs=0; queue_length cobre queued+running.
                is_busy = (
                    bool(health.get("busy"))
                    or int(health.get("active_runs") or 0) > 0
                    or int(health.get("queue_length") or 0) > 0
                )
                if is_busy:
                    desktop.reason = (
                        "Desktop is busy (busy=True, active_runs > 0 or queue_length > 0); update and restart skipped"
                    )
                    record_pending_convergence("Desktop", expected, desktop.reason)
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
                    initial.nodes[1] = _recover_if_offline(_wait_for_restart())
            except Exception as exc:
                desktop.reason = str(exc)
                initial.nodes[1] = desktop
                try:
                    # O worker pode ter reiniciado sozinho (ou morrido) durante o update: se ficou desligado,
                    # a recuperacao pela tarefa agendada vem antes de declarar divergencia (USR-186).
                    probe = _probe(
                        "Desktop", f"{desktop_url.rstrip('/')}/health", expected, http,
                        retries=1, retry_delay=retry_delay, sleep=sleep,
                    )
                    if probe.state == "offline":
                        recovered = _recover_if_offline(probe)
                        if recovered.state != "converged" and not recovered.reason:
                            recovered.reason = desktop.reason
                        initial.nodes[1] = recovered
                except Exception:
                    pass
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
        retries=retries, retry_delay=retry_delay, sleep=sleep, notifier=notifier,
        track_divergence=track_divergence,
    )
    for old, new in zip(initial.nodes, final.nodes):
        if new.state != "converged":
            if old.reason and (not new.reason or "busy" in old.reason or "offline" in old.reason):
                new.reason = old.reason
    if final.nodes[1].state == "converged":
        pending = load_pending_convergence()
        if pending and pending.get("node") == "Desktop":
            clear_pending_convergence()
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "sync", "retry"))
    args = parser.parse_args(argv)
    kwargs = {
        "desktop_url": os.environ.get("DARKFAC_DESKTOP_URL", DESKTOP_URL),
        "vps_url": os.environ.get("DARKFAC_VPS_HEALTH_URL", VPS_URL),
    }
    if args.action == "retry":
        pending = load_pending_convergence()
        if not pending:
            print(json.dumps({"message": "No pending convergence found", "ok": True}))
            return 0
        report = sync(**kwargs, token=os.environ.get("DARKFAC_WORKER_TOKEN"))
    elif args.action == "verify":
        report = verify(**kwargs)
    else:
        report = sync(**kwargs, token=os.environ.get("DARKFAC_WORKER_TOKEN"))
    print(report.model_dump_json())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
