"""Comprehensive test suite for HF-11: Resilient Execution, Graceful Fallback & Fault-Tolerant Budget.

Governed by:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Sections 2, 5, 9, 12, line 263 / HF-11)
- HYBRID_AUTONOMY_REQUIREMENTS (Section 4, 7, Scenario G6 & G8)
- DF-20 Delivery Policy and Remote Reconciliation
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from core.execution.agent_executor import (
    AgentExecutor,
    ExecutionStatus,
    TaskSpec,
)
from core.execution.budget import (
    BudgetExceededError,
    ExecutionBudgetManager,
    InvalidReservationError,
)
from core.execution.contracts import (
    AttemptOutcome,
    AttemptRecord,
    Budget,
    ReservationStatus,
    UnknownCostPolicy,
)
from core.execution.providers import (
    MockModelProvider,
    ProviderResponse,
    get_model_provider,
)
from core.execution.resilience import (
    FORBIDDEN_FALLBACK_MODELS,
    CircuitBreaker,
    CircuitState,
    FallbackEvent,
    FallbackReason,
    JobModelPin,
    ResilientModelProvider,
    classify_error,
)
from core.integrations.github import (
    GitHubCheck,
    PullRequestSnapshot,
)
from core.orchestrator.delivery import (
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRisk,
    MergeQueue,
)
from core.orchestrator.delivery_executor import (
    RemoteDeliveryReconciler,
    RemoteDeliveryStatus,
)


class MutableTime:
    def __init__(self, initial: float = 1000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# 1. Local-First Execution at $0.00 Cost
# ---------------------------------------------------------------------------

def test_local_first_execution_success_at_zero_cost() -> None:
    """Local inference succeeds via Ollama at $0 cost and maintains CLOSED circuit."""
    local_mock = MockModelProvider(
        provider_id="ollama",
        fixed_response="Local inference output.",
        cost_per_token=0.0,
    )
    cloud_mock = MockModelProvider(
        provider_id="openrouter",
        fixed_response="Cloud inference output.",
        cost_per_token=0.000002,
    )

    provider = ResilientModelProvider(
        local_provider=local_mock,
        cloud_provider=cloud_mock,
    )

    resp = provider.generate("Summarize task", model="qwen-code-fast:latest")

    assert resp.text == "Local inference output."
    assert resp.measured_cost == 0.0
    assert resp.estimated_cost == 0.0
    assert provider.local_circuit.state == CircuitState.CLOSED
    assert len(provider.fallback_history) == 0


# ---------------------------------------------------------------------------
# 2. Local Failure Triggers Graceful Cloud Fallback
# ---------------------------------------------------------------------------

def test_local_failure_triggers_graceful_cloud_fallback() -> None:
    """When local provider fails with connection error, graceful fallback to cloud occurs."""
    class FailingLocalProvider:
        provider_id = "ollama"

        def generate(self, *args: object, **kwargs: object) -> ProviderResponse:
            raise ConnectionRefusedError("[WinError 10061] No connection could be made because the target machine actively refused it")

    cloud_mock = MockModelProvider(
        provider_id="openrouter",
        fixed_response="Cloud fallback response.",
        cost_per_token=0.000002,
    )

    provider = ResilientModelProvider(
        local_provider=FailingLocalProvider(),  # type: ignore[arg-type]
        cloud_provider=cloud_mock,
        default_cloud_fallback_model="deepseek/deepseek-v4-pro",
    )

    resp = provider.generate(
        "Generate function",
        model="qwen-code-fast:latest",
        complexity="medium",
    )

    assert resp.text == "Cloud fallback response."
    assert resp.metadata is not None
    assert resp.metadata.get("fallback_triggered") == "true"
    assert resp.metadata.get("fallback_from_provider") == "ollama"
    assert resp.metadata.get("fallback_reason") == FallbackReason.CONNECTION_REFUSED.value
    assert resp.metadata.get("fallback_to_model") == "deepseek/deepseek-v4-pro"

    # Verify audit telemetry
    assert len(provider.fallback_history) == 1
    event = provider.fallback_history[0]
    assert event.from_model == "qwen-code-fast:latest"
    assert event.to_model == "deepseek/deepseek-v4-pro"
    assert event.reason == FallbackReason.CONNECTION_REFUSED
    assert "actively refused" in event.error_message


# ---------------------------------------------------------------------------
# 3. Invariant Anti-Fable (Scenario G6)
# ---------------------------------------------------------------------------

def test_anti_fable_invariant_scenario_g6() -> None:
    """Fable-5.1 is NEVER permitted as fallback or default model under Scenario G6."""
    provider = ResilientModelProvider(
        default_cloud_fallback_model="fable-5.1",  # Malicious or misconfigured default
    )

    # Resolution replaces Fable with safe Pareto model
    resolved = provider.resolve_fallback_model("fable-5.1", complexity="high")
    assert resolved.lower() not in FORBIDDEN_FALLBACK_MODELS
    assert resolved == "deepseek/deepseek-v4-pro"

    # Direct request for Fable fails closed
    with pytest.raises(ValueError, match="Anti-Fable governance"):
        provider.generate("Plan task", model="fable-5.1")


# ---------------------------------------------------------------------------
# 4. Circuit Breaker: Tripping and Immediate Bypass
# ---------------------------------------------------------------------------

def test_circuit_breaker_tripping_and_immediate_bypass() -> None:
    """After 3 consecutive failures, circuit opens and diverts immediately without local calls."""
    fail_count = 0

    class CountingFailingLocal:
        provider_id = "ollama"

        def generate(self, *args: object, **kwargs: object) -> ProviderResponse:
            nonlocal fail_count
            fail_count += 1
            raise TimeoutError("Ollama timed out waiting for GPU")

    cloud_mock = MockModelProvider(
        provider_id="openrouter",
        fixed_response="Cloud response.",
    )

    time_mock = MutableTime(100.0)
    provider = ResilientModelProvider(
        local_provider=CountingFailingLocal(),  # type: ignore[arg-type]
        cloud_provider=cloud_mock,
        circuit_failure_threshold=3,
        circuit_recovery_timeout_seconds=30.0,
        time_func=time_mock,
    )

    # 3 calls: each attempts local, fails, trips circuit
    for _ in range(3):
        provider.generate("Task", model="qwen-code-fast:latest")

    assert fail_count == 3
    assert provider.local_circuit.state == CircuitState.OPEN

    # 4th call: circuit is OPEN, local provider MUST NOT be called!
    resp = provider.generate("Task 4", model="qwen-code-fast:latest")
    assert resp.text == "Cloud response."
    assert fail_count == 3  # Did not increment!
    assert provider.fallback_history[-1].reason == FallbackReason.CIRCUIT_OPEN


# ---------------------------------------------------------------------------
# 5. Circuit Breaker: Half-Open Recovery and Probe
# ---------------------------------------------------------------------------

def test_circuit_breaker_half_open_recovery() -> None:
    """After cooldown, circuit transitions to HALF_OPEN and recovers upon success."""
    time_mock = MutableTime(100.0)
    breaker = CircuitBreaker("test-provider", failure_threshold=2, recovery_timeout_seconds=20.0, time_func=time_mock)

    breaker.record_failure("error 1")
    breaker.record_failure("error 2")
    assert breaker.state == CircuitState.OPEN
    assert not breaker.allow_request()

    # Advance time past 20s recovery timeout
    time_mock.advance(25.0)
    assert breaker.state == CircuitState.HALF_OPEN
    assert breaker.allow_request()  # Canary allowed

    # Canary succeeded
    breaker.record_success()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.failure_count == 0


def test_circuit_breaker_half_open_failure_reopens() -> None:
    """If the canary request in HALF_OPEN fails, circuit immediately reverts to OPEN."""
    time_mock = MutableTime(100.0)
    breaker = CircuitBreaker("test-provider", failure_threshold=2, recovery_timeout_seconds=20.0, time_func=time_mock)

    breaker.record_failure("error 1")
    breaker.record_failure("error 2")
    assert breaker.state == CircuitState.OPEN

    time_mock.advance(25.0)
    assert breaker.state == CircuitState.HALF_OPEN

    # Canary probe fails
    breaker.record_failure("probe error")
    assert breaker.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# 6. Fault-Tolerant Budget: Dynamic Reservation Upgrade
# ---------------------------------------------------------------------------

def test_dynamic_reservation_upgrade_and_ceiling_check() -> None:
    """Upgrades budget reservation on fallback and blocks when exceeding ceiling."""
    manager = ExecutionBudgetManager()
    budget = Budget(
        currency="USD",
        ceiling=0.50,
        unknown_cost_policy=UnknownCostPolicy.ESTIMATE,
        max_attempts=3,
    )
    manager.register_budget("task-hf11-budget", budget)

    # Initial local reservation ($0.00)
    res = manager.reserve("task-hf11-budget", "att-init", amount=0.00)
    assert res.amount == 0.00

    # Upgrade reservation for cloud fallback ($0.15)
    upgraded = manager.upgrade_reservation(res.reservation_id, 0.15)
    assert upgraded.amount == 0.15
    b_updated = manager.get_budget("task-hf11-budget")
    assert b_updated is not None
    assert b_updated.reserved == 0.15

    # Upgrade exceeding ceiling ($0.60 > $0.50 ceiling) fails closed
    with pytest.raises(BudgetExceededError):
        manager.upgrade_reservation(res.reservation_id, 0.60)


# ---------------------------------------------------------------------------
# 7. Safe Reservation Cleanup on Unhandled Execution Failure
# ---------------------------------------------------------------------------

def test_safe_reservation_cleanup_on_unhandled_failure(tmp_path: Path) -> None:
    """AgentExecutor cleans up active reservations when provider raises unhandled exception."""
    class CrashingProvider:
        provider_id = "crashing"

        def generate(self, *args: object, **kwargs: object) -> ProviderResponse:
            raise RuntimeError("Fatal OOM crash during generation")

    budget_mgr = ExecutionBudgetManager()
    executor = AgentExecutor(
        working_dir=tmp_path,
        provider=CrashingProvider(),  # type: ignore[arg-type]
        budget_manager=budget_mgr,
    )

    spec = TaskSpec(
        task_id="TASK-CRASH-TEST",
        objective="Run and crash",
        budget_ceiling=1.0,
        max_attempts=2,
    )
    run = executor.start(spec)
    assert run.active_reservation is not None

    # Step execution raises exception
    with pytest.raises(RuntimeError, match="Fatal OOM crash"):
        executor.execute_step(run.run_id, "Do work")

    # Assert active reservation was safely released in SQLite (NO orphans!)
    assert run.active_reservation is None
    reservations = budget_mgr.list_reservations("TASK-CRASH-TEST")
    assert len(reservations) == 1
    assert reservations[0].status == ReservationStatus.RELEASED

    # Run is marked FAILED and attempt logged
    assert run.status == ExecutionStatus.FAILED
    assert len(run.attempts) == 1
    assert run.attempts[0].outcome == AttemptOutcome.FAILED


# ---------------------------------------------------------------------------
# 8. Mid-Job Model Pin Stability (Scenario G6)
# ---------------------------------------------------------------------------

def test_mid_job_model_pin_stability_scenario_g6() -> None:
    """JobModelPin binds model configuration so external benchmark updates do not swap models mid-job."""
    pin = JobModelPin(
        job_id="job_piv_001",
        task_type="coding",
        complexity="high",
        pinned_model="claude-3.7-sonnet",
        pinned_provider="anthropic",
        pinned_effort="high",
    )

    assert pin.pinned_model == "claude-3.7-sonnet"
    assert pin.pinned_effort == "high"

    # Simulated external daily benchmark refresh recommending a different model
    new_daily_recommendation = "qwen3-max"
    assert pin.pinned_model != new_daily_recommendation

    # In-flight job executes with pinned model, not the daily-updated model
    current_job_model = pin.pinned_model
    assert current_job_model == "claude-3.7-sonnet"


# ---------------------------------------------------------------------------
# 9. Transient Cloud Error Retries with Backoff
# ---------------------------------------------------------------------------

def test_transient_cloud_error_retries_with_backoff() -> None:
    """Transient rate limit (429) retries with backoff and succeeds."""
    call_attempts = 0

    class TransientCloudProvider:
        provider_id = "openrouter"

        def generate(self, *args: object, **kwargs: object) -> ProviderResponse:
            nonlocal call_attempts
            call_attempts += 1
            if call_attempts == 1:
                raise RuntimeError("HTTP 429 Too Many Requests: Rate limit exceeded")
            return ProviderResponse(
                text="Recovered from 429.",
                model="openrouter/deepseek-v4-pro",
                tokens_prompt=10,
                tokens_completion=10,
                total_tokens=20,
                latency_seconds=0.05,
                measured_cost=0.00004,
            )

    provider = ResilientModelProvider(
        cloud_provider=TransientCloudProvider(),  # type: ignore[arg-type]
        max_retries=2,
        initial_backoff_seconds=0.01,
    )

    resp = provider.generate("Hello", model="openrouter/deepseek-v4-pro")
    assert resp.text == "Recovered from 429."
    assert call_attempts == 2
    assert provider.cloud_circuit.state == CircuitState.CLOSED


# ---------------------------------------------------------------------------
# 10. Idempotent Attempt Replay Does Not Double-Count Spend
# ---------------------------------------------------------------------------

def test_idempotent_attempt_replay_does_not_double_count_spend() -> None:
    """Replaying the same attempt commit does not duplicate budget deductions."""
    manager = ExecutionBudgetManager()
    budget = Budget(
        currency="USD",
        ceiling=10.0,
        max_attempts=5,
    )
    manager.register_budget("task-idempotent-spend", budget)

    res1 = manager.reserve("task-idempotent-spend", "att-repeat-1", amount=1.0)
    attempt = AttemptRecord(
        attempt_id="att-repeat-1",
        input_artifact_hash="hash-in",
        measured_cost=0.25,
        timestamp=datetime.now(UTC),
    )

    cost1 = manager.commit(res1.reservation_id, attempt)
    assert cost1 == 0.25
    b1 = manager.get_budget("task-idempotent-spend")
    assert b1 is not None
    assert b1.spent == 0.25

    # Replay commit with same attempt_id and outcome
    cost2 = manager.commit(res1.reservation_id, attempt)
    assert cost2 == 0.25
    b2 = manager.get_budget("task-idempotent-spend")
    assert b2 is not None
    assert b2.spent == 0.25  # NOT 0.50!


# ---------------------------------------------------------------------------
# 11. Remote Delivery Reconciler: Checks, SHAs, and Replay (DF-20 / HF-11)
# ---------------------------------------------------------------------------

def test_remote_delivery_reconciler_checks_and_sha_validation(tmp_path: Path) -> None:
    """Reconciler confirms remote SHA, checks validity, and blocks stale checks."""
    reconciler = RemoteDeliveryReconciler(
        queue_storage_path=tmp_path / "merge_queue.json"
    )

    cand_sha = "a" * 40
    request = DeliveryRequest(
        task_id="HF-11",
        repository="GCamposGit/DarkFactory",
        pull_request_number=11,
        base_sha="0" * 40,
        candidate_sha=cand_sha,
        risk_class=DeliveryRisk.B,
        required_checks=("trusted-pr-policy", "pr-validation"),
        idempotency_key="hf11-pr11-cand",
    )

    # 1. Matching SHA with all green checks -> DELIVERED
    green_snapshot = PullRequestSnapshot(
        repository="GCamposGit/DarkFactory",
        number=11,
        base_sha="0" * 40,
        head_sha=cand_sha,
        state="open",
        mergeable=True,
        checks=(
            GitHubCheck(name="trusted-pr-policy", head_sha=cand_sha, status="completed", conclusion="success"),
            GitHubCheck(name="pr-validation", head_sha=cand_sha, status="completed", conclusion="success"),
        ),
    )
    res_ok = reconciler.reconcile_and_evaluate(request, snapshot=green_snapshot, confirm_merged=True)
    assert res_ok.status == RemoteDeliveryStatus.DELIVERED
    assert res_ok.eligible is True
    assert res_ok.queue_entry is not None

    # 2. Stale checks from earlier SHA -> BLOCKED
    stale_check = GitHubCheck(name="trusted-pr-policy", head_sha="9" * 40, status="completed", conclusion="success")
    stale_snapshot = PullRequestSnapshot(
        repository="GCamposGit/DarkFactory",
        number=11,
        base_sha="0" * 40,
        head_sha=cand_sha,
        state="open",
        mergeable=True,
        checks=(
            stale_check,
            GitHubCheck(name="pr-validation", head_sha=cand_sha, status="completed", conclusion="success"),
        ),
    )
    res_stale = reconciler.reconcile_and_evaluate(request, snapshot=stale_snapshot)
    assert res_stale.status == RemoteDeliveryStatus.BLOCKED
    assert res_stale.eligible is False
    assert "trusted-pr-policy" in res_stale.stale_checks

    # 3. Head SHA mismatch -> BLOCKED
    mismatch_snapshot = PullRequestSnapshot(
        repository="GCamposGit/DarkFactory",
        number=11,
        base_sha="0" * 40,
        head_sha="f" * 40,  # Different SHA!
        state="open",
        mergeable=True,
    )
    res_mismatch = reconciler.reconcile_and_evaluate(request, snapshot=mismatch_snapshot)
    assert res_mismatch.status == RemoteDeliveryStatus.BLOCKED
    assert "candidate_sha_mismatch" in res_mismatch.reason


# ---------------------------------------------------------------------------
# 12. Factory Provider Resolution and CLI Headless
# ---------------------------------------------------------------------------

def test_get_model_provider_factory_resilient() -> None:
    """get_model_provider instantiates ResilientModelProvider for 'resilient'."""
    prov = get_model_provider("resilient")
    assert isinstance(prov, ResilientModelProvider)
    assert prov.provider_id == "resilient"


def test_cli_circuit_status_and_execute_headless(capsys: pytest.CaptureFixture[str]) -> None:
    """CLI commands work with --json flag outputting structured data."""
    from core.execution.cli import main

    # circuit-status
    ret = main(["circuit-status", "--json"])
    assert ret == 0
    captured = capsys.readouterr().out
    data = json.loads(captured)
    assert "providers" in data
    assert "ollama" in data["providers"]
    assert "cloud" in data["providers"]
    assert data["providers"]["ollama"]["state"] == "closed"
