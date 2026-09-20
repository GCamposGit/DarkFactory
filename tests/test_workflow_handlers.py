"""Deterministic unit tests for HF-05-04: Workflow Handlers and Successor Materialization.

Governed by:
- docs/handoffs/continuous-autonomy/HF-05-04.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/workflow/control_contracts.py
- core/workflow/control_store.py
"""

from __future__ import annotations

from datetime import UTC, datetime
import pytest

from core.workflow.control_contracts import (
    Claim,
    IntakeCommand,
    InvalidResultError,
    JobKey,
    StageContext,
    StageResult,
)
from core.workflow.control_store import SQLiteControlStore
from core.workflow.handlers import (
    MissingHandlerError,
    StageHandler,
    STANDARD_STAGES,
    build_handlers,
    dispatch_stage,
)
from core.workflow.successors import (
    LEARNING_STAGES,
    materialize_result,
)


NOW = datetime(2026, 9, 18, 21, 0, 0, tzinfo=UTC)


def _make_context(stage: str = "grill", iteration: int = 0) -> StageContext:
    jk = JobKey(
        run_id="run-hf05-01",
        ticket_id="HF-05-04",
        plan_version="1.0",
        stage=stage,
        iteration=iteration,
    )
    claim = Claim(
        job_key=jk,
        lease_id="lease-test-1",
        owner="worker-test",
        fencing_token=1,
        expires_at="2026-09-18T22:00:00+00:00",
    )
    return StageContext(
        claim=claim,
        plan_ref="plan-1",
        plan_digest="sha256:" + "a" * 64,
        config_version="1.0",
        environment_ref="env-local",
        identity="test-runner",
        route_ref="route-1",
        memory_version="1.0",
        input_refs=["ref://input-1"],
    )


# ---------------------------------------------------------------------------
# Test 1: Handler Build and Standard Stages Registration
# ---------------------------------------------------------------------------


def test_build_handlers_registers_all_standard_stages() -> None:
    handlers = build_handlers()
    for stage in STANDARD_STAGES:
        key = (stage, "v1")
        assert key in handlers
        assert isinstance(handlers[key], StageHandler)


def test_handler_dispatch_default_execution() -> None:
    handlers = build_handlers()
    ctx = _make_context(stage="planning")
    result = handlers[("planning", "v1")].handle(ctx)
    assert result.outcome == "failed"
    assert result.cause_code == "missing_stage_service"
    assert not result.output_refs


def test_handler_dispatch_custom_service() -> None:
    calls = []

    def mock_service(context: StageContext) -> StageResult:
        calls.append(context.claim.job_key.stage)
        return StageResult(
            outcome="success",
            output_refs=["ref://custom/development/output"],
            evidence_refs=["ref://custom/development/evidence"],
        )

    handlers = build_handlers(services={"development": mock_service})
    ctx = _make_context(stage="development")
    result = handlers[("development", "v1")].handle(ctx)
    assert result.outcome == "success"
    assert result.output_refs == ["ref://custom/development/output"]
    assert calls == ["development"]


# ---------------------------------------------------------------------------
# Test 2: Missing Handler Fails Closed
# ---------------------------------------------------------------------------


def test_missing_handler_raises_error_on_direct_lookup() -> None:
    handlers = build_handlers()
    with pytest.raises(MissingHandlerError):
        _ = handlers[("non_existent_stage", "v1")]


def test_missing_handler_dispatch_fails_closed() -> None:
    handlers = build_handlers()
    ctx = _make_context(stage="non_existent_stage")
    res = dispatch_stage(handlers, ctx)
    assert res.outcome == "failed"
    assert res.cause_code == "missing_handler"


# ---------------------------------------------------------------------------
# Test 3: Success with Empty Output Refs Fails Closed
# ---------------------------------------------------------------------------


def test_success_with_empty_output_refs_fails_closed() -> None:
    # 1. Pydantic validation invariant
    with pytest.raises(ValueError):
        StageResult(outcome="success", output_refs=[])

    # 2. Handler wrapper enforcement
    def bad_service(context: StageContext) -> dict:
        return {"outcome": "success", "output_refs": []}

    handlers = build_handlers(services={"grill": bad_service})
    ctx = _make_context(stage="grill")
    with pytest.raises(InvalidResultError):
        handlers[("grill", "v1")].handle(ctx)


# ---------------------------------------------------------------------------
# Test 4: Materialize Result DAG Transitions
# ---------------------------------------------------------------------------


def test_materialize_result_productive_dag_sequence() -> None:
    store = SQLiteControlStore(":memory:")
    jk_base = JobKey(
        run_id="run-dag-1",
        ticket_id="TICKET-DAG",
        plan_version="1.0",
        stage="grill",
        iteration=0,
    )

    transitions = [
        ("grill", "planning"),
        ("planning", "development"),
        ("environment", "development"),
        ("development", "validation"),
        ("validation", "independent_review"),
        ("independent_review", "integration"),
        ("integration", "build_deploy"),
        ("build_deploy", "target_journey"),
    ]

    for current_stage, expected_next in transitions:
        current_key = JobKey(
            run_id=jk_base.run_id,
            ticket_id=jk_base.ticket_id,
            plan_version=jk_base.plan_version,
            stage=current_stage,
            iteration=0,
        )
        res = StageResult(outcome="success", output_refs=[f"ref://{current_stage}/out"])
        successors = materialize_result(current_key, res, store, now=NOW)

        # Non-learning stage produces productive successor AND memory_observation
        productive_successors = [s for s in successors if s.stage != "memory_observation"]
        memory_successors = [s for s in successors if s.stage == "memory_observation"]

        assert len(productive_successors) == 1
        assert productive_successors[0].stage == expected_next
        assert productive_successors[0].run_id == jk_base.run_id
        assert productive_successors[0].ticket_id == jk_base.ticket_id
        assert productive_successors[0].iteration == 0

        assert len(memory_successors) == 1
        assert memory_successors[0].stage == "memory_observation"


def test_materialize_planning_requires_environment() -> None:
    store = SQLiteControlStore(":memory:")
    planning_key = JobKey(
        run_id="run-dag-env",
        ticket_id="TICKET-ENV",
        plan_version="1.0",
        stage="planning",
        iteration=0,
    )
    res = StageResult(
        outcome="success",
        output_refs=["ref://manifest/needs_environment"],
    )
    successors = materialize_result(
        planning_key,
        res,
        store,
        now=NOW,
        manifest_requires_environment=True,
    )

    prod = [s for s in successors if s.stage != "memory_observation"]
    assert len(prod) == 1
    assert prod[0].stage == "environment"


def test_materialize_target_journey_terminal() -> None:
    store = SQLiteControlStore(":memory:")
    tj_key = JobKey(
        run_id="run-dag-tj",
        ticket_id="TICKET-TJ",
        plan_version="1.0",
        stage="target_journey",
        iteration=0,
    )
    res = StageResult(outcome="success", output_refs=["ref://journey/passed"])
    successors = materialize_result(tj_key, res, store, now=NOW)

    # Terminal productive stage has no next productive stage, only memory observation
    prod = [s for s in successors if s.stage != "memory_observation"]
    assert len(prod) == 0

    mem = [s for s in successors if s.stage == "memory_observation"]
    assert len(mem) == 1


def test_materialize_retry_increments_iteration() -> None:
    store = SQLiteControlStore(":memory:")
    dev_key = JobKey(
        run_id="run-retry",
        ticket_id="TICKET-RETRY",
        plan_version="1.0",
        stage="development",
        iteration=1,
    )
    res = StageResult(outcome="retry", cause_code="SYNTAX_ERROR")
    successors = materialize_result(dev_key, res, store, now=NOW)

    assert len(successors) == 1
    assert successors[0].stage == "development"
    assert successors[0].iteration == 2


def test_materialize_replan_enqueues_planning() -> None:
    store = SQLiteControlStore(":memory:")
    val_key = JobKey(
        run_id="run-replan",
        ticket_id="TICKET-REPLAN",
        plan_version="1.0",
        stage="validation",
        iteration=2,
    )
    res = StageResult(outcome="replan", cause_code="ARCH_DEFECT")
    successors = materialize_result(val_key, res, store, now=NOW)

    assert len(successors) == 1
    assert successors[0].stage == "planning"
    assert successors[0].iteration == 0


# ---------------------------------------------------------------------------
# Test 5: Anti-Recursion in Learning Loop
# ---------------------------------------------------------------------------


def test_anti_recursion_memory_observation_does_not_spawn_memory_observation() -> None:
    store = SQLiteControlStore(":memory:")
    mem_key = JobKey(
        run_id="run-mem-test",
        ticket_id="TICKET-MEM",
        plan_version="1.0",
        stage="memory_observation",
        iteration=0,
    )
    res = StageResult(outcome="success", output_refs=["ref://hypothesis/1"])
    successors = materialize_result(mem_key, res, store, now=NOW)

    stages = [s.stage for s in successors]
    assert "memory_observation" not in stages
    assert "learning_eval" in stages


def test_anti_recursion_learning_eval_does_not_spawn_memory_observation() -> None:
    store = SQLiteControlStore(":memory:")
    eval_key = JobKey(
        run_id="run-eval-test",
        ticket_id="TICKET-EVAL",
        plan_version="1.0",
        stage="learning_eval",
        iteration=0,
    )
    res = StageResult(outcome="success", output_refs=["ref://eval/passed"])
    successors = materialize_result(eval_key, res, store, now=NOW)

    stages = [s.stage for s in successors]
    assert "memory_observation" not in stages


# ---------------------------------------------------------------------------
# Test 6: Sibling Isolation (WAITING_HUMAN Does Not Suspend Siblings)
# ---------------------------------------------------------------------------


def test_waiting_human_does_not_suspend_sibling_jobs() -> None:
    store = SQLiteControlStore(":memory:")

    # Prepare intake and get initial grill job
    cmd = IntakeCommand(
        project_id="test-project",
        channel="web",
        external_id="ext-human-test-1",
        mode="autonomous",
        policy_ref="default",
        payload={
            "title": "Feature with Human Confirmation",
            "problem": "Requires human decision at fork",
            "journey": "User path with check",
            "non_goals": "Automatic confirmation",
            "criteria": "Grill completed",
        },
    )
    receipt = store.accept(cmd, now=NOW)
    run_id = receipt.run_id

    # Materialize two sibling jobs in the same run
    sibling_a = JobKey(
        run_id=run_id,
        ticket_id="TICKET-A",
        plan_version="1.0",
        stage="development",
        iteration=0,
    )
    sibling_b = JobKey(
        run_id=run_id,
        ticket_id="TICKET-B",
        plan_version="1.0",
        stage="research",
        iteration=0,
    )

    # Mark initial grill job succeeded so it doesn't interfere
    with store._connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'succeeded' WHERE run_id = ? AND stage = 'grill'",
            (run_id,),
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, required_capabilities, created_at, updated_at)
            VALUES (?, ?, '1.0', 'development', 0, 'running', 'developer', '[]', ?, ?)
            """,
            (run_id, "TICKET-A", NOW.isoformat(), NOW.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, required_capabilities, created_at, updated_at)
            VALUES (?, ?, '1.0', 'research', 0, 'pending', 'researcher', '[]', ?, ?)
            """,
            (run_id, "TICKET-B", NOW.isoformat(), NOW.isoformat()),
        )

    # Sibling A enters WAITING_HUMAN
    res_waiting = StageResult(outcome="waiting_human", cause_code="NEEDS_APPROVAL")
    succs = materialize_result(sibling_a, res_waiting, store, now=NOW)
    assert succs == []

    # Verify Sibling B is untouched, still pending, and claimable by worker
    claim_b = store.claim("researcher", [], now=NOW)
    assert claim_b is not None
    assert claim_b.job_key.ticket_id == "TICKET-B"
    assert claim_b.job_key.stage == "research"


# ---------------------------------------------------------------------------
# Test 7: Idempotency (Duplicate Events Converge Safely)
# ---------------------------------------------------------------------------


def test_idempotency_duplicate_materialize_converges_safely() -> None:
    store = SQLiteControlStore(":memory:")
    job_key = JobKey(
        run_id="run-idem-1",
        ticket_id="TICKET-IDEM",
        plan_version="1.0",
        stage="development",
        iteration=0,
    )
    res = StageResult(
        outcome="success",
        output_refs=["ref://dev/code-sha"],
        evidence_refs=["ref://dev/diff"],
    )

    # First call
    successors_1 = materialize_result(job_key, res, store, now=NOW)

    # Second call (duplicate event)
    successors_2 = materialize_result(job_key, res, store, now=NOW)

    # Third call
    successors_3 = materialize_result(job_key, res, store, now=NOW)

    # Successor lists must be identical
    assert successors_1 == successors_2 == successors_3

    # Check store has not duplicated job records
    with store._connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM jobs WHERE run_id = ? AND stage = 'validation'",
            (job_key.run_id,),
        )
        val_count = cur.fetchone()[0]
        assert val_count == 1

        cur.execute(
            "SELECT COUNT(*) FROM jobs WHERE run_id = ? AND stage = 'memory_observation'",
            (job_key.run_id,),
        )
        mem_count = cur.fetchone()[0]
        assert mem_count == 1


# ---------------------------------------------------------------------------
# Test 8: Additional Boundary and Invariant Tests
# ---------------------------------------------------------------------------


def test_materialize_success_with_empty_output_refs_raises_invalid_result_error() -> None:
    store = SQLiteControlStore(":memory:")
    job_key = JobKey(
        run_id="run-empty-out",
        ticket_id="TICKET-EMPTY",
        plan_version="1.0",
        stage="grill",
        iteration=0,
    )
    # Construct an invalid object bypassing constructor if possible or mocking
    class FakeResult:
        outcome = "success"
        output_refs = []
        evidence_refs = []
        actual_cost = 0.0
        cause_code = None

    with pytest.raises(InvalidResultError):
        materialize_result(job_key, FakeResult(), store, now=NOW)  # type: ignore


def test_anti_recursion_promotion_stage() -> None:
    store = SQLiteControlStore(":memory:")
    promo_key = JobKey(
        run_id="run-promo-test",
        ticket_id="TICKET-PROMO",
        plan_version="1.0",
        stage="promotion",
        iteration=0,
    )
    res = StageResult(outcome="success", output_refs=["ref://promo/receipt"])
    successors = materialize_result(promo_key, res, store, now=NOW)
    stages = [s.stage for s in successors]
    assert "memory_observation" not in stages


def test_materialize_failed_and_cancelled_outcomes() -> None:
    store = SQLiteControlStore(":memory:")
    for outcome in ("failed", "cancelled"):
        jk = JobKey(
            run_id=f"run-{outcome}",
            ticket_id=f"TICKET-{outcome.upper()}",
            plan_version="1.0",
            stage="development",
            iteration=0,
        )
        res = StageResult(outcome=outcome, cause_code=f"TEST_{outcome.upper()}")
        successors = materialize_result(jk, res, store, now=NOW)
        assert successors == []

        with store._connect() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT status, cause_code FROM jobs WHERE run_id = ? AND stage = 'development'",
                (jk.run_id,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row["status"] == outcome
            assert row["cause_code"] == f"TEST_{outcome.upper()}"


def test_handler_registry_get_handler_success_and_missing() -> None:
    registry = build_handlers()
    handler = registry.get_handler("grill", "v1")
    assert isinstance(handler, StageHandler)

    with pytest.raises(MissingHandlerError):
        registry.get_handler("unknown_stage", "v1")


def test_default_stage_handler_invalid_return_type_fails_closed() -> None:
    def invalid_service(context: StageContext) -> int:
        return 42

    registry = build_handlers(services={"grill": invalid_service})
    ctx = _make_context(stage="grill")
    with pytest.raises(InvalidResultError):
        registry[("grill", "v1")].handle(ctx)
