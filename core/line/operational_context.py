"""Single operational-context receipt (USR-471 T6).

Reconciles, with one read-only command, the three facts the 05-06/11 audit found scattered
across chat, host config and model registry: which harness THIS process is, what the fleet's
effective host policy currently is, and whether the configured model route matches what each
executor actually reported.

Pure composition: every fact comes from an existing, already-tested entry point --
`core.line.operating_harness.resolve_process_identity`, `core.line.fleet_policy.build_effective_policy_report`
and the model-audit ledger reader. No routing, admission or host decision is made here, and
nothing is queued, scheduled or restarted: an offline or unreadable host, or a ticket this receipt
reconciles against, only ever appears with a `reason` in the output.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.paths import state_root
from core.line import fleet_policy
from core.line.operating_harness import ProcessHarnessIdentity, resolve_process_identity
from core.models import audit as model_audit

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RECEIPT_PATH = _REPO_ROOT / "docs" / "evidence" / "USR-471-operational-context.json"

#: Tickets whose own operational acceptance criteria this receipt reconciles against (USR-471
#: intake), read-only via `core.demands`: this module never creates, reopens or changes their
#: status, so the subproblems they track are never duplicated by a new ticket.
RECONCILED_TICKETS: tuple[str, ...] = ("USR-452", "USR-454", "USR-464", "USR-466", "USR-456")

# Historical claims from the USR-471 intake are retained as claims, never promoted to
# observations. Evidence paths are repo-relative so a receipt can be interpreted on another
# host without exposing arbitrary local filesystem locations.
INTAKE_REPORTED_FACTS: tuple[dict[str, str], ...] = (
    {"claim": "Notebook had 2.89 GiB free memory and zero workers during the 06/11 audit.", "source": "USR-471 intake, evidence dated 2026-11-06"},
    {"claim": "Desktop was healthy and idle but explicitly excluded by fleet policy; Grokbox was offline.", "source": "USR-471 intake, evidence dated 2026-11-06"},
    {"claim": "Notebook and Desktop converged at SHA 11366cac.", "source": "USR-471 intake, evidence dated 2026-11-06"},
    {"claim": "A PT1M TimeTrigger with IgnoreNew recovered exit 15 from PID 13084 to PID 14520 while preserving queue and SHA.", "source": "USR-471 intake, evidence dated 2026-11-07"},
    {"claim": "Review through Grok took about 10 minutes without valid schema; Sonnet via OpenRouter completed reviews in 9–13 seconds after reasoning was corrected.", "source": "USR-471 intake, evidence dated 2026-11-06"},
    {"claim": "The model registry configured distinct Codex roles for development (development.codex) and planning (planning.codex) at the time of the audit.", "source": "USR-471 intake, evidence dated 2026-11-06"},
)
HISTORICAL_EVIDENCE_PATHS: tuple[str, ...] = (
    "docs/evidence/usr452-node-verify-20261106.json",
)


def _identity_as_dict(identity: ProcessHarnessIdentity) -> dict[str, Any]:
    return {
        "harness": identity.harness,
        "origin": identity.origin,
        "process_harness": identity.process_harness,
        "process_harness_origin": identity.process_harness_origin,
        "execution_context": identity.execution_context,
        "project_root": str(identity.project_root),
        "state_root": str(identity.state_root),
        "mode": identity.mode,
        "write_capable": identity.write_capable,
    }


def _audit_summary(report: model_audit.AuditReport) -> dict[str, Any]:
    """Compact view of `AuditReport`: pass/fail plus the effective route per stage.harness."""
    by_route: dict[str, Any] = {}
    for sample in report.samples:
        key = f"{sample.get('stage')}.{sample.get('harness')}"
        by_route[key] = {
            "execution_id": sample.get("execution_id"),
            "provider": sample.get("provider"),
            "model_source": sample.get("model_source"),
            "effective_model": sample.get("effective_model"),
            "reported_model": sample.get("reported_model"),
            "requested_model": sample.get("requested_model"),
            "started_at": sample.get("started_at"),
            "finished_at": sample.get("finished_at"),
            "complexity": sample.get("complexity"),
            "context_tokens": sample.get("context_tokens"),
            "reported_context_tokens": sample.get("reported_context_tokens"),
            "context_sha256": sample.get("context_sha256"),
            "context_bytes": sample.get("context_bytes"),
            "latency_ms": sample.get("latency_ms"),
            "schema_valid": sample.get("schema_valid"),
            "real_delivery": sample.get("real_delivery"),
            "delivery_result": sample.get("delivery_result"),
        }
    return {
        "audited_at": report.audited_at,
        "passed": report.passed,
        "sample_count": len(report.samples),
        "configured_models": dict(report.configured_models),
        "violations": list(report.violations),
        "divergences": list(report.divergences),
        "by_route": by_route,
    }


def _read_current_policy(config: Optional[fleet_policy.FleetConfig]) -> dict[str, Any]:
    """Compute live policy without writing the fleet snapshot sidecar."""
    return fleet_policy.build_effective_policy_report(config)


def _read_current_model_audit(ticket_id: Optional[str]) -> model_audit.AuditReport:
    """Audit recorded samples without writing the standalone model-audit sidecar."""
    samples = model_audit._recorded_samples(state_root() / "runs" / "model_audit", ticket_id)
    return model_audit.audit_executions(samples)


def _reconciled_ticket_notes(ticket_ids: tuple[str, ...]) -> dict[str, Any]:
    """Read-only status of `ticket_ids` via `core.demands` -- the reconciliation note this
    receipt carries instead of opening a duplicate ticket. Never writes a ticket."""
    from core.demands import build_default_demands_service

    notes: dict[str, Any] = {}
    try:
        service = build_default_demands_service()
    except Exception as exc:  # noqa: BLE001 - the demands backend being unreachable is evidence, not a crash
        logger.warning("operational_context: demands service unavailable: %s", exc)
        return {ticket_id: {"status": None, "reason": f"demands_service_unavailable:{exc}"} for ticket_id in ticket_ids}

    for ticket_id in ticket_ids:
        try:
            ticket = service.get_ticket(ticket_id)
        except Exception as exc:  # noqa: BLE001 - one unreadable ticket must not sink the receipt
            logger.warning("operational_context: could not read %s: %s", ticket_id, exc)
            notes[ticket_id] = {"status": None, "reason": f"unreadable:{exc}"}
            continue
        if ticket is None:
            notes[ticket_id] = {"status": None, "reason": "ticket_not_found"}
        else:
            notes[ticket_id] = {"status": ticket.status.value, "title": ticket.title}
    return notes


def build_operational_context_receipt(
    *,
    identity_kwargs: Optional[dict[str, Any]] = None,
    fleet_config: Optional[fleet_policy.FleetConfig] = None,
    audit_ticket_id: Optional[str] = None,
    reconciled_tickets: tuple[str, ...] = RECONCILED_TICKETS,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The single receipt reconciling process identity, effective fleet policy and model-route
    audit (USR-471 T6). Reads do not write fleet-policy or model-audit sidecars; only the
    explicitly requested operational evidence receipt is persisted by the CLI.
    """
    instant = now or datetime.now(timezone.utc)
    identity = resolve_process_identity(**(identity_kwargs or {}))
    policy = _read_current_policy(fleet_config)
    audit_report = _read_current_model_audit(audit_ticket_id)
    missing_historical_evidence = [
        evidence_path for evidence_path in HISTORICAL_EVIDENCE_PATHS
        if not (_REPO_ROOT / evidence_path).is_file()
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": instant.isoformat(),
        "identity": _identity_as_dict(identity),
        "hosts": policy.get("hosts", []),
        "max_fleet_slots": policy.get("max_fleet_slots"),
        "reservation_state": policy.get("reservation_state"),
        "policy_observed_at": policy.get("generated_at"),
        "model_audit": _audit_summary(audit_report),
        "reconciled_tickets": _reconciled_ticket_notes(reconciled_tickets),
        "provenance": {
            "observed_now": ["identity", "hosts", "model_audit", "reconciled_tickets"],
            "intake_reported": list(INTAKE_REPORTED_FACTS),
            "historical_evidence_unavailable": missing_historical_evidence,
            "limits": [
                "A zero-sample executor audit means no local execution samples were available; it is not evidence of a successful model delivery.",
                "Current host readings describe the machine and policy visible to this process, not production hosts on another machine.",
                "When fleet reservation state is unavailable, remote hosts are fail-closed by fleet policy; this receipt preserves that state explicitly.",
                "Ticket status is the status returned by the demands store at receipt generation time; status alone does not prove each ticket's acceptance criteria.",
            ],
        },
    }


def write_receipt(receipt: dict[str, Any], *, path: Optional[Path] = None) -> Path:
    """Persist `receipt` atomically (temp file + replace), same pattern as
    `fleet_policy.write_effective_policy_snapshot`."""
    target = path if path is not None else DEFAULT_RECEIPT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, target)
    return target


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="DarkFac operational-context receipt (USR-471 T6)")
    parser.add_argument(
        "--verify", action="store_true",
        help="Observe process identity, effective fleet policy, executor audit and related ticket statuses; write the evidence receipt",
    )
    parser.add_argument("--out", type=Path, default=None, help="Override the receipt path")
    parser.add_argument("--ticket", type=str, default=None, help="Limit the model audit to one ticket's ledger")
    args = parser.parse_args(argv)
    if not args.verify:
        parser.print_help()
        return 1
    receipt = build_operational_context_receipt(audit_ticket_id=args.ticket)
    target = write_receipt(receipt, path=args.out)
    print(json.dumps(receipt, indent=2, ensure_ascii=False))
    print(f"wrote {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
