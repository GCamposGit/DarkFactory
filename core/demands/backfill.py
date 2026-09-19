"""Legacy ticket backfill and reconciliation import service (HF-08-03).

Governed by:
- docs/handoffs/continuous-autonomy/HF-08-03.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- ADR-HF-001

Key Invariants:
1. Idempotency: Restarting at or before cursor never duplicates records or creates concurrent runs.
2. Terminal Safety: Legacy tickets in terminal states (COMPLETED, CANCELLED) with valid proof
   are marked terminal and NEVER create or re-execute jobs.
3. Unverified Legacy: Legacy tickets lacking verified terminal proof are marked as
   `reconciliation_required` for deliberate analysis without triggering blind autonomous runs.
4. Single Shared Schema: Uses ControlStore runs/intake_commands/reconciliation_ledger without parallel queues.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Sequence

from pydantic import BaseModel, ConfigDict, Field

from core.demands.models import DeliveryStatus, UserTicket
from core.workflow.control_contracts import (
    RuntimeOwner,
    canonical_payload_digest,
)
from core.workflow.control_store import ControlStore

logger = logging.getLogger("darkfac.demands.backfill")


class ImportItem(BaseModel):
    """Descriptor of an imported legacy ticket."""

    model_config = ConfigDict(extra="ignore")

    project_id: str
    ticket_id: str
    version: str
    payload_fingerprint: str
    status: str = ""
    reconciliation_required: bool = False

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


class ImportPage(BaseModel):
    """Paginated result page for legacy backfill operations."""

    model_config = ConfigDict(extra="ignore")

    cursor: str | None = None
    next_cursor: str | None = None
    items: list[ImportItem] = Field(default_factory=list)
    total_processed: int = 0
    has_more: bool = False


def is_terminal_status(status: Any) -> bool:
    """Check if the given status string or enum indicates a terminal state."""
    if isinstance(status, DeliveryStatus):
        return status in (DeliveryStatus.COMPLETED, DeliveryStatus.CANCELLED)
    if isinstance(status, str):
        return status.lower().strip() in (
            "completed",
            "cancelled",
            "succeeded",
            "closed",
            "discarded",
        )
    return False


def has_valid_terminal_proof(ticket: UserTicket | dict[str, Any]) -> bool:
    """Check if the ticket contains verifiable proof of termination (evidence, commit, etc.)."""
    if isinstance(ticket, UserTicket):
        evidence_refs = getattr(ticket, "evidence_refs", None)
        if evidence_refs and isinstance(evidence_refs, (list, tuple)) and len(evidence_refs) > 0:
            return True

        evidence = getattr(ticket, "evidence", None)
        if evidence:
            if isinstance(evidence, (list, tuple, dict)) and len(evidence) > 0:
                return True
            if isinstance(evidence, str) and evidence.strip():
                return True

        proof = (
            getattr(ticket, "proof", None)
            or getattr(ticket, "terminal_proof", None)
            or getattr(ticket, "resolution_proof", None)
        )
        if proof:
            if isinstance(proof, (dict, list)) and len(proof) > 0:
                return True
            if isinstance(proof, str) and proof.strip():
                return True

        for tag in ticket.tags:
            if tag.startswith("proof:") or tag.startswith("evidence:"):
                return True

        return False

    elif isinstance(ticket, dict):
        for field in (
            "evidence_refs",
            "evidence",
            "proof",
            "terminal_proof",
            "resolution_proof",
            "closure_proof",
            "receipt_id",
            "commit_sha",
        ):
            val = ticket.get(field)
            if val:
                if isinstance(val, (list, tuple, dict)) and len(val) > 0:
                    return True
                if isinstance(val, str) and val.strip():
                    return True

        tags = ticket.get("tags") or []
        if isinstance(tags, (list, tuple)):
            for tag in tags:
                if isinstance(tag, str) and (tag.startswith("proof:") or tag.startswith("evidence:")):
                    return True

        return False

    return False


def extract_ticket_payload(ticket: UserTicket | dict[str, Any]) -> tuple[str, str, str, Any, dict[str, Any]]:
    """Extract standard identifiers and canonical payload dictionary from a ticket."""
    if isinstance(ticket, UserTicket):
        ticket_id = ticket.id
        project_id = ticket.project_id or "darkfac"
        version = "1.0"
        status = ticket.status
        payload = {
            "title": ticket.title,
            "problem": ticket.problem_statement or f"Demand {ticket.id}",
            "journey": ticket.core_journey or [f"User runs {ticket.id}"],
            "non_goals": ticket.non_goals or [],
            "criteria": ticket.acceptance_criteria or ["Verification passes"],
        }
    else:
        ticket_id = str(ticket.get("id") or ticket.get("ticket_id") or "")
        project_id = str(ticket.get("project_id") or "darkfac")
        version = str(ticket.get("version") or "1.0")
        status = ticket.get("status") or "planned"
        journey_raw = ticket.get("journey") or ticket.get("core_journey") or [f"User runs {ticket_id}"]
        journey = journey_raw if isinstance(journey_raw, list) else [str(journey_raw)]
        payload = {
            "title": str(ticket.get("title") or f"Demand {ticket_id}"),
            "problem": str(ticket.get("problem") or ticket.get("problem_statement") or f"Demand {ticket_id}"),
            "journey": journey,
            "non_goals": list(ticket.get("non_goals") or []),
            "criteria": list(ticket.get("criteria") or ticket.get("acceptance_criteria") or ["Verification passes"]),
        }

    return ticket_id, project_id, version, status, payload


def _find_start_index(legacy_tickets: Sequence[UserTicket | dict[str, Any]], cursor: str | None) -> int:
    """Find the zero-based resume index for cursor-based pagination."""
    if not cursor:
        return 0

    # 1. Match by ticket ID
    for idx, item in enumerate(legacy_tickets):
        tid = item.id if isinstance(item, UserTicket) else str(item.get("id") or item.get("ticket_id") or "")
        if tid == cursor:
            return idx + 1

    # 2. Fallback to numeric offset
    if cursor.isdigit():
        num = int(cursor)
        if 0 <= num <= len(legacy_tickets):
            return num

    return 0


def backfill(
    store: ControlStore,
    legacy_tickets: Sequence[UserTicket | dict[str, Any]],
    cursor: str | None = None,
    limit: int = 100,
) -> ImportPage:
    """Import legacy tickets into ControlStore with idempotency, cursor pagination, and safety invariants.

    Invariants:
    - Terminal tickets with valid proof are marked terminal ('completed' or 'cancelled') and do NOT re-execute jobs.
    - Legacy tickets without terminal proof are marked 'reconciliation_required' with zero pending jobs.
    - Restarting before or at cursor does not duplicate records or create concurrent runs.
    """
    effective_limit = max(1, limit)
    start_idx = _find_start_index(legacy_tickets, cursor)
    batch = legacy_tickets[start_idx : start_idx + effective_limit]
    has_more = (start_idx + len(batch)) < len(legacy_tickets)

    imported_items: list[ImportItem] = []
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    has_conn = hasattr(store, "_connect")
    conn = store._connect() if has_conn else None

    try:
        for item in batch:
            ticket_id, project_id, version, status, payload = extract_ticket_payload(item)
            fingerprint = canonical_payload_digest(payload)

            is_term = is_terminal_status(status)
            has_proof = has_valid_terminal_proof(item)

            if is_term and has_proof:
                status_str = status.value if isinstance(status, DeliveryStatus) else str(status).lower()
                effective_status = "cancelled" if "cancel" in status_str else "completed"
                reconciliation_required = False
            else:
                effective_status = "reconciliation_required"
                reconciliation_required = True

            if conn is not None:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.cursor()

                # Check existing run
                cur.execute(
                    "SELECT run_id, status FROM runs WHERE project_id = ? AND demand_id = ? AND demand_version = ?",
                    (project_id, ticket_id, version),
                )
                existing_run = cur.fetchone()

                if existing_run:
                    # Idempotent: record already present, do NOT insert duplicate run or job
                    effective_status = existing_run["status"]
                    reconciliation_required = (effective_status == "reconciliation_required")
                    conn.commit()
                else:
                    run_id = f"legacy-{ticket_id}"
                    completed_at = now_iso if not reconciliation_required else None

                    # 1. Insert into runs
                    cur.execute(
                        """
                        INSERT INTO runs (
                            run_id, project_id, demand_id, demand_version, runtime_owner,
                            mode, status, plan_digest, config_version, created_at, updated_at, completed_at
                        ) VALUES (?, ?, ?, ?, ?, 'documentary', ?, ?, '1.0', ?, ?, ?)
                        """,
                        (
                            run_id,
                            project_id,
                            ticket_id,
                            version,
                            RuntimeOwner.DF11_LEGACY.value,
                            effective_status,
                            fingerprint,
                            now_iso,
                            now_iso,
                            completed_at,
                        ),
                    )

                    # 2. Insert into intake_commands (idempotent primary key on channel, external_id)
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO intake_commands (
                            channel, external_id, project_id, payload_digest, payload,
                            mode, policy_ref, demand_id, demand_version, run_id,
                            initial_job_id, committed_at
                        ) VALUES (?, ?, ?, ?, ?, 'documentary', 'legacy-backfill-v1', ?, ?, ?, NULL, ?)
                        """,
                        (
                            "legacy_backfill",
                            ticket_id,
                            project_id,
                            fingerprint,
                            json.dumps(payload),
                            ticket_id,
                            version,
                            run_id,
                            now_iso,
                        ),
                    )

                    # 3. If reconciliation_required, record in reconciliation_ledger
                    if reconciliation_required:
                        cur.execute(
                            """
                            INSERT INTO reconciliation_ledger (
                                cycle_id, action_type, target_key, reason, status, recorded_at
                            ) VALUES ('backfill', 'flag_reconciliation', ?, 'legacy_missing_terminal_proof', 'success', ?)
                            """,
                            (ticket_id, now_iso),
                        )

                    # Note: No entries inserted into jobs table -> zero jobs created/re-executed!
                    conn.commit()

            imported_items.append(
                ImportItem(
                    project_id=project_id,
                    ticket_id=ticket_id,
                    version=version,
                    payload_fingerprint=fingerprint,
                    status=effective_status,
                    reconciliation_required=reconciliation_required,
                )
            )

    finally:
        if conn is not None:
            conn.close()

    next_cursor = imported_items[-1].ticket_id if (has_more and imported_items) else None

    return ImportPage(
        cursor=cursor,
        next_cursor=next_cursor,
        items=imported_items,
        total_processed=len(imported_items),
        has_more=has_more,
    )
