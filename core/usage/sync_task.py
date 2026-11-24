"""Snapshot-only publication and portable scheduling policy for usage telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PureWindowsPath

TASK_NAME = "DarkFac-Usage-Cloud-Sync"
WORKTREE_MARKERS: tuple[str, ...] = (".worktrees", ".claude/worktrees", ".codex/worktrees")


class SyncTaskAction(str, Enum):
    CREATE = "create"
    KEEP = "keep"
    UPDATE = "update"
    REFUSE = "refuse"


@dataclass(frozen=True)
class SyncTaskState:
    """Estado observado da tarefa: script e diretorio de trabalho registrados."""

    script_path: str
    working_directory: str = ""
    schedule_current: bool = True


@dataclass(frozen=True)
class SyncTaskDecision:
    action: SyncTaskAction
    reason: str


def normalize_path(path: str) -> str:
    """Normaliza para comparacao: barras `/`, sem aspas nem barra final, minusculas."""
    cleaned = path.strip().strip('"').replace("\\", "/")
    while len(cleaned) > 1 and cleaned.endswith("/"):
        cleaned = cleaned[:-1]
    return cleaned.lower()


def is_worktree_path(path: str) -> bool:
    """Indica se o caminho passa por uma worktree (`.worktrees`, `.claude|.codex/worktrees`)."""
    norm = "/" + normalize_path(path) + "/"
    return any(f"/{marker}/" in norm for marker in WORKTREE_MARKERS)


def expected_script_path(root: str) -> str:
    return str(PureWindowsPath(root) / "scripts" / "sync_usage_to_cloud.py")


def decide_sync_task(
    current: SyncTaskState | None, desired_root: str, execution_root: str
) -> SyncTaskDecision:
    """Decide a acao sobre a tarefa; recusa quando a raiz de execucao ou a desejada e worktree."""
    if is_worktree_path(execution_root):
        return SyncTaskDecision(
            SyncTaskAction.REFUSE,
            f"a raiz de execucao '{execution_root}' e uma worktree; rode a partir do checkout principal",
        )
    if is_worktree_path(desired_root):
        return SyncTaskDecision(
            SyncTaskAction.REFUSE, f"a raiz desejada '{desired_root}' e uma worktree"
        )
    if current is None:
        return SyncTaskDecision(SyncTaskAction.CREATE, "tarefa ausente")

    if is_worktree_path(current.script_path) or is_worktree_path(current.working_directory):
        return SyncTaskDecision(
            SyncTaskAction.UPDATE,
            f"tarefa aponta para worktree ('{current.script_path}')",
        )
    if not current.schedule_current:
        return SyncTaskDecision(SyncTaskAction.UPDATE, "schedule disabled, interactive or missing five-minute trigger")
    wanted = expected_script_path(desired_root)
    if normalize_path(current.script_path) != normalize_path(wanted):
        return SyncTaskDecision(
            SyncTaskAction.UPDATE,
            f"caminho atual '{current.script_path}' difere do esperado '{wanted}'",
        )
    if current.working_directory and normalize_path(current.working_directory) != normalize_path(
        desired_root
    ):
        return SyncTaskDecision(
            SyncTaskAction.UPDATE,
            f"diretorio de trabalho '{current.working_directory}' difere de '{desired_root}'",
        )
    return SyncTaskDecision(SyncTaskAction.KEEP, "tarefa ja aponta para o checkout principal")

import json
import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from pydantic import BaseModel
from core.usage.models import snapshot_age_seconds

logger = logging.getLogger("darkfac.sync_usage")

def _unquote_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", "'"):
        return value[1:-1].strip()
    return value


def load_telemetry_key(
    repo_root: Path,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """Read the telemetry key at runtime, preferring the process environment to local .env."""
    environment = os.environ if environ is None else environ
    from_environment = _unquote_env_value(environment.get("DARKFAC_TELEMETRY_KEY", ""))
    if from_environment:
        return from_environment

    env_file = Path(repo_root) / ".env"
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            candidate = line.strip()
            if not candidate or candidate.startswith("#"):
                continue
            if candidate.lower().startswith("export "):
                candidate = candidate[7:].lstrip()
            name, separator, value = candidate.partition("=")
            if separator and name.strip() == "DARKFAC_TELEMETRY_KEY":
                return _unquote_env_value(value)
    except OSError:
        return ""
    return ""


def configure_sync_logging(repo_root: Path) -> None:
    """Persist task diagnostics locally because pythonw has no visible console."""
    logger.setLevel(logging.INFO)
    if any(getattr(handler, "_darkfac_sync_file", False) for handler in logger.handlers):
        return
    try:
        log_dir = Path(repo_root) / ".factory" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / "usage_sync.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler._darkfac_sync_file = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
        logger.addHandler(handler)
    except OSError as exc:
        logger.warning("Could not open local sync log: %s", type(exc).__name__)


def collect_local_usage_payload(
    repo_root: Path,
    node_id: str = "workstation",
    force_probe: bool = False,
) -> Dict[str, Any]:
    """Collects local provider accounts and API credits into a unified sync payload."""
    # Publication reads files only: even inspect(force=False) may probe stale accounts.
    def read_snapshots(kind: str) -> list[dict[str, Any]]:
        rows = []
        for path in sorted((repo_root / ".factory" / "usage" / kind).glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError(f"invalid {kind} snapshot")
            rows.append(payload)
        return rows

    return {
        "client_node_id": node_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "accounts": read_snapshots("providers"),
        "credits": read_snapshots("credits"),
    }


def _settle_published_refresh_requests(provider_dir: Path, node_id: str, accounts: list[dict[str, Any]]) -> None:
    """A successful telemetry publish is the durable acknowledgement for local refresh requests."""
    try:
        from core.usage.refresh_requests import RefreshRequestStore

        RefreshRequestStore(Path(provider_dir).parent / "refresh_requests.json").complete_from_snapshots(
            node_id, accounts
        )
    except Exception as exc:  # publication success must not be reported as failed by local bookkeeping
        logger.warning("Could not settle published quota refresh requests: %s", type(exc).__name__)


class PublishResult(BaseModel):
    """A publication receipt; errors contain no response body, token or database URL."""

    ok: bool = False
    http_status: int | None = None
    oldest_snapshot_age_seconds: int | None = None
    error: str | None = None


def publish_once(
    repo_root: Path | None = None,
    *,
    target_url: str | None = None,
    node_id: str | None = None,
) -> PublishResult:
    """Publish existing snapshots without probing; HTTP timeout is six seconds; never raises."""
    age = None
    try:
        import httpx
        from core.paths import project_root

        root = repo_root if repo_root is not None else project_root()
        key = load_telemetry_key(root)
        if not key:
            return PublishResult(error="telemetry_key_missing")
        payload = collect_local_usage_payload(root, node_id or os.environ.get("DARKFAC_NODE_ID", "workstation"), force_probe=False)
        snapshots = payload["accounts"] + payload["credits"]
        ages = [snapshot_age_seconds(row.get("checked_at")) for row in snapshots]
        age = max(ages) if ages and all(a is not None for a in ages) else None
        if not payload["accounts"]:
            return PublishResult(error="account_snapshots_missing", oldest_snapshot_age_seconds=age)
        response = httpx.post(
            f"{(target_url or os.environ.get('DARKHUB_CLOUD_URL', 'https://darkhub.ggcampos.com')).rstrip('/')}/api/usage/sync",
            json=payload,
            headers={"X-DarkFac-Telemetry-Key": key},
            timeout=6.0,
        )
        ok = 200 <= response.status_code < 300
        if ok:
            _settle_published_refresh_requests(
                root / ".factory" / "usage" / "providers",
                node_id or os.environ.get("DARKFAC_NODE_ID", "workstation"),
                payload["accounts"],
            )
        return PublishResult(ok=ok, http_status=response.status_code,
                             oldest_snapshot_age_seconds=age, error=None if ok else "http_error")
    except Exception as exc:
        return PublishResult(oldest_snapshot_age_seconds=age, error=type(exc).__name__)
