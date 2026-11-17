"""Read-only admission before a ticket worktree or PLAN (USR-468 T1, simplified by USR-504)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
from pydantic import ValidationError

from core.line import admission, routing
from core.line.admission import (
    CODE_NO_BUILD_ROUTE,
    CODE_NO_PLANNING_ROUTE,
    STAGE_BUILD,
    STAGE_PLANNING,
    AdmissionDecision,
    admit,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
_FIELDS = {"outcome", "code", "stage", "resource", "observed_at", "evidence_ref", "not_before", "cause"}


def test_decision_fields_are_allow_or_wait_with_the_audit_set() -> None:
    allowed = admission.allow_decision(NOW, evidence_ref="admission://allow")
    assert set(AdmissionDecision.model_fields) == _FIELDS
    assert set(allowed.public_dict()) == _FIELDS
    assert allowed.outcome == "allow" and allowed.not_before is None
    assert allowed.public_dict()["not_before"] is None
    assert allowed.public_dict()["cause"] is None
    assert allowed.public_dict()["observed_at"].endswith("+00:00")
    with pytest.raises(ValidationError):
        AdmissionDecision(
            outcome="allow", code="admitted", stage="admission", resource="line",
            observed_at=NOW, evidence_ref="admission://allow", not_before=NOW,
        )
    with pytest.raises(ValidationError):
        AdmissionDecision(
            outcome="wait", code="no_build_route", stage="build", resource="route:build",
            observed_at=NOW, evidence_ref="routing://development/none",
        )
    with pytest.raises(ValidationError):
        AdmissionDecision(
            outcome="allow", code="admitted", stage="admission", resource="line",
            observed_at=NOW, evidence_ref="admission://allow", harness="claude",
        )



def _admit(
    routes: dict[str, Optional[tuple[str, Optional[str]]]],
    *,
    plan_exists: bool = False,
    earliest_at: Optional[datetime] = None,
) -> tuple[AdmissionDecision, list[dict[str, Any]], list[str]]:
    seen: list[dict[str, Any]] = []
    earliest_calls: list[str] = []

    def pick_route(stage: str, caps: list[str], reserve_quota: bool = False, **kwargs: Any) -> Any:
        seen.append({"stage": stage, "reserve_quota": reserve_quota, "mode": kwargs.get("mode")})
        return routes.get(stage)

    def earliest(stage: str, *_args: Any, **_kwargs: Any) -> datetime:
        earliest_calls.append(stage)
        return earliest_at if earliest_at is not None else NOW

    decision = admit(
        build_caps=["harness:codex", "harness:grok"],
        read_caps=["harness:claude", "harness:codex"],
        execution_context="headless",
        plan_exists=plan_exists,
        pick_route=pick_route,
        earliest=earliest,
        clock=lambda: NOW,
    )
    return decision, seen, earliest_calls


def test_admission_needs_only_a_development_route_and_a_planning_route_without_a_plan() -> None:
    full = {"planning": ("claude", "opus"), "development": ("codex", None)}
    decision, seen, earliest_calls = _admit(full)
    assert decision.outcome == "allow" and earliest_calls == []
    assert [row["stage"] for row in seen] == ["planning", "development"]
    assert all(row["reserve_quota"] is False for row in seen)
    assert decision.evidence_ref == "routing://planning/claude;routing://development/codex"

    # No review route and no gate observation matter: both decide when their turn comes.
    # An existing plan skips the planning route altogether.
    planned, seen, _ = _admit({"planning": None, "development": ("openrouter", None)}, plan_exists=True)
    assert planned.outcome == "allow" and [row["stage"] for row in seen] == ["development"]


def test_missing_routes_wait_with_the_nearest_reset_capped_at_thirty_minutes() -> None:
    no_build, _seen, calls = _admit({"planning": ("claude", "opus"), "development": None})
    assert no_build.code == CODE_NO_BUILD_ROUTE and no_build.stage == STAGE_BUILD
    assert calls == ["development"]
    assert no_build.not_before == NOW + timedelta(seconds=routing.MIN_ROUTE_WAIT_SECONDS)

    routes = {"planning": None, "development": ("codex", None)}
    later, _seen, _calls = _admit(routes, earliest_at=NOW + timedelta(hours=2))
    assert later.code == CODE_NO_PLANNING_ROUTE and later.stage == STAGE_PLANNING
    assert later.not_before == NOW + timedelta(minutes=30)
    earlier, _seen, calls = _admit(routes, earliest_at=NOW - timedelta(hours=1))
    assert earlier.not_before == NOW + timedelta(seconds=routing.MIN_ROUTE_WAIT_SECONDS)
    assert calls == ["planning"]


def _routing_admission_for_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    remaining: float,
    openrouter_credit: float,
    project_id: str = "darkfac",
):
    from core.enterprise import enforcement

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(
        enforcement,
        "_shared_configs_file",
        lambda: tmp_path / "shared" / "data_policies.json",
    )

    def pick_route(stage: str, caps: list[str], **kwargs: Any):
        return routing.pick(
            stage,
            caps,
            quota_lookup=lambda _provider: remaining,
            openrouter_balance_lookup=lambda: openrouter_credit,
            cooldown_path=tmp_path / "cooldowns.json",
            **kwargs,
        )

    all_caps = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]
    return admit(
        build_caps=all_caps,
        read_caps=all_caps,
        execution_context="headless",
        project_id=project_id,
        pick_route=pick_route,
        earliest=lambda *_args, **_kwargs: NOW,
        clock=lambda: NOW,
    )


def test_no_planning_route_reports_residual_policy_and_source_file(tmp_path, monkeypatch):
    policy_file = tmp_path / "state" / "enterprise" / "configs.json"
    policy_file.parent.mkdir(parents=True)
    policy_file.write_text(
        json.dumps([
            {
                "project_id": "darkfac",
                "enabled": True,
                "residency_mode": "local_only",
                "allowed_providers": ["ollama_local"],
                "metadata": {"private_note": "must not be returned"},
            }
        ]),
        encoding="utf-8",
    )

    decision = _routing_admission_for_test(
        tmp_path, monkeypatch, remaining=90.0, openrouter_credit=5.0,
    )

    cause = decision.public_dict()["cause"]
    assert decision.code == CODE_NO_PLANNING_ROUTE
    assert cause["code"] == "data_policy"
    assert cause["project_id"] == "darkfac"
    assert cause["source_file"] == str(policy_file.resolve())
    assert cause["providers"] == ["claude", "codex", "grok", "openrouter"]
    assert cause["restriction"] == {
        "residency_mode": "local_only",
        "allowed_providers": ["ollama_local"],
    }
    assert "must not be returned" not in json.dumps(cause)


def test_no_planning_route_does_not_blame_policy_when_routes_fail_for_quota(tmp_path, monkeypatch):
    decision = _routing_admission_for_test(
        tmp_path, monkeypatch, remaining=5.0, openrouter_credit=0.0,
    )

    assert decision.code == CODE_NO_PLANNING_ROUTE
    assert decision.public_dict()["cause"] is None


def test_unknown_execution_context_is_rejected() -> None:
    with pytest.raises(ValueError):
        admit(
            build_caps=["harness:codex"],
            read_caps=["harness:claude"],
            execution_context="fleet",
            pick_route=lambda *_a, **_k: ("codex", None),
            clock=lambda: NOW,
        )


def test_recovery_receipt_requires_fresh_candidate_bound_production_chain():
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    candidate = "candidate-468"
    stamp = now.isoformat()

    def stage(**values):
        return {"candidate_digest": candidate, "observed_at": stamp,
                "evidence_ref": "evidence://stage", **values}

    receipt = {
        "schema_version": 1, "simulated": False, "candidate_digest": candidate,
        "run_id": "fleet-run-468", "implementer_family": "codex",
        "gate": stage(passed=True, marker="[HARNESS_PASS]"),
        "review": stage(approved=True, independent=True, reviewer_family="claude", implementer_family="codex"),
        "ci": stage(green=True),
        "merge": stage(sha="a" * 40, reachable=True),
        "deploy": stage(deployed=True, sha="a" * 40),
        "smoke": stage(http_status=204),
        "learning_pack": stage(indexed=True),
    }
    assert admission.verify_recovery_receipt(
        receipt, candidate_digest=candidate, run_id="fleet-run-468", now=now
    ) == (True, "receipt_valid")
    assert admission.verify_recovery_receipt(
        {**receipt, "candidate_digest": "other-candidate"}, candidate_digest=candidate,
        run_id="fleet-run-468", now=now
    )[1] == "receipt_candidate_mismatch"
    assert admission.verify_recovery_receipt(
        {**receipt, "simulated": True}, candidate_digest=candidate, run_id="fleet-run-468", now=now
    )[1] == "receipt_simulation"
    assert admission.verify_recovery_receipt(
        {**receipt, "ci": stage(green=True, observed_at=(now - timedelta(days=8)).isoformat())},
        candidate_digest=candidate, run_id="fleet-run-468", now=now,
    )[1] == "receipt_ci_stale"
    assert admission.verify_recovery_receipt(
        {**receipt, "learning_pack": None}, candidate_digest=candidate, run_id="fleet-run-468", now=now
    )[1] == "receipt_learning_pack_missing"
    assert admission.verify_recovery_receipt(
        {**receipt, "deploy": stage(deployed=True, sha="b" * 40)}, candidate_digest=candidate,
        run_id="fleet-run-468", now=now
    )[1] == "receipt_deploy_mismatch"
    assert admission.verify_recovery_receipt(
        {**receipt, "run_id": "other-run"}, candidate_digest=candidate,
        run_id="fleet-run-468", now=now,
    )[1] == "receipt_run_mismatch"
    bad_review = {**receipt, "review": stage(approved=True, independent=True,
                                              reviewer_family="claude", implementer_family="grok")}
    assert admission.verify_recovery_receipt(
        bad_review, candidate_digest=candidate, run_id="fleet-run-468", now=now,
    )[1] == "receipt_review_not_independent"
    same_family = dict(approved=True, independent=True, reviewer_family="codex", implementer_family="codex")
    contingency = {"level": 3, "persona": "standard", "reason": "same_family_other_model"}
    for review, expected in (
        (stage(**same_family), "receipt_review_not_independent"),
        (stage(**same_family, contingency={"level": 1, "persona": "standard", "reason": "x"}),
         "receipt_review_not_independent"),
        (stage(**same_family, contingency=contingency), "receipt_valid"),
    ):
        assert admission.verify_recovery_receipt(
            {**receipt, "review": review}, candidate_digest=candidate, run_id="fleet-run-468", now=now,
        )[1] == expected
