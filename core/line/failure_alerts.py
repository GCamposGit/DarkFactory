"""One Hub/Telegram alert per deterministic failure fingerprint."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_LEDGER_LOCK = threading.RLock()


def failure_fingerprint(ticket: str, cause: str, evidence: str) -> str:
    """Identity of one actionable failure: ticket, cause and evidence, nothing else."""
    payload = json.dumps(
        {"ticket": str(ticket).strip(), "cause": str(cause).strip(), "evidence": str(evidence).strip()},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ledger_path() -> Path:
    from core.paths import state_root

    return state_root() / "failure_alert_fingerprints.json"


def _actionable_path() -> Path:
    from core.paths import state_root

    return state_root() / "actionable_failures.json"


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _path_mutex(path: Path):
    """Use the repository's process-safe ledger lock for state shared by workers."""
    from core.demands.store import _PathMutex

    return _PathMutex(path, timeout=5.0)


def _load_actionable() -> dict[str, dict[str, Any]]:
    path = _actionable_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"actionable failure ledger is unreadable: {path}") from exc
    if not isinstance(loaded, dict) or loaded.get("schema_version") != 1 or not isinstance(loaded.get("tickets"), dict):
        raise RuntimeError(f"actionable failure ledger is contradictory: {path}")
    return {str(key): value for key, value in loaded["tickets"].items() if isinstance(value, dict)}


def _actionable_record(ticket: str) -> dict[str, Any] | None:
    try:
        return _load_actionable().get(str(ticket).strip())
    except RuntimeError:
        logger.exception("could not read actionable failure for %s", ticket)
        return None


def record_actionable_failure(
    ticket: str,
    cause: str,
    evidence: str,
    *,
    phase: str = "unknown",
    attempts: int = 0,
    next_step: str = "Inspect the recorded evidence and correct the failure before retrying.",
    transition: str = "retrying",
) -> dict[str, Any]:
    """Persist the latest action for a ticket before any alert is emitted."""
    ticket_value = str(ticket).strip()
    cause_value = str(cause).strip()
    evidence_value = str(evidence).strip()[:1000]
    fingerprint = failure_fingerprint(ticket_value, cause_value, evidence_value)
    record = {
        "ticket": ticket_value,
        "phase": str(phase).strip() or "unknown",
        "cause": cause_value,
        "fingerprint": fingerprint,
        "attempts": max(0, int(attempts)),
        "last_evidence": evidence_value,
        "next_step": str(next_step).strip(),
        "transition": str(transition).strip() or "retrying",
    }
    path = _actionable_path()
    with _LEDGER_LOCK, _path_mutex(path):
        tickets = _load_actionable()
        previous = tickets.get(ticket_value)
        comparable = {key: value for key, value in record.items() if key != "attempts"}
        previous_comparable = ({key: value for key, value in previous.items() if key not in {"attempts", "updated_at"}}
                               if previous else None)
        if previous_comparable != comparable or not previous:
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            tickets[ticket_value] = record
            _atomic_json(path, {"schema_version": 1, "tickets": tickets})
        else:
            # Attempts are useful evidence even when an identical failure repeats.
            if int(previous.get("attempts", 0)) != record["attempts"]:
                previous["attempts"] = record["attempts"]
                previous["updated_at"] = datetime.now(timezone.utc).isoformat()
                _atomic_json(path, {"schema_version": 1, "tickets": tickets})
                record = previous
            else:
                record = previous
    return dict(record)


def _load_ledger() -> dict[str, dict[str, str]]:
    path = _ledger_path()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError(f"failure alert ledger is unreadable: {path}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"failure alert ledger is contradictory: {path}")
    return {str(key): value for key, value in loaded.items() if isinstance(value, dict)}


def _remember(fingerprint: str, ticket: str, cause: str, evidence: str) -> None:
    path = _ledger_path()
    with _LEDGER_LOCK, _path_mutex(path):
        ledger = _load_ledger()
        previous = ledger.get(fingerprint, {})
        previous.update({
            "ticket": ticket, "cause": cause, "evidence": evidence,
            "status": "sent", "alerted_at": datetime.now(timezone.utc).isoformat(),
        })
        ledger[fingerprint] = previous
        _atomic_json(path, ledger)


def _emit_failure_alert(
    *, ticket: str, cause: str, evidence: str, log_path: str, title: str,
    phase: str = "unknown", fingerprint: str = "", attempts: int = 0,
    next_step: str = "", transition: str = "failure",
) -> bool:
    from core.notifications.models import AlertCategory, AlertSeverity, NotificationChannel
    from core.notifications.service import NotificationService

    try:
        event = NotificationService().notify(
            category=AlertCategory.SYSTEM_HEALTH, severity=AlertSeverity.CRITICAL,
            title=title,
            message=(f"Fase: {phase}\nCausa: {cause}\nFingerprint: {fingerprint}\n"
                     f"Tentativas: {attempts}\nUltima evidencia: {evidence}\n"
                     f"Proximo passo: {next_step}\nLog: {log_path}"),
            channel=NotificationChannel.ALL,
            details={"ticket": ticket, "phase": phase, "cause": cause, "fingerprint": fingerprint,
                     "attempts": attempts, "last_evidence": evidence, "next_step": next_step,
                     "transition": transition, "log": log_path},
        )
    except Exception:
        logger.exception("Notification failed for deterministic failure %s", ticket)
        return False
    return event is not None


def notify_deterministic_failure(
    ticket: str,
    cause: str,
    evidence: str,
    log_path: str = "",
    *,
    phase: str = "unknown",
    attempts: int = 0,
    next_step: str = "Inspect the recorded evidence and correct the failure before retrying.",
    transition: str = "retrying",
) -> bool:
    """Alert once for this fingerprint. The same cause and evidence stay silent.

    A different cause or a different evidence string is a new fingerprint and
    alerts exactly once. A failed send is not recorded, so a later attempt can.
    """
    action = record_actionable_failure(
        ticket, cause, evidence, phase=phase, attempts=attempts,
        next_step=next_step, transition=transition,
    )
    fingerprint = str(action["fingerprint"])
    ledger_path = _ledger_path()
    alert_key = f"{fingerprint}:{transition}"
    try:
        with _LEDGER_LOCK, _path_mutex(ledger_path):
            ledger = _load_ledger()
            # Older releases keyed directly by fingerprint. Preserve their dedupe behavior.
            if alert_key in ledger or (transition == "retrying" and fingerprint in ledger):
                return False
            ledger[alert_key] = {
                "ticket": str(ticket), "cause": str(cause), "evidence": str(evidence),
                "transition": transition, "status": "sending",
                "reserved_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic_json(ledger_path, ledger)
    except Exception:
        logger.exception("could not reserve failure alert for %s", ticket)
        return False

    sent = _emit_failure_alert(
        ticket=ticket, cause=cause, evidence=evidence, log_path=log_path,
        title=f"Falha deterministica em {ticket}", phase=phase, fingerprint=fingerprint,
        attempts=attempts, next_step=next_step, transition=transition,
    )
    if sent:
        _remember(alert_key, ticket, cause, evidence)
        return True
    try:
        with _LEDGER_LOCK, _path_mutex(ledger_path):
            ledger = _load_ledger()
            if ledger.get(alert_key, {}).get("status") == "sending":
                del ledger[alert_key]
                _atomic_json(ledger_path, ledger)
    except Exception:
        logger.exception("could not release unsent failure alert for %s", ticket)
    return False


def notify_ticket_hold(ticket: str, cause: str, detail: str, log_path: str) -> None:
    """Hold notification uses the same fingerprint as any other deterministic failure."""
    record = _actionable_record(ticket) or {}
    notify_deterministic_failure(
        ticket, cause, detail, log_path,
        phase=str(record.get("phase") or "unknown"),
        attempts=int(record.get("attempts") or 0),
        next_step=str(record.get("next_step") or "Review the evidence, correct the cause, then release the ticket hold."),
        transition="blocked",
    )
