from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from core.models.audit import (
    ExecutionSample,
    audit_executions,
    audit_recorded,
    main as audit_main,
    receipt_path,
    record_execution_sample,
    reported_context_tokens,
)


def _sample(**updates):
    sample = {
        "execution_id": "exec-1",
        "ticket_id": "USR-471",
        "stage": "development",
        "harness": "codex",
        "reported_model": "development.codex",
        "complexity": "medium",
        "quota_remaining_percent": 26.0,
        "latency_ms": 1200,
        "timeout_sec": 30,
        "timed_out": False,
        "cost_usd": 0,
        "spent_usd": 0,
        "reported_context_tokens": 40,
        "context_sha256": "a" * 64,
        "context_bytes": 12,
        "reported_effort": "high",
        "expected_model": "development.codex",
        "expected_effort": "high",
        "candidate_digest": "b" * 64,
        "schema_valid": True,
        "real_delivery": True,
    }
    sample.update(updates)
    harness = sample["harness"]
    stage = sample["stage"]
    complexity = sample.get("complexity") or "medium"
    sample.setdefault("provider", {
        "claude": "anthropic", "codex": "openai", "grok": "xai",
        "antigravity": "google", "openrouter": "openrouter",
    }[harness])
    sample.setdefault("model_source", "provider_reported")
    sample.setdefault("started_at", "2026-11-16T11:59:58.800Z")
    sample.setdefault("finished_at", "2026-11-16T12:00:00Z")
    if "execution_parameters" not in updates:
        if stage == "planning" and harness == "codex":
            requested_model = "planning.codex"
        elif stage == "review" and harness == "openrouter":
            requested_model = "distill.openrouter"
        elif stage == "development" and harness == "codex" and complexity in {"high", "critical"}:
            requested_model = "development.high.codex"
        else:
            requested_model = "development.codex"
        sample["execution_parameters"] = {
            "requested_model": requested_model,
            "requested_effort": "high",
            "reasoning": {"effort": "high"},
        }
    return sample


def test_audit_resolves_effective_model_context_and_stage_divergence() -> None:
    report = audit_executions([
        _sample(),
        _sample(
            execution_id="plan-1",
            stage="planning",
            reported_model="planning.codex",
            expected_model="planning.codex",
            complexity="high",
        ),
    ])

    assert report.passed
    assert report.schema_version == 2
    assert report.samples[0]["effective_model"] == "gpt-6-luna"
    assert report.samples[0]["context_tokens"] == 1_050_000
    assert report.samples[0]["candidate_digest"] == "b" * 64
    assert report.samples[0]["expected_model"] == "gpt-6-luna"
    assert report.samples[0]["reported_context_tokens"] == 40
    assert report.samples[0]["route"] == "development.codex"
    assert report.samples[0]["provider"] == "openai"
    assert report.samples[0]["started_at"] == "2026-11-16T11:59:58.800000+00:00"
    assert report.samples[0]["execution_parameters"]["reasoning"] == {"effort": "high"}
    assert report.samples[0]["delivery_result"] == "delivered"
    assert report.configured_models["development.codex"] == "gpt-6-luna"
    assert report.configured_models["planning.codex"] == "gpt-sol"
    assert report.samples[1]["effective_model"] == "gpt-sol"
    assert any("stage_model_divergence:codex" in item for item in report.divergences)
    assert not any(item.startswith("stage_model_divergence:") for item in report.violations)


def test_audit_fails_closed_on_quota_model_schema_and_timeout() -> None:
    report = audit_executions([_sample(
        quota_remaining_percent=15,
        reported_model="planning.codex",
        schema_valid=False,
        latency_ms=31_000,
    )])

    assert not report.passed
    assert any(item.startswith("quota_floor:") for item in report.violations)
    assert any(item.startswith("reported_model_mismatch:") for item in report.violations)
    assert any(item.startswith("invalid_delivery_schema:") for item in report.violations)
    assert any(item.startswith("timeout_exceeded:") for item in report.violations)


def test_audit_checks_cross_family_and_real_delivery_and_writes_receipt(tmp_path: Path) -> None:
    receipt = tmp_path / "audit.json"
    report = audit_executions([_sample(
        execution_id="review-1",
        stage="review",
        harness="openrouter",
        reported_model="distill.openrouter",
        expected_model="distill.openrouter",
        implementing_harness="codex",
        real_delivery=False,
    )], receipt_path=receipt)

    assert not report.passed
    assert any(item.startswith("no_real_delivery:") for item in report.violations)
    assert not any(item.startswith("same_family_review:") for item in report.violations)
    from core.models.registry import resolve
    written = json.loads(receipt.read_text(encoding="utf-8"))
    assert written["samples"][0]["effective_model"] == resolve("distill.openrouter")
    assert written["samples"][0]["latency_ms"] == 1200
    assert written["samples"][0]["schema_valid"] is True
    assert written["samples"][0]["candidate_digest"] == "b" * 64


def test_audit_flags_same_family_reviewer() -> None:
    report = audit_executions([_sample(
        execution_id="review-2",
        stage="review",
        harness="openrouter",
        reported_model="gpt-sol",
        implementing_harness="codex",
    )])

    assert not report.passed
    assert "same_family_review:review-2" in report.violations


def test_record_execution_sample_appends_one_json_line_per_ticket(tmp_path: Path) -> None:
    runs_dir = tmp_path / "model_audit"
    sample = ExecutionSample.model_validate(_sample())
    record_execution_sample(sample, ticket_id="USR-471", runs_dir=runs_dir)
    record_execution_sample(
        sample.model_copy(update={"execution_id": "exec-2"}), ticket_id="USR-471", runs_dir=runs_dir,
    )

    target = runs_dir / "USR-471.jsonl"
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rows = [json.loads(line) for line in lines]
    assert rows[0]["execution_id"] == "exec-1"
    assert rows[1]["execution_id"] == "exec-2"
    # No prompt text, raw token detail or credential field ever reaches the ledger.
    assert set(rows[0]) == set(ExecutionSample.model_fields)


def test_record_execution_sample_requires_ticket_id(tmp_path: Path) -> None:
    sample = ExecutionSample.model_validate(_sample())
    with pytest.raises(ValueError):
        record_execution_sample(sample, ticket_id="  ", runs_dir=tmp_path)


def test_reported_context_tokens_prefers_total_then_sums_prompt_and_completion() -> None:
    assert reported_context_tokens(None) is None
    assert reported_context_tokens({}) is None
    assert reported_context_tokens({"total_tokens": 500}) == 500
    assert reported_context_tokens({"prompt_tokens": 100, "completion_tokens": 50}) == 150
    assert reported_context_tokens({"input_tokens": 80}) == 80


def _write_ledger(runs_dir: Path, ticket_id: str, *samples: dict) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    target = runs_dir / f"{ticket_id}.jsonl"
    with target.open("a", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample) + "\n")


def test_audit_recorded_reads_one_tickets_ledger_and_persists_receipt(tmp_path: Path) -> None:
    runs_dir = tmp_path / "model_audit"
    _write_ledger(runs_dir, "USR-471", _sample(), _sample(execution_id="exec-2"))
    _write_ledger(runs_dir, "USR-999", _sample(execution_id="other-ticket"))
    receipt = tmp_path / "receipt.json"

    report = audit_recorded("USR-471", runs_dir=runs_dir, receipt_target=receipt)

    assert report.passed
    assert [s["execution_id"] for s in report.samples] == ["exec-1", "exec-2"]
    written = json.loads(receipt.read_text(encoding="utf-8"))
    assert written == json.loads(report.model_dump_json())


def test_audit_recorded_without_ticket_reads_every_ledger(tmp_path: Path) -> None:
    runs_dir = tmp_path / "model_audit"
    _write_ledger(runs_dir, "USR-471", _sample())
    _write_ledger(runs_dir, "USR-999", _sample(execution_id="other-ticket"))

    report = audit_recorded(runs_dir=runs_dir, receipt_target=tmp_path / "receipt.json")

    assert {s["execution_id"] for s in report.samples} == {"exec-1", "other-ticket"}


def test_audit_recorded_without_samples_is_visible_but_not_evidence(tmp_path: Path) -> None:
    report = audit_recorded("USR-NOPE", runs_dir=tmp_path / "missing", receipt_target=tmp_path / "receipt.json")

    assert not report.passed
    assert report.samples == []
    assert report.violations == ["no_execution_samples"]


def test_audit_rejects_reported_effort_and_explicit_timeout() -> None:
    report = audit_executions([_sample(reported_effort="low", timed_out=True)])

    assert not report.passed
    assert any(item.startswith("reported_effort_mismatch:") for item in report.violations)
    assert any(item.startswith("timeout_exceeded:") for item in report.violations)


def test_record_execution_sample_refuses_cross_ticket_ledger(tmp_path: Path) -> None:
    sample = ExecutionSample.model_validate(_sample(ticket_id="USR-999"))

    with pytest.raises(ValueError, match="corresponder"):
        record_execution_sample(sample, ticket_id="USR-471", runs_dir=tmp_path)


def test_audit_recorded_recomputes_every_call_instead_of_reading_the_receipt(tmp_path: Path) -> None:
    runs_dir = tmp_path / "model_audit"
    receipt = tmp_path / "receipt.json"
    _write_ledger(runs_dir, "USR-471", _sample())
    first = audit_recorded("USR-471", runs_dir=runs_dir, receipt_target=receipt)
    assert len(first.samples) == 1

    _write_ledger(runs_dir, "USR-471", _sample(execution_id="exec-2"))
    second = audit_recorded("USR-471", runs_dir=runs_dir, receipt_target=receipt)

    assert len(second.samples) == 2
    assert json.loads(receipt.read_text(encoding="utf-8"))["samples"][-1]["execution_id"] == "exec-2"


def test_receipt_path_defaults_under_state_root_runs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("DARKFAC_STATE_ROOT", raising=False)
    assert receipt_path() == tmp_path / "runs" / "model_audit_receipt.json"


def test_cli_from_runs_requires_latest() -> None:
    with pytest.raises(SystemExit) as exc:
        audit_main(["--from-runs"])
    assert exc.value.code == 2


def test_cli_from_runs_latest_prints_recomputed_receipt(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("DARKFAC_STATE_ROOT", raising=False)
    _write_ledger(tmp_path / "runs" / "model_audit", "USR-471", _sample())

    rc = audit_main(["--from-runs", "--ticket", "USR-471", "--latest"])

    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["samples"][0]["execution_id"] == "exec-1"
    assert (tmp_path / "runs" / "model_audit_receipt.json").is_file()


def test_cli_from_runs_latest_exits_1_on_violation(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("DARKFAC_STATE_ROOT", raising=False)
    _write_ledger(
        tmp_path / "runs" / "model_audit", "USR-471",
        _sample(quota_remaining_percent=15),
    )

    rc = audit_main(["--from-runs", "--ticket", "USR-471", "--latest"])

    assert rc == 1


def test_cli_input_receipt_still_works_without_from_runs(tmp_path: Path, capsys) -> None:
    input_path = tmp_path / "samples.json"
    input_path.write_text(json.dumps([_sample()]), encoding="utf-8")
    receipt = tmp_path / "receipt.json"

    rc = audit_main(["--input", str(input_path), "--receipt", str(receipt)])

    assert rc == 0
    assert receipt.is_file()


def _hub_service(tmp_path: Path):
    from hub.backend.service import HubService

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "default_services.json").write_text("[]", encoding="utf-8")
    (data_dir / "default_prompts.json").write_text("[]", encoding="utf-8")
    return HubService(
        data_dir=data_dir,
        usage_dir=tmp_path / "usage",
        roadmap_root=tmp_path,
        state_path=tmp_path / "state.json",
        orchestrator_path=tmp_path / "orchestrator.sqlite3",
        control_db_path=tmp_path / "missing" / "control.db",
        control_database_url="",
    )


def test_hub_get_model_audit_serves_the_same_recomputed_receipt(tmp_path: Path, monkeypatch) -> None:
    """The Hub endpoint (`HubService.get_model_audit`) serves exactly what `audit_recorded`
    computes -- same source, no second dispatcher, not a read of the persisted file."""
    expected = {"schema_version": 1, "audited_at": "now", "passed": True, "samples": [], "divergences": [], "violations": []}

    class _FakeReport:
        def model_dump(self, mode: str = "python") -> dict:
            return expected

    monkeypatch.setattr("core.models.audit.audit_recorded", lambda ticket_id=None: _FakeReport())

    service = _hub_service(tmp_path)
    assert service.get_model_audit() == expected
    assert service.get_model_audit("USR-471") == expected


def test_hub_get_model_audit_degrades_instead_of_raising(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "core.models.audit.audit_recorded",
        lambda ticket_id=None: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    service = _hub_service(tmp_path)
    report = service.get_model_audit()

    assert report["passed"] is False
    assert report["samples"] == []
    assert report["violations"] == ["audit_unavailable"]
    assert "error" in report


def test_hub_get_model_audit_without_samples_is_visible_as_missing_evidence(tmp_path: Path) -> None:
    """End-to-end (no monkeypatch): absent execution evidence is not accepted as a pass."""
    service = _hub_service(tmp_path)
    report = service.get_model_audit("USR-WITH-NO-SAMPLES-RECORDED")

    assert report["passed"] is False
    assert report["samples"] == []
    assert report["violations"] == ["no_execution_samples"]


def test_api_get_model_audit_returns_the_hub_service_receipt() -> None:
    from fastapi.testclient import TestClient

    from hub.backend.api import get_hub_service
    from hub.backend.main import app

    expected = {"schema_version": 1, "audited_at": "now", "passed": True, "samples": [], "divergences": [], "violations": []}

    class _FakeService:
        def get_model_audit(self, ticket_id: Optional[str] = None) -> dict:
            return dict(expected, ticket_id=ticket_id)

    app.dependency_overrides[get_hub_service] = lambda: _FakeService()
    try:
        response = TestClient(app).get("/api/line/model-audit", params={"ticket": "USR-471"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["ticket_id"] == "USR-471"


def test_api_get_model_audit_degrades_to_200_when_the_service_fails() -> None:
    from fastapi.testclient import TestClient

    from hub.backend.api import get_hub_service
    from hub.backend.main import app

    class _BrokenService:
        def get_model_audit(self, ticket_id: Optional[str] = None) -> dict:
            return {
                "schema_version": 1,
                "audited_at": "now",
                "passed": False,
                "samples": [],
                "divergences": [],
                "violations": ["audit_unavailable"],
                "error": "boom",
            }

    app.dependency_overrides[get_hub_service] = lambda: _BrokenService()
    try:
        response = TestClient(app).get("/api/line/model-audit")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["passed"] is False
    assert response.json()["violations"] == ["audit_unavailable"]


def test_audit_rejects_missing_binding_and_still_records_the_row() -> None:
    report = audit_executions([_sample(
        candidate_digest=None,
        context_sha256="",
        context_bytes=None,
        expected_model=None,
        expected_effort=None,
    )])

    assert not report.passed
    assert [item["execution_id"] for item in report.samples] == ["exec-1"]
    assert any(item.startswith("missing_candidate_digest:") for item in report.violations)
    assert any(item.startswith("missing_context_binding:") for item in report.violations)
    assert any(item.startswith("missing_expected_model:") for item in report.violations)
    assert any(item.startswith("missing_expected_effort:") for item in report.violations)


def test_audit_rejects_expected_route_that_does_not_match_configuration() -> None:
    report = audit_executions([_sample(expected_model="planning.codex", expected_effort="low")])

    assert not report.passed
    assert report.samples
    assert any(item.startswith("expected_model_mismatch:") for item in report.violations)
    assert any(item.startswith("expected_effort_mismatch:") for item in report.violations)


def test_audit_binds_provider_timestamps_parameters_and_complexity_route() -> None:
    report = audit_executions([_sample(
        complexity="high",
        reported_model="development.high.codex",
        expected_model="development.high.codex",
    )])

    assert report.passed
    assert report.samples[0]["effective_model"] == "gpt-sol"
    assert report.samples[0]["execution_parameters"]["requested_model"] == "development.high.codex"


def test_high_complexity_codex_review_audits_the_pinned_independent_model() -> None:
    report = audit_executions([_sample(
        stage="review",
        harness="codex",
        complexity="high",
        implementing_harness="claude",
        reported_model="gpt-sol",
        expected_model="gpt-sol",
        execution_parameters={"requested_model": "gpt-sol", "reasoning": {"effort": "high"}},
    )])

    assert report.passed
    assert report.samples[0]["effective_model"] == "gpt-sol"
    assert report.samples[0]["model_source"] == "provider_reported"


def test_run_agent_returns_measured_execution_context(monkeypatch, tmp_path: Path) -> None:
    import core.line.agent_cli as agent_cli
    from core.line.agent_cli import AgentRequest, AgentResult

    class _Ledger:
        def record(self, _event) -> None:
            return None

    monkeypatch.setattr("core.usage.ledger.ModelUsageLedger", lambda _path: _Ledger())
    monkeypatch.setattr(agent_cli.cancellation, "current_token", lambda: None)
    monkeypatch.setitem(
        agent_cli._HARNESS_RUNNERS,
        "codex",
        lambda _req: AgentResult(
            ok=True, text="done", harness="codex", model="gpt-6-luna", effort="high", duration_s=0.01,
        ),
    )

    result = agent_cli.run_agent(AgentRequest(
        prompt="task", cwd=tmp_path, mode="read", harness="codex",
        model="development.codex", effort="high",
    ))

    assert result.provider == "openai"
    assert result.model_source == "explicit_request"
    assert result.started_at is not None and result.finished_at is not None
    assert result.finished_at >= result.started_at
    assert result.execution_parameters == {
        "requested_model": "development.codex",
        "requested_effort": "high",
        "reasoning": {"effort": "high"},
    }


def test_audit_retains_and_rejects_missing_or_divergent_execution_metadata() -> None:
    report = audit_executions([_sample(
        provider="xai",
        model_source=None,
        started_at=None,
        finished_at=None,
        execution_parameters={},
        schema_valid=False,
        real_delivery=False,
        error_kind="timeout",
    )])

    assert not report.passed
    assert report.samples[0]["provider"] == "xai"
    assert report.samples[0]["model_source"] is None
    assert report.samples[0]["delivery_result"] == "no_delivery"
    assert report.samples[0]["error_kind"] == "timeout"
    assert any(item.startswith("provider_mismatch:") for item in report.violations)
    assert any(item.startswith("missing_model_source:") for item in report.violations)
    assert any(item.startswith("missing_execution_timestamps:") for item in report.violations)
    assert any(item.startswith("missing_execution_parameters:") for item in report.violations)


def test_review_benchmark_stores_observed_latency_without_fixed_gates(tmp_path: Path) -> None:
    from core.models.review_benchmark import (
        ReviewObservation,
        benchmark_has_successful_delivery,
        historical_intake_observations,
        observation_succeeded,
        record_review_benchmark,
    )

    historical = historical_intake_observations()
    grok_slow = ReviewObservation(
        label="grok",
        harness="grok",
        model="review.grok",
        latency_ms=700_000,
        schema_valid=True,
        real_delivery=True,
        delivery_nonempty=True,
        reviewer_family="xai",
        implementer_family="anthropic",
        source="measured",
    )
    sonnet_slow = ReviewObservation(
        label="sonnet",
        harness="openrouter",
        model="sonnet",
        latency_ms=60_000,
        schema_valid=True,
        real_delivery=True,
        delivery_nonempty=True,
        reviewer_family="anthropic",
        implementer_family="openai",
        source="measured",
    )
    sonnet_empty = ReviewObservation(
        label="sonnet",
        harness="openrouter",
        model="sonnet",
        latency_ms=11_000,
        schema_valid=True,
        real_delivery=True,
        delivery_nonempty=False,
        reviewer_family="anthropic",
        implementer_family="openai",
        source="measured",
    )

    assert not observation_succeeded(historical[0])
    assert observation_succeeded(historical[1])
    assert observation_succeeded(grok_slow)
    assert observation_succeeded(sonnet_slow)
    assert not observation_succeeded(sonnet_empty)
    assert not benchmark_has_successful_delivery({})
    assert not benchmark_has_successful_delivery({"observations": []})

    payload = record_review_benchmark(
        [*historical, grok_slow, sonnet_slow, sonnet_empty],
        tmp_path / "review_benchmark.json",
    )

    assert payload["latency_gates"] == []
    assert benchmark_has_successful_delivery(payload)
    assert not (tmp_path / "review_benchmark.tmp").exists()


def test_missing_review_family_keeps_measured_fields_and_is_not_proven(tmp_path: Path, monkeypatch) -> None:
    from core.line.recovery_receipt import record_review_execution

    stored: dict[str, str] = {}

    def _write(_ws: object, name: str, text: str) -> None:
        stored[name] = text

    monkeypatch.setattr("core.line.workspace.context_dir", lambda _ws: tmp_path)
    monkeypatch.setattr("core.line.workspace.write_context", _write)

    record_review_execution(
        object(),
        "digest",
        "not-a-harness",
        "also-unknown",
        "mystery-model",
        ticket_id="USR-471",
        latency_ms=1250,
        schema_valid=True,
        real_delivery=True,
        delivery_nonempty=True,
        context_sha256="ab",
        context_bytes=4,
    )

    payload = json.loads(stored["review_execution.json"])
    assert payload["latency_ms"] == 1250
    assert payload["schema_valid"] is True
    assert payload["delivery_nonempty"] is True
    assert payload["family_evidence_missing"] is True
    assert payload["proven"] is False
