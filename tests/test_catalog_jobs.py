"""Comprehensive deterministic test suite for HF-07-03: Catalog Refresh & Model Qualification.

Governed by Universal Engineering Standards (AGENTS.md), HF-07-03, and CONTRACTS.md.
Validates:
1. 24h timer persistence and boot catchup.
2. Idempotent timer: multiple triggers within 24h window run refresh only once.
3. Active jobs retain pinned model version; new jobs get promoted catalog (run-level model pinning).
4. Counter-proof: missing price is NOT treated as 0.0 (preço ausente não zero).
5. Counter-proof: incompatible reasoning effort is stripped/not sent (effort incompatível não enviado).
6. Failure maintains last valid catalog with age and warning metadata.
7. StageHandler execution conforming to CONTRACTS.md.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest

from core.benchmarks.qualification import (
    ALLOWED_REASONING_EFFORTS,
    MissingPriceError,
    ModelCandidate,
    ModelQualificationRecord,
    QualificationEngine,
)
from core.workflow.catalog_jobs import (
    CatalogRefreshHandler,
    CatalogRefreshState,
)
from core.workflow.control_contracts import (
    Claim,
    JobKey,
    StageContext,
    StageResult,
)


# ==============================================================================
# Fixtures and Sample Candidates
# ==============================================================================


@pytest.fixture
def local_economy_candidate() -> ModelCandidate:
    """Explicit $0 local Ollama candidate."""
    return ModelCandidate(
        model_id="qwen-code-fast:latest",
        alias="qwen-fast",
        provider_id="ollama",
        family="qwen",
        cost_per_1k_input_usd=0.0,
        cost_per_1k_output_usd=0.0,
        tool_calling_supported=True,
        tool_capabilities=["read_file", "write_file", "run_command"],
        supported_roles=["economy"],
        coding_score=65.0,
        supports_reasoning_effort=False,
    )


@pytest.fixture
def cloud_frontier_candidate() -> ModelCandidate:
    """High architecture candidate with reasoning effort support."""
    return ModelCandidate(
        model_id="anthropic/claude-opus-5",
        alias="claude-opus-5",
        provider_id="anthropic",
        family="claude",
        cost_per_1k_input_usd=0.005,
        cost_per_1k_output_usd=0.025,
        tool_calling_supported=True,
        tool_capabilities=["read_file", "write_file", "run_command"],
        supported_roles=["high_architecture"],
        coding_score=92.0,
        supports_reasoning_effort=True,
        allowed_reasoning_efforts=["minimal", "low", "medium", "high", "max"],
        default_reasoning_effort="high",
    )


@pytest.fixture
def verifier_candidate() -> ModelCandidate:
    """Verifier candidate."""
    return ModelCandidate(
        model_id="gpt-review:latest",
        alias="gpt-review",
        provider_id="ollama",
        family="gpt-oss",
        cost_per_1k_input_usd=0.0,
        cost_per_1k_output_usd=0.0,
        tool_calling_supported=True,
        tool_capabilities=["read_file", "run_command"],
        supported_roles=["verifier", "independent_review"],
        coding_score=78.0,
        supports_reasoning_effort=False,
    )


@pytest.fixture
def standard_candidates(
    local_economy_candidate: ModelCandidate,
    cloud_frontier_candidate: ModelCandidate,
    verifier_candidate: ModelCandidate,
) -> list[ModelCandidate]:
    return [local_economy_candidate, cloud_frontier_candidate, verifier_candidate]


# ==============================================================================
# 1. Invariant Counter-Proofs
# ==============================================================================


def test_counterproof_missing_price_is_not_treated_as_zero() -> None:
    """Counter-proof: Missing price is NEVER treated as 0.0.

    Treating missing price as 0.0 would allow expensive or unknown cloud models
    to corrupt the economy router and pose severe financial risks.
    """
    candidate_no_price = ModelCandidate(
        model_id="cloud-unknown/expensive-llm",
        provider_id="openrouter",
        cost_per_1k_input_usd=None,
        cost_per_1k_output_usd=None,
        tool_calling_supported=True,
        tool_capabilities=["read_file", "write_file", "run_command"],
    )

    # Invariant: has_explicit_pricing must be False
    assert not candidate_no_price.has_explicit_pricing

    # Invariant: Calling get_task_cost must raise MissingPriceError rather than returning 0.0
    with pytest.raises(MissingPriceError) as exc_info:
        candidate_no_price.get_task_cost()
    assert "missing price is NOT zero" in str(exc_info.value)

    # Invariant: Qualification engine must disqualify model from economy role
    engine = QualificationEngine()
    record = engine.qualify_candidate(candidate_no_price)
    assert not record.has_valid_pricing
    assert "economy" not in record.qualified_roles
    assert "economy" in record.disqualified_roles
    assert "missing price is NOT zero" in record.disqualified_roles["economy"]

    # Invariant: Unpriced models cannot be placed on Pareto frontier
    frontier = engine.compute_pareto_frontier([record])
    assert record not in frontier
    assert not record.is_pareto_optimal


def test_counterproof_incompatible_reasoning_effort_is_stripped_or_sanitized(
    local_economy_candidate: ModelCandidate,
    cloud_frontier_candidate: ModelCandidate,
) -> None:
    """Counter-proof: Models that do not support reasoning effort have it stripped.

    Models that do support it have forbidden levels (e.g. xhigh, ultra) sanitized,
    and invalid levels rejected/defaulted.
    """
    # 1. Model that doesn't support reasoning effort: stripped to None
    assert not local_economy_candidate.supports_reasoning_effort
    for effort in ["minimal", "low", "medium", "high", "max", "xhigh", "ultra", "invalid"]:
        result = local_economy_candidate.sanitize_reasoning_effort(effort)
        assert result is None, f"Expected stripped (None) for unsupported model, got {result}"

    # 2. Model that supports reasoning effort
    assert cloud_frontier_candidate.supports_reasoning_effort

    # Valid allowed efforts pass through
    for allowed in ["minimal", "low", "medium", "high", "max"]:
        assert cloud_frontier_candidate.sanitize_reasoning_effort(allowed) == allowed

    # Forbidden levels (xhigh, extra-high, ultra) sanitized to max
    assert cloud_frontier_candidate.sanitize_reasoning_effort("xhigh") == "max"
    assert cloud_frontier_candidate.sanitize_reasoning_effort("extra-high") == "max"
    assert cloud_frontier_candidate.sanitize_reasoning_effort("ultra") == "max"

    # Nonsense or invalid effort defaults to model's default reasoning effort
    assert cloud_frontier_candidate.sanitize_reasoning_effort("extreme-thinking") == "high"
    assert cloud_frontier_candidate.sanitize_reasoning_effort(None) == "high"


def test_tool_calling_evaluation_per_role(local_economy_candidate: ModelCandidate) -> None:
    """Tool-calling evaluation enforces required capabilities per role."""
    engine = QualificationEngine()

    # Model without tool calling support is disqualified from all active roles
    no_tools_cand = local_economy_candidate.model_copy(update={"tool_calling_supported": False})
    record = engine.qualify_candidate(no_tools_cand)
    assert not record.qualified_roles
    for role in ["economy", "high_architecture", "verifier", "independent_review"]:
        assert role in record.disqualified_roles
        assert "lacks structured tool-calling" in record.disqualified_roles[role]

    # Model missing write_file is disqualified from economy & high_architecture, but qualified for verifier
    read_only_cand = local_economy_candidate.model_copy(
        update={"tool_capabilities": ["read_file", "run_command"]}
    )
    ro_record = engine.qualify_candidate(read_only_cand)
    assert "economy" in ro_record.disqualified_roles
    assert "high_architecture" in ro_record.disqualified_roles
    assert "verifier" in ro_record.qualified_roles
    assert "independent_review" in ro_record.qualified_roles


def test_pareto_frontier_calculation(
    local_economy_candidate: ModelCandidate,
    cloud_frontier_candidate: ModelCandidate,
) -> None:
    """Pareto frontier identifies non-dominated models across cost and score."""
    engine = QualificationEngine()

    # Intermediate mid model
    mid_candidate = ModelCandidate(
        model_id="deepseek/deepseek-v4.1-flash",
        provider_id="openrouter",
        cost_per_1k_input_usd=0.00015,
        cost_per_1k_output_usd=0.0006,
        tool_calling_supported=True,
        tool_capabilities=["read_file", "write_file", "run_command"],
        supported_roles=["economy", "verifier"],
        coding_score=85.0,
    )

    # Inferior dominated model (higher cost, lower score than mid_candidate)
    dominated_candidate = ModelCandidate(
        model_id="legacy/overpriced-slow-model",
        provider_id="legacy",
        cost_per_1k_input_usd=0.001,
        cost_per_1k_output_usd=0.004,
        tool_calling_supported=True,
        tool_capabilities=["read_file", "write_file", "run_command"],
        supported_roles=["economy"],
        coding_score=70.0,
    )

    candidates = [
        local_economy_candidate,  # cost: 0.0, score: 65.0 (Pareto optimal)
        mid_candidate,            # cost: low, score: 85.0 (Pareto optimal)
        cloud_frontier_candidate, # cost: higher, score: 92.0 (Pareto optimal)
        dominated_candidate,      # cost: > mid, score: < mid (DOMINATED)
    ]

    records = engine.qualify_all(candidates)
    frontier = engine.compute_pareto_frontier(records)

    frontier_ids = [r.model_id for r in frontier]
    assert "qwen-code-fast:latest" in frontier_ids
    assert "deepseek/deepseek-v4.1-flash" in frontier_ids
    assert "anthropic/claude-opus-5" in frontier_ids
    assert "legacy/overpriced-slow-model" not in frontier_ids


# ==============================================================================
# 2. 24h Timer Persistence and Boot Catchup Tests
# ==============================================================================


def test_boot_catchup_triggered_when_uninitialized(
    tmp_path: Path,
    standard_candidates: list[ModelCandidate],
) -> None:
    """When no previous refresh state exists, boot catchup triggers refresh."""
    state_file = tmp_path / "state.json"
    catalog_file = tmp_path / "catalog.json"

    handler = CatalogRefreshHandler(
        state_path=state_file,
        catalog_path=catalog_file,
        candidate_provider=lambda: standard_candidates,
    )

    # Initial state: no previous refresh
    assert handler.check_boot_catchup()
    assert handler.is_refresh_due()

    # Execute refresh
    now = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)
    result_cat = handler.refresh_catalog(now=now)
    assert result_cat is not None
    assert state_file.is_file()
    assert catalog_file.is_file()

    # State recorded
    assert handler.state.last_status == "success"
    assert handler.state.last_refresh_timestamp == now.isoformat()
    assert handler.state.refresh_count == 1
    assert not handler.check_boot_catchup(now=now + timedelta(hours=1))


def test_boot_catchup_when_state_older_than_24h(
    tmp_path: Path,
    standard_candidates: list[ModelCandidate],
) -> None:
    """If last refresh was >= 24h ago, boot catchup triggers refresh."""
    state_file = tmp_path / "state.json"
    catalog_file = tmp_path / "catalog.json"

    t0 = datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC)
    initial_state = CatalogRefreshState(
        last_refresh_timestamp=t0.isoformat(),
        catalog_version="v-old",
        last_status="success",
        refresh_count=1,
    )
    state_file.write_text(json.dumps(initial_state.model_dump()))

    handler = CatalogRefreshHandler(
        state_path=state_file,
        catalog_path=catalog_file,
        candidate_provider=lambda: standard_candidates,
    )

    # 26 hours later
    t1 = t0 + timedelta(hours=26)
    assert handler.check_boot_catchup(now=t1)
    assert handler.is_refresh_due(now=t1)

    # Refresh executes
    handler.refresh_catalog(now=t1)
    assert handler.state.refresh_count == 2
    assert handler.state.last_refresh_timestamp == t1.isoformat()


# ==============================================================================
# 3. Idempotency Tests (Timer lost or delayed triggers only once in 24h)
# ==============================================================================


def test_idempotent_timer_runs_refresh_only_once_in_24h_window(
    tmp_path: Path,
    standard_candidates: list[ModelCandidate],
) -> None:
    """Multiple triggers within 24h execute refresh exactly once unless forced."""
    state_file = tmp_path / "state.json"
    catalog_file = tmp_path / "catalog.json"

    refresh_counter = 0

    def candidates_provider() -> list[ModelCandidate]:
        nonlocal refresh_counter
        refresh_counter += 1
        return standard_candidates

    handler = CatalogRefreshHandler(
        state_path=state_file,
        catalog_path=catalog_file,
        candidate_provider=candidates_provider,
    )

    t0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=UTC)

    # 1. First trigger: executes refresh
    cat1 = handler.refresh_catalog(now=t0)
    assert refresh_counter == 1
    assert handler.state.refresh_count == 1

    # 2. Second trigger 10 minutes later: skipped (idempotent)
    cat2 = handler.refresh_catalog(now=t0 + timedelta(minutes=10))
    assert refresh_counter == 1
    assert handler.state.refresh_count == 1
    assert cat2 == cat1

    # 3. Third trigger 23 hours later: still skipped
    cat3 = handler.refresh_catalog(now=t0 + timedelta(hours=23))
    assert refresh_counter == 1
    assert handler.state.refresh_count == 1

    # 4. Forced trigger: runs even within 24h
    cat4 = handler.refresh_catalog(now=t0 + timedelta(hours=23), force=True)
    assert refresh_counter == 2
    assert handler.state.refresh_count == 2


# ==============================================================================
# 4. Run-Level Model Pinning Tests (Promotion ONLY to new jobs)
# ==============================================================================


def test_active_jobs_retain_pinned_catalog_new_jobs_get_promoted(
    tmp_path: Path,
    local_economy_candidate: ModelCandidate,
    cloud_frontier_candidate: ModelCandidate,
    verifier_candidate: ModelCandidate,
) -> None:
    """Active jobs retain their pinned model catalog; only new jobs receive promoted catalog."""
    state_file = tmp_path / "state.json"
    catalog_file = tmp_path / "catalog.json"

    # Candidate set 1 (Version 1)
    cand_set_1 = [local_economy_candidate, cloud_frontier_candidate, verifier_candidate]

    handler = CatalogRefreshHandler(
        state_path=state_file,
        catalog_path=catalog_file,
    )

    t0 = datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)
    handler.refresh_catalog(now=t0, candidates=cand_set_1, force=True)
    v1 = handler.active_version

    # Job Alpha starts under Version 1
    alpha_cat = handler.get_catalog_for_run("run-alpha")
    alpha_ver = handler.get_version_for_run("run-alpha")
    assert alpha_ver == v1
    assert "anthropic/claude-opus-5" in alpha_cat["roles_mapping"]["high_architecture"]["allowed_models"]

    # Now promote Catalog Version 2 with a newly qualified high_architecture model
    new_frontier_model = ModelCandidate(
        model_id="deepseek/deepseek-v4-pro",
        alias="deepseek-v4-pro",
        provider_id="openrouter",
        family="deepseek",
        cost_per_1k_input_usd=0.00045,
        cost_per_1k_output_usd=0.0018,
        tool_calling_supported=True,
        tool_capabilities=["read_file", "write_file", "run_command"],
        supported_roles=["high_architecture"],
        coding_score=95.0,
    )
    cand_set_2 = [local_economy_candidate, new_frontier_model, verifier_candidate]

    t1 = t0 + timedelta(hours=25)
    handler.refresh_catalog(now=t1, candidates=cand_set_2)
    v2 = handler.active_version
    assert v2 != v1

    # INVARIANT: Active Job Alpha MUST retain pinned Catalog Version 1!
    alpha_cat_rechecked = handler.get_catalog_for_run("run-alpha")
    alpha_ver_rechecked = handler.get_version_for_run("run-alpha")
    assert alpha_ver_rechecked == v1
    assert "deepseek/deepseek-v4-pro" not in alpha_cat_rechecked["roles_mapping"]["high_architecture"]["allowed_models"]

    # INVARIANT: New Job Beta receives newly promoted Catalog Version 2!
    beta_cat = handler.get_catalog_for_run("run-beta")
    beta_ver = handler.get_version_for_run("run-beta")
    assert beta_ver == v2
    assert "deepseek/deepseek-v4-pro" in beta_cat["roles_mapping"]["high_architecture"]["allowed_models"]


# ==============================================================================
# 5. Fallback and Rollback Tests (Failure maintains last valid catalog with age)
# ==============================================================================


def test_failure_maintains_last_valid_catalog_with_age_and_warning(
    tmp_path: Path,
    standard_candidates: list[ModelCandidate],
) -> None:
    """If refresh fails (e.g. network/provider error, invalid data), keep last valid catalog."""
    state_file = tmp_path / "state.json"
    catalog_file = tmp_path / "catalog.json"

    handler = CatalogRefreshHandler(
        state_path=state_file,
        catalog_path=catalog_file,
    )

    t0 = datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)
    valid_cat = handler.refresh_catalog(now=t0, candidates=standard_candidates, force=True)
    v_valid = handler.active_version
    assert handler.state.last_status == "success"

    # 12 hours later, a refresh fails due to an empty candidate list or error
    t1 = t0 + timedelta(hours=12)

    def failing_provider() -> list[ModelCandidate]:
        raise ConnectionError("Provider API unreachable (network partition)")

    handler.candidate_provider = failing_provider
    cat_after_failure = handler.refresh_catalog(now=t1, force=True)

    # Invariant: Active catalog is maintained (retained last valid)
    assert cat_after_failure == valid_cat
    assert handler.active_version == v_valid

    # Invariant: State tracks fallback status, age, and warning
    assert handler.state.last_status == "fallback_retained"
    assert handler.state.age_hours_at_last_attempt == 12.0
    assert handler.state.warning is not None
    assert "retained last valid catalog" in handler.state.warning
    assert "Provider API unreachable" in handler.state.warning


# ==============================================================================
# 6. StageHandler handle() Execution Tests
# ==============================================================================


def test_stage_handler_handle_conforms_to_contracts(
    tmp_path: Path,
    standard_candidates: list[ModelCandidate],
) -> None:
    """StageHandler.handle() produces valid StageResult with outputs conforming to CONTRACTS.md."""
    state_file = tmp_path / "state.json"
    catalog_file = tmp_path / "catalog.json"

    handler = CatalogRefreshHandler(
        state_path=state_file,
        catalog_path=catalog_file,
        candidate_provider=lambda: standard_candidates,
    )

    claim = Claim(
        job_key=JobKey(
            run_id="run-catalog-001",
            ticket_id="HF-07-03",
            plan_version="1.0",
            stage="catalog_refresh",
            iteration=0,
        ),
        lease_id="lease-001",
        owner="worker-alpha",
        fencing_token=1,
        expires_at=(datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
    )

    context = StageContext(
        claim=claim,
        plan_ref="plan-hf07-03",
        plan_digest="abc123digest",
        config_version="v1",
        environment_ref="local-env",
        identity="worker-alpha:economy",
        route_ref="ollama:qwen-code-fast:latest",
        memory_version="mem-v1",
    )

    result = handler.handle(context)

    assert isinstance(result, StageResult)
    assert result.outcome == "success"
    assert len(result.output_refs) > 0
    assert str(catalog_file.resolve()) in result.output_refs
    assert len(result.evidence_refs) > 0
    assert str(state_file.resolve()) in result.evidence_refs
    assert result.actual_cost == 0.0


def test_run_level_pinning_survives_restart(
    tmp_path: Path,
    local_economy_candidate: ModelCandidate,
    cloud_frontier_candidate: ModelCandidate,
    verifier_candidate: ModelCandidate,
) -> None:
    """A restarted handler must not silently move an active run to a new catalog."""
    state_file = tmp_path / "state.json"
    catalog_file = tmp_path / "catalog.json"
    first_candidates = [local_economy_candidate, cloud_frontier_candidate, verifier_candidate]

    handler = CatalogRefreshHandler(state_path=state_file, catalog_path=catalog_file)
    t0 = datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)
    handler.refresh_catalog(now=t0, candidates=first_candidates, force=True)
    old_catalog = handler.get_catalog_for_run("run-persisted")
    old_version = handler.get_version_for_run("run-persisted")

    replacement = ModelCandidate(
        model_id="deepseek/deepseek-v4-pro",
        alias="deepseek-v4-pro",
        provider_id="openrouter",
        family="deepseek",
        cost_per_1k_input_usd=0.00045,
        cost_per_1k_output_usd=0.0018,
        tool_calling_supported=True,
        tool_capabilities=["read_file", "write_file", "run_command"],
        supported_roles=["high_architecture"],
        coding_score=95.0,
    )
    handler.refresh_catalog(
        now=t0 + timedelta(hours=25),
        candidates=[local_economy_candidate, replacement, verifier_candidate],
    )
    new_version = handler.active_version

    restarted = CatalogRefreshHandler(state_path=state_file, catalog_path=catalog_file)
    assert restarted.get_version_for_run("run-persisted") == old_version
    assert restarted.get_catalog_for_run("run-persisted") == old_catalog
    assert "deepseek/deepseek-v4-pro" not in restarted.get_catalog_for_run(
        "run-persisted"
    )["roles_mapping"]["high_architecture"]["allowed_models"]
    assert restarted.get_version_for_run("run-new") == new_version


def test_generated_catalog_does_not_fabricate_price_or_send_effort() -> None:
    """Unknown pricing stays unknown and unsupported effort is omitted entirely."""
    candidate = ModelCandidate(
        model_id="cloud/unpriced-architecture",
        alias="unpriced-architecture",
        provider_id="cloud",
        family="unknown",
        cost_per_1k_input_usd=None,
        cost_per_1k_output_usd=None,
        tool_calling_supported=True,
        tool_capabilities=["read_file", "write_file", "run_command"],
        supported_roles=["high_architecture"],
        coding_score=90.0,
        supports_reasoning_effort=False,
        default_reasoning_effort="xhigh",
        allowed_reasoning_efforts=["xhigh"],
    )

    catalog = QualificationEngine().generate_catalog(
        [QualificationEngine().qualify_candidate(candidate)]
    )
    model = catalog["providers"]["provider_cloud"]["models"][0]

    assert model["cost_per_1k_input_usd"] is None
    assert model["cost_per_1k_output_usd"] is None
    assert model["has_valid_pricing"] is False
    assert "default_reasoning_effort" not in model
    assert "allowed_reasoning_efforts" not in model
    assert "max_reasoning_effort" not in model

    supported_candidate = candidate.model_copy(
        update={
            "model_id": "cloud/supported-architecture",
            "supports_reasoning_effort": True,
            "default_reasoning_effort": "xhigh",
            "allowed_reasoning_efforts": ["xhigh"],
        }
    )
    supported_record = QualificationEngine().qualify_candidate(supported_candidate)
    supported_model = QualificationEngine().generate_catalog([supported_record])["providers"][
        "provider_cloud"
    ]["models"][0]
    assert supported_model["default_reasoning_effort"] == "max"
    assert supported_model["allowed_reasoning_efforts"] == ["max"]
    assert supported_model["max_reasoning_effort"] == "max"


def test_generated_catalog_preserves_binding_metadata() -> None:
    """Refreshes replace qualification data but retain binding policy metadata."""
    base_catalog = {
        "schema_version": "1.0",
        "policy_ref": "hf-07-01",
        "providers": {
            "provider_ollama": {
                "provider_id": "ollama",
                "provider_name": "Local Ollama",
                "models": [],
            }
        },
        "roles_mapping": {
            "economy": {"binding_scope": "local-only"},
            "high_architecture": {"binding_scope": "qualified-only"},
        },
    }
    candidate = ModelCandidate(
        model_id="qwen-code-fast:latest",
        provider_id="ollama",
        family="qwen",
        cost_per_1k_input_usd=0.0,
        cost_per_1k_output_usd=0.0,
        tool_capabilities=["read_file", "write_file", "run_command"],
        supported_roles=["economy", "high_architecture"],
        coding_score=80.0,
    )

    engine = QualificationEngine()
    catalog = engine.generate_catalog(
        engine.qualify_all([candidate]),
        base_catalog=base_catalog,
    )

    assert catalog["schema_version"] == "1.0"
    assert catalog["policy_ref"] == "hf-07-01"
    assert catalog["roles_mapping"]["economy"]["binding_scope"] == "local-only"
    assert catalog["providers"]["provider_ollama"]["provider_name"] == "Local Ollama"


def test_failure_persists_degradation_timestamp(
    tmp_path: Path,
    standard_candidates: list[ModelCandidate],
) -> None:
    """A retained catalog exposes when the failed refresh was observed."""
    state_file = tmp_path / "state.json"
    catalog_file = tmp_path / "catalog.json"
    handler = CatalogRefreshHandler(state_path=state_file, catalog_path=catalog_file)
    t0 = datetime(2026, 9, 18, 10, 0, 0, tzinfo=UTC)
    handler.refresh_catalog(now=t0, candidates=standard_candidates, force=True)

    t1 = t0 + timedelta(hours=12)
    handler.candidate_provider = lambda: (_ for _ in ()).throw(
        ConnectionError("provider unavailable")
    )
    handler.refresh_catalog(now=t1, candidates=None, force=True)

    assert handler.state.last_attempt_timestamp == t1.isoformat()
    assert handler.state.degraded_at == t1.isoformat()
    restored = CatalogRefreshHandler(state_path=state_file, catalog_path=catalog_file)
    assert restored.state.degraded_at == t1.isoformat()
