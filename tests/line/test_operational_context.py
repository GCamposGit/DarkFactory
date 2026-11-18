"""USR-471 T3: one read-only receipt reconciling process identity, effective fleet policy and
model-route audit. Everything is injected -- these tests never touch real hosts, the real
model-audit ledger or the real demands backend.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from core.demands.models import DemandOrigin, UserTicket
from core.line import operational_context as oc
from core.line.fleet_policy import FleetConfig, HostConfig
from core.line.operating_harness import ProcessHarnessIdentity
from core.models.audit import AuditReport
from core.roadmap.models import DeliveryStatus


def _identity(**overrides: Any) -> ProcessHarnessIdentity:
    defaults: dict[str, Any] = dict(
        harness=None,
        origin="headless",
        execution_context="headless",
        project_root=Path("/repo"),
        state_root=Path("/repo/.factory"),
        mode="write",
        write_capable=True,
    )
    defaults.update(overrides)
    return ProcessHarnessIdentity(**defaults)


def _policy(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": "2026-11-08T00:00:00+00:00",
        "max_fleet_slots": 3,
        "reservation_state": "unavailable",
        "hosts": [
            {"host_id": "grokbox", "eligible": False, "reason": "resource_reading_missing_or_stale"},
            {"host_id": "notebook", "eligible": False, "reason": "zero_workers"},
            {"host_id": "desktop", "eligible": False, "reason": "excluded"},
        ],
    }
    base.update(overrides)
    return base


def _audit_report(**overrides: Any) -> AuditReport:
    base: dict[str, Any] = dict(
        audited_at="2026-11-08T00:00:00+00:00",
        passed=True,
        configured_models={
            "development.codex": "gpt-6-luna",
            "planning.codex": "gpt-sol",
        },
        samples=[
            {
                "stage": "development",
                "harness": "codex",
                "provider": "openai",
                "model_source": "provider_reported",
                "effective_model": "gpt-sol",
                "reported_model": "gpt-sol",
                "requested_model": "gpt-sol",
                "complexity": "medium",
                "context_tokens": 500000,
                "reported_context_tokens": 120,
                "latency_ms": 9000,
                "schema_valid": True,
                "real_delivery": True,
                "delivery_result": "delivered",
            }
        ],
        divergences=[],
        violations=[],
    )
    base.update(overrides)
    return AuditReport(**base)


def _ticket(ticket_id: str, status: DeliveryStatus, title: str = "titulo") -> UserTicket:
    return UserTicket(id=ticket_id, title=title, origin=DemandOrigin.USER, status=status)


class _FakeDemandsService:
    def __init__(self, tickets: dict[str, UserTicket | Exception | None]) -> None:
        self._tickets = tickets

    def get_ticket(self, ticket_id: str) -> UserTicket | None:
        value = self._tickets.get(ticket_id)
        if isinstance(value, Exception):
            raise value
        return value


def _patch_core(
    monkeypatch: pytest.MonkeyPatch,
    *,
    identity: ProcessHarnessIdentity | None = None,
    policy: dict[str, Any] | None = None,
    report: AuditReport | None = None,
) -> dict[str, list[Any]]:
    """Patch the three composed entry points and record the calls each receives."""
    calls: dict[str, list[Any]] = {"identity": [], "policy": [], "audit": []}

    def fake_identity(**kwargs: Any) -> ProcessHarnessIdentity:
        calls["identity"].append(kwargs)
        return identity or _identity()

    def fake_policy(config: Any = None) -> dict[str, Any]:
        calls["policy"].append(config)
        return policy or _policy()

    def fake_audit(ticket_id: str | None = None) -> AuditReport:
        calls["audit"].append(ticket_id)
        return report or _audit_report()

    monkeypatch.setattr(oc, "resolve_process_identity", fake_identity)
    monkeypatch.setattr(oc, "_read_current_policy", fake_policy)
    monkeypatch.setattr(oc, "_read_current_model_audit", fake_audit)
    return calls


# --------------------------------------------------------------------------
# build_operational_context_receipt: pure composition
# --------------------------------------------------------------------------


def test_receipt_composes_identity_policy_and_audit_without_any_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_core(monkeypatch)
    monkeypatch.setattr(oc, "_reconciled_ticket_notes", lambda ticket_ids: {t: {"status": "planned"} for t in ticket_ids})

    receipt = oc.build_operational_context_receipt()

    assert receipt["schema_version"] == oc.SCHEMA_VERSION
    assert receipt["identity"]["harness"] is None
    assert receipt["identity"]["origin"] == "headless"
    assert receipt["identity"]["process_harness"] is None
    assert receipt["identity"]["process_harness_origin"] == "none"
    assert receipt["identity"]["execution_context"] == "headless"
    assert receipt["identity"]["project_root"] == str(Path("/repo"))
    assert receipt["identity"]["state_root"] == str(Path("/repo/.factory"))
    assert receipt["identity"]["write_capable"] is True
    assert receipt["identity"]["mode"] == "write"
    assert receipt["hosts"][0]["host_id"] == "grokbox"
    assert receipt["hosts"][0]["reason"] == "resource_reading_missing_or_stale"
    assert receipt["max_fleet_slots"] == 3
    assert receipt["reservation_state"] == "unavailable"
    assert receipt["policy_observed_at"] == "2026-11-08T00:00:00+00:00"
    assert receipt["model_audit"]["passed"] is True
    assert "generated_at" in receipt
    assert len(calls["identity"]) == 1
    assert len(calls["policy"]) == 1
    assert len(calls["audit"]) == 1


def test_receipt_forwards_identity_kwargs_fleet_config_and_audit_ticket(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_core(monkeypatch)
    monkeypatch.setattr(oc, "_reconciled_ticket_notes", lambda ticket_ids: {})
    config = FleetConfig(hosts=[HostConfig(id="notebook", name="Notebook", priority=1)])

    oc.build_operational_context_receipt(
        identity_kwargs={"explicit": "codex", "execution_context": "headless"},
        fleet_config=config,
        audit_ticket_id="USR-471",
    )

    assert calls["identity"] == [{"explicit": "codex", "execution_context": "headless"}]
    assert calls["policy"] == [config]
    assert calls["audit"] == ["USR-471"]


def test_receipt_includes_a_model_audit_summary_per_stage_harness_route(monkeypatch: pytest.MonkeyPatch) -> None:
    report = _audit_report(
        passed=False,
        violations=["quota_floor:exec-1"],
        divergences=["stage_model_divergence:codex:development=gpt-sol,planning=gpt-6-luna"],
        samples=[
            {
                "stage": "development", "harness": "codex", "effective_model": "gpt-sol",
                "reported_model": "gpt-sol", "complexity": "medium", "latency_ms": 9500, "real_delivery": True,
                "provider": "openai", "model_source": "provider_reported", "requested_model": "gpt-sol",
                "context_tokens": 500000, "reported_context_tokens": 120, "schema_valid": True,
                "delivery_result": "delivered",
            },
            {
                "stage": "planning", "harness": "codex", "effective_model": "gpt-6-luna",
                "reported_model": "gpt-6-luna", "complexity": "high", "latency_ms": 12000, "real_delivery": True,
            },
        ],
    )
    _patch_core(monkeypatch, report=report)
    monkeypatch.setattr(oc, "_reconciled_ticket_notes", lambda ticket_ids: {})

    receipt = oc.build_operational_context_receipt()

    summary = receipt["model_audit"]
    assert summary["passed"] is False
    assert summary["sample_count"] == 2
    assert summary["violations"] == ["quota_floor:exec-1"]
    assert summary["by_route"]["development.codex"]["effective_model"] == "gpt-sol"
    assert summary["by_route"]["planning.codex"]["effective_model"] == "gpt-6-luna"
    assert summary["configured_models"]["development.codex"] == "gpt-6-luna"
    assert summary["configured_models"]["planning.codex"] == "gpt-sol"
    assert summary["by_route"]["development.codex"]["provider"] == "openai"
    assert summary["by_route"]["development.codex"]["model_source"] == "provider_reported"
    assert summary["by_route"]["development.codex"]["reported_context_tokens"] == 120
    assert summary["by_route"]["development.codex"]["delivery_result"] == "delivered"


def test_receipt_separates_live_observations_intake_claims_and_missing_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_core(monkeypatch)
    monkeypatch.setattr(oc, "_reconciled_ticket_notes", lambda ticket_ids: {t: {"status": "planned"} for t in ticket_ids})
    monkeypatch.setattr(oc, "_REPO_ROOT", Path("/receipt-repo"))
    monkeypatch.setattr(oc.Path, "is_file", lambda self: False)

    receipt = oc.build_operational_context_receipt()

    provenance = receipt["provenance"]
    assert provenance["observed_now"] == ["identity", "hosts", "model_audit", "reconciled_tickets"]
    assert any("2.89 GiB" in item["claim"] for item in provenance["intake_reported"])
    assert provenance["historical_evidence_unavailable"] == ["docs/evidence/usr452-node-verify-20261106.json"]
    assert any("zero-sample" in limit for limit in provenance["limits"])


def test_empty_executor_audit_is_not_described_as_verified_delivery(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_core(monkeypatch, report=_audit_report(passed=True, samples=[]))
    monkeypatch.setattr(oc, "_reconciled_ticket_notes", lambda ticket_ids: {})

    receipt = oc.build_operational_context_receipt()

    assert receipt["model_audit"]["sample_count"] == 0
    assert receipt["provenance"]["observed_now"]
    assert any("not evidence of a successful model delivery" in limit for limit in receipt["provenance"]["limits"])


def test_operational_receipt_reads_policy_without_writing_fleet_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _policy()
    monkeypatch.setattr(oc.fleet_policy, "build_effective_policy_report", lambda config: expected)

    def fail_if_persisted(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("operational receipt must not write the fleet snapshot sidecar")

    monkeypatch.setattr(oc.fleet_policy, "effective_policy_snapshot", fail_if_persisted)

    assert oc._read_current_policy(None) == expected


def test_operational_receipt_audits_samples_without_writing_model_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _audit_report()
    monkeypatch.setattr(oc.model_audit, "_recorded_samples", lambda source, ticket: [{"execution_id": "e1"}])

    def audit(samples: Any, *, receipt_path: Path | None = None) -> AuditReport:
        assert samples == [{"execution_id": "e1"}]
        assert receipt_path is None
        return report

    monkeypatch.setattr(oc.model_audit, "audit_executions", audit)
    assert oc._read_current_model_audit("USR-471") is report


# --------------------------------------------------------------------------
# reconciled tickets: read-only via core.demands, never reopened or duplicated
# --------------------------------------------------------------------------


def test_reconciled_tickets_are_the_exact_usr471_intake_list() -> None:
    assert oc.RECONCILED_TICKETS == ("USR-452", "USR-454", "USR-464", "USR-466", "USR-456")


def test_reconciled_ticket_notes_reports_status_without_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_service = _FakeDemandsService(
        {
            "USR-452": _ticket("USR-452", DeliveryStatus.COMPLETED, title="Gate timeout"),
            "USR-454": _ticket("USR-454", DeliveryStatus.PLANNED),
        }
    )

    def _fail_if_called(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("reconciliation must never create or mutate a ticket")

    monkeypatch.setattr("core.demands.build_default_demands_service", lambda: fake_service)
    # Any write path reachable from this helper must never be exercised.
    import core.demands as demands_pkg

    monkeypatch.setattr(demands_pkg.DemandsStore, "save_ticket", _fail_if_called, raising=False)
    monkeypatch.setattr(demands_pkg.DemandsStore, "update_status", _fail_if_called, raising=False)

    notes = oc._reconciled_ticket_notes(("USR-452", "USR-454"))

    assert notes["USR-452"] == {"status": "completed", "title": "Gate timeout"}
    assert notes["USR-454"] == {"status": "planned", "title": "titulo"}


def test_reconciled_ticket_notes_reports_missing_ticket_with_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_service = _FakeDemandsService({"USR-999": None})
    monkeypatch.setattr("core.demands.build_default_demands_service", lambda: fake_service)

    notes = oc._reconciled_ticket_notes(("USR-999",))

    assert notes["USR-999"] == {"status": None, "reason": "ticket_not_found"}


def test_reconciled_ticket_notes_degrades_instead_of_raising_on_unreadable_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_service = _FakeDemandsService({"USR-452": RuntimeError("control store unreachable")})
    monkeypatch.setattr("core.demands.build_default_demands_service", lambda: fake_service)

    notes = oc._reconciled_ticket_notes(("USR-452",))

    assert notes["USR-452"]["status"] is None
    assert "unreadable" in notes["USR-452"]["reason"]


def test_reconciled_ticket_notes_degrades_when_the_demands_service_cannot_be_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> Any:
        raise RuntimeError("no control store")

    monkeypatch.setattr("core.demands.build_default_demands_service", _boom)

    notes = oc._reconciled_ticket_notes(("USR-452", "USR-456"))

    assert notes["USR-452"]["status"] is None
    assert "demands_service_unavailable" in notes["USR-452"]["reason"]
    assert notes["USR-456"]["status"] is None


# --------------------------------------------------------------------------
# write_receipt: atomic persistence, same contract as fleet_policy's snapshot
# --------------------------------------------------------------------------


def test_write_receipt_persists_atomically_and_round_trips(tmp_path: Path) -> None:
    receipt = {"schema_version": 1, "generated_at": "now"}
    target = tmp_path / "evidence" / "receipt.json"

    written = oc.write_receipt(receipt, path=target)

    assert written == target
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == receipt
    assert not target.with_suffix(".tmp").exists()


# --------------------------------------------------------------------------
# CLI: --verify builds, persists and prints; nothing else starts a host or queue
# --------------------------------------------------------------------------


def test_cli_verify_builds_writes_and_prints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fixed_receipt = {"schema_version": 1, "generated_at": "now", "hosts": []}
    monkeypatch.setattr(oc, "build_operational_context_receipt", lambda **kwargs: fixed_receipt)
    out_path = tmp_path / "receipt.json"

    rc = oc._cli(["--verify", "--out", str(out_path)])

    assert rc == 0
    assert out_path.is_file()
    assert json.loads(out_path.read_text(encoding="utf-8")) == fixed_receipt
    printed = json.loads(capsys.readouterr().out)
    assert printed == fixed_receipt


def test_cli_without_verify_prints_help_and_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    rc = oc._cli([])

    assert rc == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_cli_verify_forwards_the_ticket_filter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    received: dict[str, Any] = {}

    def fake_build(**kwargs: Any) -> dict[str, Any]:
        received.update(kwargs)
        return {"schema_version": 1}

    monkeypatch.setattr(oc, "build_operational_context_receipt", fake_build)

    oc._cli(["--verify", "--ticket", "USR-471", "--out", str(tmp_path / "r.json")])

    assert received == {"audit_ticket_id": "USR-471"}
