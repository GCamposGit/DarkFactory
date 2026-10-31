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
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field


REPO_ROOT = Path(__file__).resolve().parents[2]
# The process always operates on the checkout containing this module. In a ticket worktree,
# reaching across to the primary checkout would cross session ownership boundaries.
MAIN_CHECKOUT = REPO_ROOT
DESKTOP_URL = "http://100.78.181.90:8080"
VPS_URL = "https://darkhub.ggcampos.com/health"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class NodeStatus(BaseModel):
    name: str
    state: Literal["converged", "divergent", "offline", "unknown", "excluded"]
    git_sha: str | None = None
    commit_date: str | None = None
    last_contact: datetime | None = None
    reason: str | None = None
    busy: bool | None = None
    active_runs: int | None = None
    queue_length: int | None = None
    is_legacy: bool | None = None
    sync_classification: Literal["converged", "behind", "ahead", "diverged", "dirty", "other_branch"] | None = None
    local_commits: list[str] = Field(default_factory=list)
    dirty_files: dict[str, list[str]] = Field(default_factory=dict)
    backup_branch: str | None = None
    target_sha: str | None = None
    # Whether this node's status is evidence that the deployed VPS cannot be
    # safely backed up. Parallel Notebook work is diagnostic only.
    blocks_backup: bool = True



class SyncReport(BaseModel):
    expected_sha: str | None = None
    nodes: list[NodeStatus] = Field(default_factory=list)

    @property
    def excluded_hosts(self) -> list[str]:
        return [node.name for node in self.nodes if node.state == "excluded"]

    @property
    def vps_excluded(self) -> bool:
        """Whether policy excluded the VPS, so this report cannot authorize a backup."""
        return any(node.name == "VPS" and node.state == "excluded" for node in self.nodes)

    @property
    def ok(self) -> bool:
        included = [node for node in self.nodes if node.state != "excluded"]
        return bool(self.expected_sha) and any(node.name == "VPS" for node in included) and all(
            node.state == "converged" for node in included
        )


def _disabled_hosts(hosts: set[str] | None = None) -> set[str]:
    """Parse the disabled-host policy once per call.

    Commas separate ids. Surrounding whitespace and case are ignored. Callers
    that already hold a snapshot pass it in, so a later environment change is
    not observed.
    """
    if hosts is None:
        raw = os.environ.get("DARKFAC_FLEET_DISABLED_HOSTS", "")
        configured: set[str] | list[str] = raw.split(",")
    else:
        configured = hosts
    return {host.strip().casefold() for host in configured if host.strip()}


def _excluded_node(name: str, target_sha: str) -> NodeStatus:
    # Only an excluded VPS withholds backup. Desktop/Notebook exclusion is not VPS drift.
    return NodeStatus(
        name=name, state="excluded", reason="disabled_by_policy", target_sha=target_sha,
        blocks_backup=name == "VPS",
    )


def _immutable_sha(value: Any) -> str | None:
    """Accept only a caller SHA that already matches ``^[0-9a-f]{40}$``.

    Case is not folded and no ref is consulted. Anything else is missing, so
    convergence cannot fall through to ``origin/main`` (USR-420).
    """
    if not isinstance(value, str) or SHA_PATTERN.fullmatch(value) is None:
        return None
    return value


def _target_sha(expected_sha: str | None) -> str | None:
    """Validate the immutable caller-provided candidate; never infer one from a moving ref."""
    return _immutable_sha(expected_sha)


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


def redeploy_vps(expected_sha: str | None = None) -> bool:
    """Redeploy the VPS through Dokploy, pinned to the caller's candidate.

    ``--skip-node-sync`` keeps the child from starting another convergence pass.
    A missing or invalid SHA is a refusal: the child would otherwise read the
    moving ``origin/main`` ref (USR-420).
    """
    candidate = _immutable_sha(expected_sha)
    if candidate is None:
        return False
    command = [
        sys.executable, str(REPO_ROOT / "scripts" / "dokploy_redeploy.py"),
        "--skip-backup", "--skip-node-sync", "--expected-sha", candidate,
    ]
    result = subprocess.run(command, cwd=REPO_ROOT, check=False, timeout=3600)
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


WORKER_OUTPUT_LIMIT = 800
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]*(?::[^\s/@]*)?@")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_KEY_VALUE_SECRET = re.compile(
    r"(?i)\b([\w.-]*(?:token|secret|password|passwd|api[_-]?key|authorization)[\w.-]*)(\s*[=:]\s*)[^\s&\"',;]+"
)
_KNOWN_TOKEN_SHAPES = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|xox[a-z]-[A-Za-z0-9-]{10,})\b"
)


def redact_worker_output(text: Any, *, token: str | None = None, limit: int = WORKER_OUTPUT_LIMIT) -> str:
    """Sanitize git/worker output for reports: redact credentials, flatten lines and truncate (USR-196)."""
    value = str(text or "")
    if token and len(token) >= 4:
        value = value.replace(token, "[REDACTED]")
    value = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)
    value = _BEARER.sub(lambda m: f"{m.group(1)} [REDACTED]", value)
    value = _KNOWN_TOKEN_SHAPES.sub("[REDACTED]", value)
    value = _KEY_VALUE_SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)
    value = " | ".join(line.strip() for line in value.splitlines() if line.strip())
    if len(value) > limit:
        value = value[:limit].rstrip() + f"... [truncado: {len(value)} caracteres]"
    return value


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


# USR-343 S4b / USR-374: these ledgers are generated snapshots (scripts/snapshot_queue.py); a local edit is
# never authoritative, so a node resets them (keeping a backup) instead of refusing to pull.
GENERATED_LEDGER_FILES = (
    ".factory/demands/demands.json",
    ".factory/owner_actions/owner_actions.json",
)


def is_generated_ledger(path_str: str) -> bool:
    return path_str.replace("\\", "/").strip().strip('"') in GENERATED_LEDGER_FILES


def reset_generated_ledgers(root: Path, git: GitRunner = run_git, backup_dir: Path | None = None) -> list[str]:
    """Back up and ``checkout`` locally modified generated ledgers so the next pull cannot abort. Never raises."""
    import shutil

    reset: list[str] = []
    for rel in GENERATED_LEDGER_FILES:
        try:
            if not git(root, ["status", "--porcelain", "--", rel]):
                continue
            target = root / rel
            if target.exists():
                if backup_dir is None:
                    from core.paths import state_root

                    backup_dir = state_root() / "ledger_backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                shutil.copy2(target, backup_dir / f"{target.name}.{stamp}.bak")
            git(root, ["checkout", "--", rel])
            reset.append(rel)
        except Exception:  # noqa: BLE001 - best effort; the pull reports whatever remains
            continue
    return reset


def stash_runtime_state(root: Path, git: GitRunner = run_git) -> dict[str, tuple[bytes, str]]:
    """USR-394: set aside local edits of runtime-state files so a pull cannot abort; never raises.

    Returns ``{path: (local_bytes, head_blob)}`` for the files that were modified and then restored to HEAD.
    Only the explicit ``GENERATED_LEDGER_FILES`` are touched (no global stash, no hard reset).
    """
    saved: dict[str, tuple[bytes, str]] = {}
    for rel in GENERATED_LEDGER_FILES:
        try:
            if not git(root, ["status", "--porcelain", "--", rel]):
                continue
            target = root / rel
            data = target.read_bytes() if target.exists() else b""
            blob = git(root, ["rev-parse", f"HEAD:{rel}"])
            git(root, ["checkout", "--", rel])
            saved[rel] = (data, blob)
        except Exception:  # noqa: BLE001 - best effort; the pull reports whatever remains
            continue
    return saved


def restore_runtime_state(
    root: Path,
    saved: dict[str, tuple[bytes, str]],
    git: GitRunner = run_git,
    conflict_dir: Path | None = None,
) -> list[str]:
    """Reapply stashed local runtime state after the update. Never raises; returns log notes.

    Unchanged upstream -> the local content is written back. Changed upstream -> upstream wins and the local
    copy goes to ``.factory/runs/node_sync_conflicts/<timestamp>-<name>``.
    """
    notes: list[str] = []
    for rel, (data, old_blob) in saved.items():
        try:
            new_blob = git(root, ["rev-parse", f"HEAD:{rel}"])
            if new_blob == old_blob:
                target = root / rel
                if not target.exists() or target.read_bytes() != data:
                    target.write_bytes(data)
                notes.append(f"[USR-394] kept local runtime state of {rel}")
                continue
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            folder = conflict_dir or (root / ".factory" / "runs" / "node_sync_conflicts")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{stamp}-{Path(rel).name}").write_bytes(data)
            notes.append(f"[USR-394] {rel} changed upstream and locally: upstream kept, local copy saved in {folder}")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"[USR-394] could not restore {rel}: {exc}")
    for note in notes:
        logging.getLogger(__name__).warning(note)
    return notes


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
    """Classify a checkout against the immutable candidate SHA (USR-78, USR-119, USR-420)."""
    candidate = _immutable_sha(expected_sha)
    dirty_files: dict[str, list[str]] = {"tracked_modified": [], "untracked": [], "ignored": []}
    if candidate is None:
        # No usable target: do not inspect a checkout another session may own.
        return None, [], dirty_files, None
    status_porcelain = ""
    status_ok = False
    try:
        status_porcelain = git(root, ["status", "--porcelain"])
        status_ok = True
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
    ahead_ok = False
    behind_ok = False
    try:
        rev_list_ahead = git(root, ["rev-list", f"{candidate}..HEAD"]).strip()
        ahead_ok = True
        if rev_list_ahead:
            local_commits = [c.strip() for c in rev_list_ahead.splitlines() if c.strip()]
            ahead_count = len(local_commits)
    except Exception:
        pass
    try:
        rev_list_behind = git(root, ["rev-list", f"HEAD..{candidate}"]).strip()
        behind_ok = True
        if rev_list_behind:
            behind_count = len([c.strip() for c in rev_list_behind.splitlines() if c.strip()])
    except Exception:
        pass

    if has_tracked_or_untracked:
        tracked_summary = f"{len(dirty_files['tracked_modified'])} tracked modified ({', '.join(dirty_files['tracked_modified'][:3])})" if dirty_files["tracked_modified"] else "0 tracked"
        untracked_summary = f"{len(dirty_files['untracked'])} untracked ({', '.join(dirty_files['untracked'][:3])})" if dirty_files["untracked"] else "0 untracked"
        reason = f"dirty worktree: {tracked_summary}, {untracked_summary}; no git operation attempted"
        return "dirty", local_commits, dirty_files, reason
    if ahead_ok and behind_ok and ahead_count > 0 and behind_count > 0:
        reason = f"diverged: ahead {ahead_count} commits ({', '.join(local_commits[:3])}), behind {behind_count} commits"
        return "diverged", local_commits, dirty_files, reason
    if ahead_ok and behind_ok and ahead_count > 0:
        reason = f"ahead: {ahead_count} local unpushed commits ({', '.join(local_commits[:3])})"
        return "ahead", local_commits, dirty_files, reason
    if ahead_ok and behind_ok and behind_count > 0:
        reason = f"behind: {behind_count} commits behind fixed candidate {candidate}"
        return "behind", local_commits, dirty_files, reason
    if ahead_ok and behind_ok:
        return "converged", [], dirty_files, None
    # Both ranges failed, so an empty count is not "HEAD is the candidate".
    # Claiming converged here skipped the fast-forward and could pass a checkout
    # that never reached the SHA. HEAD itself is still conclusive when it matches.
    if status_ok:
        try:
            if _sha(git(root, ["rev-parse", "HEAD"])) == candidate:
                return "converged", [], dirty_files, None
        except Exception:
            pass
    return None, local_commits, dirty_files, (
        "candidate commit is not available locally; no git operation attempted"
    )


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
            if cycles == 2 and notifier:
                # USR-242: alerta uma unica vez ao atingir 2 ciclos; falha de envio nunca perde o contador.
                try:
                    notifier(f"ALERTA: Nó {node_name} permanece divergente por {cycles} ciclos de deploy consecutivos!")
                except Exception:
                    pass
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
    reset_generated_ledgers(root, git)
    non_ephemeral = []
    for category in ("tracked_modified", "untracked"):
        for f in dirty_files.get(category, []):
            if not is_ephemeral_dirty_file(f) and not is_generated_ledger(f):
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
            if is_generated_ledger(f):
                continue
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
    """Back up unpushed commits before resetting to the immutable convergence candidate (USR-78)."""
    candidate = _target_sha(expected_sha)
    if candidate is None:
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_branch = f"backup/{node_name.lower()}-{timestamp}"
    try:
        git(root, ["branch", backup_branch, "HEAD"])
        git(root, ["push", "origin", backup_branch])
    except Exception:
        return None
    try:
        git(root, ["reset", "--hard", candidate])
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
        notifier(f"Node {node_name} was divergent. Backed up to {backup_branch} and reset to {candidate}.")
    return backup_branch


def _notebook_worktree_blocker(root: Path, git: GitRunner) -> str | None:
    """Non-``None`` when ``root`` is itself a linked worktree (not the shared main checkout) --
    that case must never run any git operation against it, since another ticket's own worktree
    session owns it. A shared main checkout parked on a non-``main`` branch is *not* blocked here
    (USR-222): see ``_local_main_ref_sha`` / the branch handling in ``verify``/``sync`` below, which
    judge the local ``main`` ref and do not advance it (USR-420) without touching HEAD or the working
    tree of whatever branch is currently checked out there."""
    git_dir = git(root, ["rev-parse", "--git-dir"])
    common_dir = git(root, ["rev-parse", "--git-common-dir"])
    if Path(git_dir) != Path(common_dir):
        return "checkout is a git worktree, not the main checkout; no git operation attempted"
    return None


def _local_main_ref_sha(root: Path, git: GitRunner) -> str | None:
    """SHA the local ``main`` branch ref points at, independent of what is currently checked out.

    Reading this ref does not fetch or advance it (USR-420). It never touches HEAD,
    the index or the working tree, so it is safe to use even while the shared checkout is parked on
    a different branch for an unrelated session (USR-222)."""
    try:
        return _sha(git(root, ["rev-parse", "refs/heads/main"]))
    except Exception:
        return None


def _notebook_against_candidate(root: Path, git: GitRunner, expected: str) -> NodeStatus:
    """Classify the Notebook checkout against the immutable candidate. Never raises."""
    try:
        local = _sha(git(root, ["rev-parse", "HEAD"]))
        worktree_blocker = _notebook_worktree_blocker(root, git)
        if worktree_blocker:
            return NodeStatus(name="Notebook", state="divergent", git_sha=local, reason=worktree_blocker)
        branch = git(root, ["rev-parse", "--abbrev-ref", "HEAD"])
        if branch != "main":
            # Shared checkout is mid-work on another branch (USR-222). Judge the local
            # `main` ref and do not fast-forward it (USR-420).
            local_main = _local_main_ref_sha(root, git)
            state: Literal["converged", "divergent"] = "converged" if local_main == expected else "divergent"
            reason = None
            if state != "converged":
                reason = (
                    f"main checkout is on branch {branch!r} (parallel session); local main ref "
                    + ("unknown" if local_main is None else f"behind fixed candidate {expected}")
                )
            try:
                notebook_commit_date = git(root, ["show", "-s", "--format=%cI", local_main]) if local_main else None
            except Exception:
                notebook_commit_date = None
            return NodeStatus(
                name="Notebook", state=state, git_sha=local_main, reason=reason,
                commit_date=notebook_commit_date,
                sync_classification="converged" if state == "converged" else "other_branch",
                blocks_backup=False,
            )
        classification, local_commits, dirty_files, classification_reason = classify_checkout(root, git, expected)
        on_candidate = local == expected and classification == "converged"
        try:
            notebook_commit_date = git(root, ["show", "-s", "--format=%cI", local]) if local else None
        except Exception:
            notebook_commit_date = None
        return NodeStatus(
            name="Notebook", state="converged" if on_candidate else "divergent", git_sha=local,
            reason=classification_reason, commit_date=notebook_commit_date,
            sync_classification=classification, local_commits=local_commits, dirty_files=dirty_files,
        )
    except Exception:
        return NodeStatus(name="Notebook", state="unknown", reason="checkout unavailable")


def verify(
    *, root: Path = MAIN_CHECKOUT, git: GitRunner = run_git,
    http: HttpClient = request_json, desktop_url: str = DESKTOP_URL,
    vps_url: str = VPS_URL,
    retries: int = 3, retry_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    notifier: Callable[[str], bool] | None = None,
    track_divergence: bool = False,
    expected_sha: str | None = None,
    disabled_hosts: set[str] | None = None,
) -> SyncReport:
    expected = _target_sha(expected_sha)
    if expected is None:
        return SyncReport(nodes=[NodeStatus(
            name=n, state="unknown", reason="missing or invalid immutable expected_sha",
        ) for n in ("Notebook", "Desktop", "VPS")])
    disabled = _disabled_hosts(disabled_hosts)
    if "notebook" in disabled:
        # Policy already removed this host. Do not rev-parse, classify, or touch its checkout.
        notebook = _excluded_node("Notebook", expected)
    else:
        notebook = _notebook_against_candidate(root, git, expected)
    desktop = _excluded_node("Desktop", expected) if "desktop" in disabled else _probe(
        "Desktop", f"{desktop_url.rstrip('/')}/health", expected, http,
        retries=retries, retry_delay=retry_delay, sleep=sleep,
    )
    vps = _excluded_node("VPS", expected) if "vps" in disabled else _probe(
        "VPS", vps_url, expected, http, unknown_without_sha=True,
        retries=retries, retry_delay=retry_delay, sleep=sleep,
    )
    if vps.state == "divergent" and vps.git_sha:
        # USR-207: a merge that only touches ledger/docs/tests leaves the Hub image unchanged on purpose.
        try:
            from core.infra import deploy_classifier

            changed = [p for p in git(root, ["diff", "--name-only", vps.git_sha, expected]).splitlines() if p.strip()]
            rebuild, why = deploy_classifier.classify(vps.git_sha, expected, changed)
            if not rebuild:
                vps = vps.model_copy(update={"state": "converged", "reason": f"imagem inalterada: {why}"})
        except Exception:
            pass
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
    expected_sha: str | None = None,
    disabled_hosts: set[str] | None = None,
) -> SyncReport:
    if task_recovery is None:
        task_recovery = recover_scheduled_task
    # Keep one policy snapshot for the whole convergence attempt. The environment
    # may change while polling nodes, but that must not change this run's scope.
    disabled = _disabled_hosts(disabled_hosts)
    initial = verify(
        root=root, git=git, http=http, desktop_url=desktop_url, vps_url=vps_url,
        retries=retries, retry_delay=retry_delay, sleep=sleep, notifier=notifier,
        track_divergence=track_divergence, expected_sha=expected_sha, disabled_hosts=disabled,
    )
    expected = initial.expected_sha
    if expected is None:
        return initial
    notebook = initial.nodes[0]
    if "notebook" in disabled:
        # The frozen policy, not the probed state, decides whether this run may touch the checkout.
        notebook = _excluded_node("Notebook", expected)
        initial.nodes[0] = notebook
    if notebook.state not in ("converged", "excluded"):
        try:
            worktree_blocker = _notebook_worktree_blocker(root, git)
            if worktree_blocker:
                notebook.reason = worktree_blocker
            elif notebook.sync_classification == "other_branch":
                # A different session owns the checked out branch; do not mutate even the main ref.
                notebook.reason = f"{notebook.reason}; no ref update attempted during fixed-target sync"
            elif notebook.sync_classification != "behind":
                # Ahead, diverged, dirty, or unclassified: never chase a moving ref.
                notebook.reason = notebook.reason or (
                    "checkout is not behind the fixed candidate; no git operation attempted"
                )
            elif git(root, ["status", "--porcelain"]):
                notebook.reason = "dirty worktree; no git operation attempted"
            else:
                # Fast-forward only when classification compared HEAD with the immutable candidate.
                git(root, ["fetch", "origin", expected])
                git(root, ["merge", "--ff-only", expected])
                notebook.state = "converged" if _sha(git(root, ["rev-parse", "HEAD"])) == expected else "divergent"
                notebook.sync_classification = "converged" if notebook.state == "converged" else "behind"
                notebook.reason = None if notebook.state == "converged" else "checkout did not reach fixed candidate"
        except Exception as exc:
            notebook.reason = str(exc)

    desktop = initial.nodes[1]
    if "desktop" in disabled:
        desktop = _excluded_node("Desktop", expected)
    observed_divergent_sha = desktop.git_sha if desktop.state == "divergent" else None
    if desktop.state != "converged" and desktop.state != "excluded":
        deadline = clock() + timeout
        while desktop.state != "converged" and clock() < deadline:
            sleep(min(interval, max(0.0, deadline - clock())))
            desktop = _probe(
                "Desktop", f"{desktop_url.rstrip('/')}/health", expected, http,
                retries=retries, retry_delay=retry_delay, sleep=sleep,
            )
            if desktop.state == "divergent":
                observed_divergent_sha = desktop.git_sha

    if desktop.state not in ("converged", "excluded"):
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
                        # USR-196: o stderr/stdout real do `git pull` do worker (campo `output`) vai ao relatorio.
                        detail = redact_worker_output(response.get("output"), token=token)
                        message = "Desktop update did not reach expected SHA"
                        if detail:
                            message += f"; worker output: {detail}"
                        raise RuntimeError(message)
                    http("POST", f"{desktop_url.rstrip('/')}/system/restart", {}, token)
                    initial.nodes[1] = _recover_if_offline(_wait_for_restart())
            except Exception as exc:
                desktop.reason = redact_worker_output(exc, token=token, limit=WORKER_OUTPUT_LIMIT + 200)
                initial.nodes[1] = desktop
                # USR-196: a divergencia do Desktop fica registrada como pendencia explicita (retomavel por `retry`).
                record_pending_convergence("Desktop", expected, desktop.reason)
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

    if "vps" in disabled:
        initial.nodes[2] = _excluded_node("VPS", expected)
    elif initial.nodes[2].state in ("divergent", "offline"):
        try:
            # Default entrypoint redeploys the same candidate. Injected callables
            # stay zero-arg: the parent deploy already shipped this SHA and must
            # not grow a second signature.
            redeployed = redeploy_vps(expected) if vps_redeploy is redeploy_vps else vps_redeploy()
            if not redeployed:
                initial.nodes[2].reason = "Dokploy redeploy failed"
        except Exception as exc:
            initial.nodes[2].reason = f"Dokploy redeploy failed: {type(exc).__name__}"
    final = verify(
        root=root, git=git, http=http, desktop_url=desktop_url, vps_url=vps_url,
        retries=retries, retry_delay=retry_delay, sleep=sleep, notifier=notifier,
        track_divergence=track_divergence, expected_sha=expected, disabled_hosts=disabled,
    )
    for old, new in zip(initial.nodes, final.nodes):
        if new.state != "converged":
            if old.reason and (
                not new.reason
                or any(
                    token in old.reason
                    for token in (
                        "busy", "offline", "failed to fast-forward",
                        "no ref update", "no git operation",
                    )
                )
            ):
                new.reason = old.reason
    if final.nodes[1].state == "converged":
        pending = load_pending_convergence()
        if pending and pending.get("node") == "Desktop":
            clear_pending_convergence()
    return final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "sync", "retry"))
    parser.add_argument(
        "--expected-sha", default=os.environ.get("DARKFAC_EXPECTED_SHA"),
        help="immutable deploy candidate SHA (defaults to DARKFAC_EXPECTED_SHA)",
    )
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
        candidate = _immutable_sha(pending.get("expected_sha"))
        if candidate is None:
            print(
                "Cannot retry node convergence: pending record has no valid immutable candidate SHA",
                file=sys.stderr,
            )
            return 2
        report = sync(**kwargs, token=os.environ.get("DARKFAC_WORKER_TOKEN"), expected_sha=candidate)
    else:
        candidate = _immutable_sha(args.expected_sha)
        if candidate is None:
            print(
                "Cannot converge nodes without an immutable candidate SHA; pass --expected-sha or set DARKFAC_EXPECTED_SHA",
                file=sys.stderr,
            )
            return 2
        if args.action == "verify":
            report = verify(**kwargs, expected_sha=candidate)
        else:
            report = sync(**kwargs, token=os.environ.get("DARKFAC_WORKER_TOKEN"), expected_sha=candidate)
    print(report.model_dump_json())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
