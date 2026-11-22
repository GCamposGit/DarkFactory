"""Append-only, project-scoped observations shared by learning entry points."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from core.paths import state_root

_SECRET_PATTERNS = (
    re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|authorization)\b\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|AKIA[A-Z0-9]{16})\b"),
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)
_MAX_TEXT = 1200


def sanitize_text(value: str | None) -> str:
    """Bound and redact credential-like values before text enters durable storage."""
    text = str(value or "")
    text = _PRIVATE_KEY.sub("[REDACTED]", text)
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)
    return " ".join(text.split())[:_MAX_TEXT]


def canonical_cause(cause: str | None) -> str:
    """Normalize cause labels so equivalent failures share one adjustment candidate."""
    value = sanitize_text(cause).strip().lower()
    value = re.sub(r"\b(?:run|ticket|attempt)[-_:# ]*[a-z0-9-]+\b", "", value)
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value or "unspecified"


def _project_path(project_id: str, root: Path | None = None) -> Path:
    project = sanitize_text(project_id).strip()
    if not project:
        raise ValueError("project_id is required for a durable observation")
    key = hashlib.sha256(project.encode("utf-8")).hexdigest()[:24]
    return (root or state_root()) / "learning" / "observations" / f"{key}.jsonl"


def _lock_path(observation_path: Path) -> Path:
    """Keep process-lock sidecars outside versioned project state."""
    key = hashlib.sha256(str(observation_path.resolve()).encode("utf-8")).hexdigest()
    return Path(tempfile.gettempdir()) / "darkfac-learning-locks" / f"{key}.lock"


@contextmanager
def _exclusive_file_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.01)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def record_observation(
    *,
    project_id: str,
    identity: str,
    kind: str,
    summary: str,
    intention: str = "",
    harness: str = "unknown",
    run_id: str = "",
    cause: str = "",
    ticket_id: str = "",
    stage: str = "",
    evidence: list[str] | None = None,
    metrics: dict[str, Any] | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    """Persist a sanitized observation once; concurrent writers serialize per project."""
    project = sanitize_text(project_id).strip()
    identity = sanitize_text(identity).strip()
    if not identity:
        raise ValueError("observation identity is required")
    path = _project_path(project, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_cause = canonical_cause(cause)
    safe_metrics = {
        sanitize_text(key): (sanitize_text(value) if isinstance(value, str) else value)
        for key, value in (metrics or {}).items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }
    observation = {
        "identity": identity,
        "project_id": project,
        "kind": sanitize_text(kind) or "interaction",
        "summary": sanitize_text(summary),
        "intention": sanitize_text(intention),
        "harness": sanitize_text(harness) or "unknown",
        "run_id": sanitize_text(run_id),
        "cause": normalized_cause,
        "ticket_id": sanitize_text(ticket_id),
        "stage": sanitize_text(stage),
        "evidence": [sanitize_text(item) for item in (evidence or []) if sanitize_text(item)],
        "metrics": safe_metrics,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _exclusive_file_lock(_lock_path(path)):
        if path.exists():
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    try:
                        existing = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if existing.get("identity") == identity:
                        return existing
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(observation, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return observation


def load_observations(project_id: str, *, root: Path | None = None) -> list[dict[str, Any]]:
    """Load one project's observations after process restart or repository sync."""
    path = _project_path(project_id, root)
    normalized_project_id = sanitize_text(project_id).strip()
    if not path.exists():
        return []
    observations: list[dict[str, Any]] = []
    with _exclusive_file_lock(_lock_path(path)):
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("project_id") == normalized_project_id:
                    observations.append(item)
    return observations


def consolidate_by_cause(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one adjustment candidate per canonical failure cause."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in observations:
        if item.get("kind") not in {"harness_failure", "failure"}:
            continue
        project = str(item.get("project_id", ""))
        cause = canonical_cause(str(item.get("cause", "")))
        grouped.setdefault((project, cause), []).append(item)
    return [
        {
            "project_id": project,
            "cause": cause,
            "candidate_id": hashlib.sha256(f"{project}:{cause}".encode("utf-8")).hexdigest()[:20],
            "occurrence_count": len(items),
            "identities": [item["identity"] for item in items],
        }
        for (project, cause), items in sorted(grouped.items())
    ]


def record_intervention_outcome(
    *, project_id: str, adjustment_id: str, independent_case_id: str,
    interventions_before: int, interventions_after: int, root: Path | None = None,
) -> dict[str, Any]:
    """Record measured before/after intervention counts for an independent case."""
    if interventions_before < 0 or interventions_after < 0:
        raise ValueError("intervention counts cannot be negative")
    return record_observation(
        project_id=project_id,
        identity=f"intervention:{adjustment_id}:{independent_case_id}",
        kind="intervention_outcome",
        summary="Measured interventions on an independent case",
        intention="verify that the adjustment reduces required operator interventions",
        cause=adjustment_id,
        root=root,
        metrics={
            "independent_case_id": sanitize_text(independent_case_id),
            "interventions_before": interventions_before,
            "interventions_after": interventions_after,
        },
    )

