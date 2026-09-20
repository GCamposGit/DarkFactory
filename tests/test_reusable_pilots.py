"""Deterministic test suite for reusable pilots and Jev shadow adapter (HF-23-02)."""

from __future__ import annotations

import hashlib
from typing import Any
import pytest

from core.pilots.contracts import (
    PilotObservation,
    PilotReport,
    PilotSpec,
)
from core.pilots.evaluator import (
    PilotEvaluator,
    compute_mcnemar_p_value,
    compute_paired_delta_ci,
    compute_wilson_interval,
)
from core.pilots.jev_adapter import (
    JevShadowAdapter,
    DEFAULT_JEV_MODEL,
    QUESTIONS_VERSION,
)
from core.workflow.contracts import SecretReference


@pytest.fixture
def base_spec() -> PilotSpec:
    return PilotSpec(
        spec_id="spec-reusable-pilot-v1",
        hypothesis="Semantic pre-classification reduces immediate stage identification error compared to regex baseline.",
        target_population="Eligible non-confidential Dark Factory development demands.",
        unit_of_analysis="demand_version",
        candidate_version="fake-classifier-v1",
        baseline_version="regex-router-v1",
        questions_version="questions-v1",
        labels=["planning", "coding", "review", "testing", "research", "unknown"],
        exclusions=["offline", "contains_secret", "missing_description"],
        oracle_source="independent_reviewer_adjudicated",
        min_sample_size=5,
        sample_size_target=10,
    )


class TestPilotContracts:
    """Validates contract invariants, serialization, and secret hygiene."""

    def test_spec_validation(self, base_spec: PilotSpec) -> None:
        assert base_spec.spec_id == "spec-reusable-pilot-v1"
        assert "planning" in base_spec.labels
        assert base_spec.budget_limit_usd == 10.0

    def test_duplicate_labels_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate labels"):
            PilotSpec(
                spec_id="spec-invalid-labels",
                hypothesis="Testing duplicate labels.",
                target_population="All tasks",
                candidate_version="v1",
                baseline_version="v1",
                questions_version="v1",
                labels=["coding", "coding"],
                oracle_source="reviewer",
            )

    def test_observation_forbids_raw_prompt_leak(self) -> None:
        # Candidate prediction leaking raw prompt key must fail validation
        with pytest.raises(ValueError, match="must not leak raw input"):
            PilotObservation(
                case_id="case-leak",
                demand_hash="a1b2c3d4e5f60718",
                stratum="coding",
                eligible=True,
                candidate_prediction={"choice": "coding", "prompt": "Implement confidential auth feature"},
            )

    def test_observation_forbids_raw_secret_leak(self) -> None:
        with pytest.raises(ValueError, match="cannot contain secret values"):
            PilotObservation(
                case_id="case-secret",
                demand_hash="a1b2c3d4e5f60718",
                stratum="coding",
                eligible=False,
                exclusion_reason="Failed with sk-1234567890abcdef1234567890abcdef",
            )

    def test_ineligible_observation_requires_reason(self) -> None:
        with pytest.raises(ValueError, match="ineligible observations must supply an exclusion_reason"):
            PilotObservation(
                case_id="case-no-reason",
                demand_hash="a1b2c3d4e5f60718",
                stratum="coding",
                eligible=False,
                exclusion_reason=None,
            )


class TestPureStatisticalEvaluator:
    """Verifies that PilotEvaluator is 100% reusable across any candidate, not just Jev."""

    def test_wilson_interval_bounds(self) -> None:
        low, high = compute_wilson_interval(5, 10)
        assert 0.0 <= low < high <= 1.0
        assert 0.23 < low < 0.25
        assert 0.75 < high < 0.77

        # Zero sample
        assert compute_wilson_interval(0, 0) == (0.0, 0.0)

    def test_paired_delta_ci(self) -> None:
        # 10 cases where candidate is correct (0) and baseline wrong (1) -> diff = -1
        diffs = [-1] * 10
        mean_d, ci_l, ci_u = compute_paired_delta_ci(diffs)
        assert mean_d == -1.0
        assert ci_l == -1.0
        assert ci_u == -1.0

        # Mixed diffs
        mixed = [-1, -1, 0, 0, 1]
        mean_d, ci_l, ci_u = compute_paired_delta_ci(mixed)
        assert mean_d == -0.2
        assert ci_l < mean_d < ci_u

    def test_mcnemar_p_value(self) -> None:
        # Large asymmetry (10 improvements vs 0 degradations)
        p = compute_mcnemar_p_value(10, 0)
        assert p is not None and p < 0.05

        # Symmetric (5 vs 5)
        p_sym = compute_mcnemar_p_value(5, 5)
        assert p_sym == 1.0

    def test_evaluator_deduplication_replay_does_not_increase_n(self, base_spec: PilotSpec) -> None:
        evaluator = PilotEvaluator(base_spec)
        obs1 = PilotObservation(
            case_id="case-001",
            demand_hash="1111222233334444",
            stratum="coding",
            eligible=True,
            candidate_prediction={"choice": "coding"},
            baseline_prediction={"choice": "planning"},
            ground_truth="coding",
        )
        # Replaying identical case_id multiple times
        obs_replays = [obs1, obs1, obs1]
        report = evaluator.evaluate(obs_replays)

        assert report.total_cases == 1
        assert report.eligible_cases == 1
        assert any("duplicate replay runs collapsed" in lim for lim in report.limitations)

    def test_evaluator_inconclusive_when_underpowered(self, base_spec: PilotSpec) -> None:
        # min_sample_size is 5; providing only 2 observations
        evaluator = PilotEvaluator(base_spec)
        obs_list = [
            PilotObservation(
                case_id=f"case-00{i}",
                demand_hash=f"hash{i}000000000000",
                stratum="coding",
                eligible=True,
                candidate_prediction={"choice": "coding"},
                baseline_prediction={"choice": "coding"},
                ground_truth="coding",
            )
            for i in range(2)
        ]
        report = evaluator.evaluate(obs_list)
        assert report.verdict == "inconclusive"
        assert "insufficient" in report.verdict_reason.lower()

    def test_evaluator_promising_on_significant_improvement(self, base_spec: PilotSpec) -> None:
        # 12 cases where candidate got 100% correct, baseline missed all
        evaluator = PilotEvaluator(base_spec)
        obs_list = [
            PilotObservation(
                case_id=f"case-perf-{i}",
                demand_hash=f"perfhash{i:04d}00000000",
                stratum="coding" if i % 2 == 0 else "planning",
                eligible=True,
                candidate_prediction={"choice": "coding" if i % 2 == 0 else "planning"},
                baseline_prediction={"choice": "review"},  # wrong
                ground_truth="coding" if i % 2 == 0 else "planning",
                latency_ms=120.0 + i,
                cost_usd=0.0001,
            )
            for i in range(12)
        ]
        report = evaluator.evaluate(obs_list)
        assert report.verdict == "promising"
        assert report.candidate_error_rate == 0.0
        assert report.baseline_error_rate == 1.0
        assert report.paired_delta == -1.0
        assert report.ci_upper < 0.0
        assert "coding" in report.strata_breakdown
        assert report.latency_p50_ms > 0

    def test_evaluator_harmful_on_significant_regression(self, base_spec: PilotSpec) -> None:
        # 10 cases where candidate got everything wrong, baseline got everything right
        evaluator = PilotEvaluator(base_spec)
        obs_list = [
            PilotObservation(
                case_id=f"case-bad-{i}",
                demand_hash=f"badhash{i:04d}00000000",
                stratum="planning",
                eligible=True,
                candidate_prediction={"choice": "coding"},  # wrong
                baseline_prediction={"choice": "planning"},  # right
                ground_truth="planning",
            )
            for i in range(10)
        ]
        report = evaluator.evaluate(obs_list)
        assert report.verdict == "harmful"
        assert report.paired_delta > 0.0
        assert report.ci_lower > 0.0

    def test_evaluator_sentinel_risk_offline_leak(self, base_spec: PilotSpec) -> None:
        evaluator = PilotEvaluator(base_spec)
        # Observation in offline stratum that leaked candidate prediction to cloud
        bad_obs = PilotObservation(
            case_id="case-leak-offline",
            demand_hash="leakhash00000000",
            stratum="offline",
            eligible=True,  # VIOLATION: offline marked eligible
            candidate_prediction={"choice": "coding"},  # VIOLATION: cloud prediction generated
            baseline_prediction={"choice": "coding"},
            ground_truth="coding",
        )
        report = evaluator.evaluate([bad_obs])
        assert report.verdict == "harmful"
        assert len(report.sentinel_violations) > 0
        assert any(v["risk"] == "offline_leak" for v in report.sentinel_violations)

    def test_evaluator_sentinel_risk_executor_mutation(self, base_spec: PilotSpec) -> None:
        evaluator = PilotEvaluator(base_spec)
        obs = PilotObservation(
            case_id="case-mutation",
            demand_hash="mutarthash0000000",
            stratum="coding",
            eligible=True,
            candidate_prediction={"choice": "coding"},
            baseline_prediction={"choice": "coding"},
            ground_truth="coding",
            provenance={"executor_mutated": True},  # VIOLATION: mutated production executor
        )
        report = evaluator.evaluate([obs])
        assert report.verdict == "harmful"
        assert any(v["risk"] == "production_executor_mutation" for v in report.sentinel_violations)


class TestJevShadowAdapter:
    """Validates Jev adapter in shadow mode with privacy fail-closed guarantees."""

    def test_adapter_repr_redacts_keys(self) -> None:
        ref = SecretReference(
            ref_id="my-secret-ref",
            provider="typesafe",
            locator="env:MY_JEV_KEY",
            variable_name="MY_JEV_KEY",
        )
        adapter = JevShadowAdapter(secret_ref=ref)
        rep = repr(adapter)
        assert "my-secret-ref" in rep
        assert "sk-" not in rep

    def test_offline_task_fails_closed_without_network_call(self) -> None:
        called = False

        def dummy_transport(payload: dict[str, Any], key: str) -> dict[str, Any]:
            nonlocal called
            called = True
            return {}

        adapter = JevShadowAdapter(transport=dummy_transport)

        # 1. Explicit offline parameter
        obs1 = adapter.observe_task(
            case_id="case-off-1",
            task_text="Corrigir formatação no terminal Windows",
            offline=True,
            ground_truth="coding",
        )
        assert not called
        assert not obs1.eligible
        assert "offline_task_preserved" in (obs1.exclusion_reason or "")
        assert obs1.candidate_prediction is None
        assert obs1.baseline_prediction is not None
        assert obs1.provenance and obs1.provenance.get("offline_guard") is True

        # 2. Offline keyword detection in task text
        obs2 = adapter.observe_task(
            case_id="case-off-2",
            task_text="Módulo com dados confidenciais de cliente que não pode sair da máquina.",
            offline=False,
            ground_truth="coding",
        )
        assert not called
        assert not obs2.eligible
        assert "confidential" in (obs2.exclusion_reason or "").lower()

    def test_shadow_mode_mock_call_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-test-key")

        transport_invoked = False

        def mock_transport(payload: dict[str, Any], key: str) -> dict[str, Any]:
            nonlocal transport_invoked
            transport_invoked = True
            assert key == "dummy-test-key"
            assert payload["model"] == DEFAULT_JEV_MODEL
            assert "questions" in payload
            return {
                "model": "typesafe/jev-1.13-20260917",
                "answers": {
                    "stage": {
                        "choice": "coding",
                        "confidence": 0.95,
                        "distribution": {"coding": 0.95, "planning": 0.05},
                    },
                    "complexity": {
                        "choice": "low",
                        "confidence": 0.90,
                    },
                    "needs_owner_decision": {"noul": 0.15},
                    "must_stay_local": {"noul": 0.05},
                },
                "usage": {"prompt_tokens": 120, "completion_tokens": 20},
            }

        adapter = JevShadowAdapter(transport=mock_transport)
        obs = adapter.observe_task(
            case_id="case-cloud-1",
            task_text="Implementar correção de UTF-8 no CLI Python com teste unitário.",
            stratum="coding",
            ground_truth="coding",
        )

        assert transport_invoked
        assert obs.eligible
        assert obs.candidate_prediction is not None
        assert obs.candidate_prediction["choice"] == "coding"
        assert obs.candidate_prediction["complexity"]["choice"] == "low"
        assert obs.candidate_prediction["needs_owner_decision_p"] == 0.15
        assert obs.cost_usd is not None and obs.cost_usd > 0.0
        assert obs.effective_version == "typesafe/jev-1.13-20260917"

    def test_shadow_mode_resilient_to_network_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-test-key")

        def failing_transport(payload: dict[str, Any], key: str) -> dict[str, Any]:
            raise TimeoutError("Connection timed out to TypeSafe decisions endpoint")

        adapter = JevShadowAdapter(transport=failing_transport)
        obs = adapter.observe_task(
            case_id="case-timeout",
            task_text="Fazer revisão adversarial independente do patch.",
            stratum="review",
            ground_truth="review",
        )

        assert obs.eligible
        assert obs.candidate_prediction is None
        assert obs.error == "timeout"
        # Baseline must still be computed
        assert obs.baseline_prediction is not None
        assert obs.baseline_prediction["choice"] == "review"

    def test_missing_credentials_fails_gracefully(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

        adapter = JevShadowAdapter()
        obs = adapter.observe_task(
            case_id="case-no-creds",
            task_text="Pesquisar documentação técnica do Pytest.",
            stratum="research",
            ground_truth="research",
        )

        assert not obs.eligible
        assert obs.exclusion_reason == "missing_api_credentials"
        assert obs.candidate_prediction is None
        assert obs.baseline_prediction is not None
        assert obs.baseline_prediction["choice"] == "research"

    def test_shadow_mode_http_429_rate_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-test-key")

        import urllib.error
        import io

        def rate_limited_transport(payload: dict[str, Any], key: str) -> dict[str, Any]:
            fp = io.BytesIO(b'{"error": {"message": "Rate limit exceeded"}}')
            raise urllib.error.HTTPError(
                url="https://openrouter.ai/api/alpha/decisions",
                code=429,
                msg="Too Many Requests",
                hdrs={},
                fp=fp,
            )

        adapter = JevShadowAdapter(transport=rate_limited_transport)
        obs = adapter.observe_task(
            case_id="case-429",
            task_text="Implementar parser JSON.",
            stratum="coding",
            ground_truth="coding",
        )

        assert obs.eligible
        assert obs.candidate_prediction is None
        assert obs.error == "http_429"
        assert obs.baseline_prediction is not None

    def test_sentinel_risk_bypass_grill_violation(self, base_spec: PilotSpec) -> None:
        evaluator = PilotEvaluator(base_spec)
        obs = PilotObservation(
            case_id="case-grill-bypass",
            demand_hash="hashgrillbypass0",
            stratum="planning",
            eligible=True,
            candidate_prediction={"choice": "planning"},
            baseline_prediction={"choice": "planning"},
            ground_truth="planning",
            provenance={"bypassed_grill": True},
        )
        report = evaluator.evaluate([obs])
        assert report.verdict == "harmful"
        assert any(v["risk"] == "fake_ready_for_spec" for v in report.sentinel_violations)

    def test_sentinel_risk_out_of_catalog(self, base_spec: PilotSpec) -> None:
        evaluator = PilotEvaluator(base_spec)
        obs = PilotObservation(
            case_id="case-unknown-label",
            demand_hash="hashunknownlabel",
            stratum="coding",
            eligible=True,
            candidate_prediction={"choice": "quantum_computing"},  # outside catalog
            baseline_prediction={"choice": "coding"},
            ground_truth="coding",
        )
        report = evaluator.evaluate([obs])
        assert report.verdict == "harmful"
        assert any(v["risk"] == "out_of_catalog" for v in report.sentinel_violations)

    def test_replay_historical_experiment_results(self, monkeypatch: pytest.MonkeyPatch, base_spec: PilotSpec) -> None:
        """Replays historical synthetic records from 2026-09-19 experiment."""
        import json
        from pathlib import Path

        results_path = Path("C:/dev/DarkFac/.factory/experiments/jev-routing-20260919/results.json")
        if not results_path.exists():
            pytest.skip("Historical experiment results.json not found")

        data = json.loads(results_path.read_text(encoding="utf-8"))
        records = data.get("records", [])
        records_by_id = {r["id"]: r for r in records}

        monkeypatch.setenv("TYPESAFE_API_KEY", "dummy-test-key")

        def replay_transport(payload: dict[str, Any], key: str) -> dict[str, Any]:
            # Mock transport looks up the record by task content match
            task_text = payload["state"]["task"]
            for r in records:
                if r["task"] == task_text:
                    return {
                        "model": r["model"],
                        "provider": r["provider"],
                        "answers": r["answers"],
                        "usage": r["usage"],
                    }
            raise ValueError(f"Task not found in historical records: {task_text}")

        adapter = JevShadowAdapter(transport=replay_transport)
        observations: list[PilotObservation] = []

        for r in records:
            obs = adapter.observe_task(
                case_id=f"replay-{r['id']}",
                task_text=r["task"],
                stratum="offline" if "offline" in r["id"] else "online",
                ground_truth=r["expected_stage"],
            )
            observations.append(obs)

        # 1. Total records: 8
        assert len(observations) == 8

        # 2. Offline case was preserved without network call
        offline_obs = next(o for o in observations if "offline" in o.case_id)
        assert not offline_obs.eligible
        assert "offline_task_preserved" in (offline_obs.exclusion_reason or "")
        assert offline_obs.candidate_prediction is None

        # 3. 7 eligible cases processed
        eligible_obs = [o for o in observations if o.eligible]
        assert len(eligible_obs) == 7

        # 4. Statistical evaluation under base_spec (min_sample_size = 5)
        evaluator = PilotEvaluator(base_spec)
        report = evaluator.evaluate(observations)

        # 7 cases is between min_sample_size (5) and target (10)
        assert report.total_cases == 8
        assert report.eligible_cases == 7
        assert report.excluded_cases == 1
        assert report.candidate_error_rate == 0.0  # 7/7 correct for eligible
        assert len(report.sentinel_violations) == 0

        # Verify no raw task text is exposed in report
        report_str = report.model_dump_json()
        for r in records:
            assert r["task"] not in report_str

