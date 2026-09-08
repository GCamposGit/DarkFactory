"""Acceptance tests for DF-20 delivery checks, risk policy and idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.integrations.github import GitHubCheck, GitHubClient, PullRequestSnapshot
from core.orchestrator.delivery import (
    DeliveryBlockedError,
    DeliveryPolicy,
    DeliveryRequest,
    DeliveryRisk,
    DeliveryStatus,
    IdempotencyConflictError,
    MergeQueue,
)


def make_request(**overrides: object) -> DeliveryRequest:
    values: dict[str, object] = {
        "task_id": "DF-20",
        "repository": "GCamposGit/DarkFactory",
        "pull_request_number": 20,
        "base_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "risk_class": DeliveryRisk.B,
        "required_checks": ("trusted-pr-policy", "pr-validation"),
        "idempotency_key": "df20-pr20-b" ,
    }
    values.update(overrides)
    return DeliveryRequest(**values)


def make_snapshot(*, checks: tuple[GitHubCheck, ...] | None = None, **overrides: object) -> PullRequestSnapshot:
    head_sha = str(overrides.get("head_sha", "b" * 40))
    values: dict[str, object] = {
        "repository": "GCamposGit/DarkFactory",
        "number": 20,
        "base_sha": "a" * 40,
        "head_sha": head_sha,
        "state": "open",
        "draft": False,
        "mergeable": True,
        "checks": checks if checks is not None else (
            GitHubCheck(
                name="trusted-pr-policy",
                head_sha=head_sha,
                status="completed",
                conclusion="success",
            ),
            GitHubCheck(
                name="pr-validation",
                head_sha=head_sha,
                status="completed",
                conclusion="success",
            ),
        ),
    }
    values.update(overrides)
    return PullRequestSnapshot(**values)


def test_exact_sha_checks_are_required_for_eligibility() -> None:
    decision = DeliveryPolicy().evaluate(make_request(), make_snapshot())

    assert decision.status is DeliveryStatus.ELIGIBLE
    assert decision.eligible is True
    assert decision.passed_checks == ("trusted-pr-policy", "pr-validation")


def test_old_green_check_does_not_release_new_candidate_sha() -> None:
    stale_check = GitHubCheck(
        name="trusted-pr-policy",
        head_sha="c" * 40,
        status="completed",
        conclusion="success",
    )
    decision = DeliveryPolicy().evaluate(
        make_request(),
        make_snapshot(checks=(stale_check, make_snapshot().checks[1])),
    )

    assert decision.status is DeliveryStatus.BLOCKED
    assert decision.stale_checks == ("trusted-pr-policy",)
    assert "stale_checks" in decision.reason


def test_missing_and_failed_checks_block_delivery() -> None:
    failed = GitHubCheck(
        name="trusted-pr-policy",
        head_sha="b" * 40,
        status="completed",
        conclusion="failure",
    )
    decision = DeliveryPolicy().evaluate(
        make_request(),
        make_snapshot(checks=(failed,)),
    )

    assert decision.status is DeliveryStatus.BLOCKED
    assert decision.failed_checks == ("trusted-pr-policy",)
    assert decision.missing_checks == ("pr-validation",)


@pytest.mark.parametrize(
    "overrides, expected_reason",
    [
        ({"base_sha": "c" * 40}, "base_sha_mismatch"),
            ({"head_sha": "c" * 40}, "candidate_sha_mismatch"),
        ({"state": "closed"}, "pull_request_not_open"),
        ({"draft": True}, "draft_pull_request"),
        ({"mergeable": None}, "mergeability_not_confirmed"),
    ],
)
def test_current_pr_identity_and_state_are_fail_closed(
    overrides: dict[str, object], expected_reason: str
) -> None:
    decision = DeliveryPolicy().evaluate(make_request(), make_snapshot(**overrides))

    assert decision.status is DeliveryStatus.BLOCKED
    assert expected_reason in decision.reason


def test_high_risk_delivery_requires_manual_review_even_when_checks_pass() -> None:
    decision = DeliveryPolicy().evaluate(
        make_request(risk_class=DeliveryRisk.C),
        make_snapshot(),
    )

    assert decision.status is DeliveryStatus.MANUAL_REVIEW
    assert not decision.eligible


def test_merge_queue_replays_same_request_and_rejects_key_reuse(tmp_path: Path) -> None:
    request = make_request()
    policy = DeliveryPolicy()
    decision = policy.evaluate(request, make_snapshot())
    queue = MergeQueue(tmp_path / "merge_queue.json")

    first = queue.enqueue(request, decision)
    replay = queue.enqueue(request, decision)

    assert replay.queue_id == first.queue_id
    assert replay.replayed is True
    assert replay.replay_count == 1

    conflicting_request = make_request(candidate_sha="c" * 40)
    with pytest.raises(IdempotencyConflictError):
        queue.enqueue(conflicting_request, policy.evaluate(conflicting_request, make_snapshot(head_sha="c" * 40)))


def test_merge_queue_persists_and_blocks_ineligible_decision(tmp_path: Path) -> None:
    request = make_request()
    policy = DeliveryPolicy()
    queue_path = tmp_path / "merge_queue.json"
    queue = MergeQueue(queue_path)
    queue.enqueue(request, policy.evaluate(request, make_snapshot()))

    reloaded = MergeQueue(queue_path)
    assert reloaded.get(request.idempotency_key) is not None
    assert reloaded.get(request.idempotency_key).queue_id.startswith("mq_")  # type: ignore[union-attr]

    blocked = policy.evaluate(
        make_request(idempotency_key="blocked"),
        make_snapshot(checks=()),
    )
    with pytest.raises(DeliveryBlockedError):
        reloaded.enqueue(make_request(idempotency_key="blocked"), blocked)


def test_github_client_normalizes_pull_request_and_checks_without_network() -> None:
    calls: list[str] = []

    def transport(url: str, headers: dict[str, str]) -> dict[str, object]:
        calls.append(url)
        assert "Authorization" not in headers
        if "/pulls/20" in url:
            return {
                "state": "open",
                "draft": False,
                "mergeable": True,
                "updated_at": "2026-09-08T00:00:00Z",
                "base": {"sha": "a" * 40},
                "head": {"sha": "b" * 40},
            }
        return {
            "check_runs": [
                {
                    "name": "pr-validation",
                    "head_sha": "b" * 40,
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        }

    snapshot = GitHubClient(transport=transport).get_pull_request_snapshot(
        "GCamposGit/DarkFactory", 20
    )

    assert snapshot.head_sha == "b" * 40
    assert snapshot.checks[0].passed is True
    assert len(calls) == 2
