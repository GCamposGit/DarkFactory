"""Deterministic integration tests for the HF-02-03 effect oracle."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from spikes.runtime_choice.effect_server import EffectServer
from spikes.runtime_choice.effect_store import (
    ApprovalSubjectConflictError,
    ApprovalSubjectNotBoundError,
    MAX_REQUEST_BYTES,
    NativeEffectStore,
    load_scenario_catalog,
    scenario_catalog_hash,
)


def payload_hash(operation_key: str) -> str:
    return hashlib.sha256(operation_key.encode("utf-8")).hexdigest()


def request_json(method: str, url: str, payload: dict[str, object] | None = None) -> tuple[int, dict[str, object]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, method=method, data=body)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))
    except (URLError, ConnectionError, OSError):
        raise


def effect_payload(operation_key: str, *, payload_hash_value: str | None = None) -> dict[str, object]:
    return {
        "operation_key": operation_key,
        "workflow_id": "workflow-1",
        "release_digest": "release-A",
        "payload_hash": payload_hash_value or payload_hash(operation_key),
    }


def test_same_operation_is_idempotent_under_concurrency(tmp_path: Path) -> None:
    with EffectServer(tmp_path) as server:
        payload = effect_payload("lab:workflow-1:S3")

        def post(_: int) -> tuple[int, dict[str, object]]:
            return request_json("POST", f"{server.base_url}/effects", payload)

        with ThreadPoolExecutor(max_workers=10) as executor:
            outcomes = list(executor.map(post, range(10)))

        assert {status for status, _ in outcomes} <= {200, 201}
        assert sum(status == 201 for status, _ in outcomes) == 1
        assert len({body["receipt_id"] for _, body in outcomes}) == 1
        assert server.store.effect_count() == 1

        conflict = request_json(
            "POST",
            f"{server.base_url}/effects",
            effect_payload("lab:workflow-1:S3", payload_hash_value="f" * 64),
        )
        assert conflict == (409, {"error_code": "EFFECT_CONFLICT"})


def test_commit_then_disconnect_preserves_receipt_and_restart(tmp_path: Path) -> None:
    with EffectServer(tmp_path) as server:
        key = "lab:workflow-1:S3-disconnect"
        server.arm_disconnect_once(key)
        with pytest.raises((URLError, ConnectionError, OSError)):
            request_json("POST", f"{server.base_url}/effects", effect_payload(key))

        receipt_status, receipt = request_json("GET", f"{server.base_url}/effects/{key}")
        assert receipt_status == 200
        assert receipt["operation_key"] == key

        repeated_status, repeated = request_json("POST", f"{server.base_url}/effects", effect_payload(key))
        assert repeated_status == 200
        assert repeated["receipt_id"] == receipt["receipt_id"]

        server.stop()
        server.start()
        restarted_status, restarted = request_json("GET", f"{server.base_url}/effects/{key}")
        assert restarted_status == 200
        assert restarted["receipt_id"] == receipt["receipt_id"]


def test_observations_are_not_deduplicated_and_catalog_is_frozen(tmp_path: Path) -> None:
    with EffectServer(tmp_path) as server:
        observation = {
            "workflow_id": "workflow-1",
            "kind": "step_observed",
            "step_id": "S1",
            "details": {"attempt": 1},
        }
        first = request_json("POST", f"{server.base_url}/observations", observation)
        second = request_json("POST", f"{server.base_url}/observations", observation)
        assert first[0] == second[0] == 201
        assert first[1]["sequence"] == 1
        assert second[1]["sequence"] == 2

        count_status, count = request_json(
            "GET", f"{server.base_url}/observations/count?workflow_id=workflow-1"
        )
        assert (count_status, count) == (200, {"count": 2})

    catalog = load_scenario_catalog()
    assert [item.scenario_id for item in catalog] == [f"R{index:02d}" for index in range(1, 13)]
    assert scenario_catalog_hash() == "daba0f9e6304c3c84ad653b0e0a2011b36b4749caf6eb5c2f69537300bd39ce8"


def test_approval_requires_stable_subject_binding_and_is_scoped(tmp_path: Path) -> None:
    store = NativeEffectStore(tmp_path)
    approval = {
        "decision_id": "decision-1",
        "workflow_id": "workflow-1",
        "release_digest": "release-A",
        "choice": "approved",
    }

    with pytest.raises(ApprovalSubjectNotBoundError):
        store.record_approval(approval)

    store.bind_approval_subject("workflow-1", "release-A")
    store.bind_approval_subject("workflow-1", "release-A")

    with pytest.raises(ApprovalSubjectConflictError):
        store.bind_approval_subject("workflow-1", "release-B")

    wrong_digest = {**approval, "release_digest": "release-B"}
    with pytest.raises(ApprovalSubjectConflictError):
        store.record_approval(wrong_digest)

    valid = store.record_approval(approval)
    replay = store.record_approval(approval)
    assert replay == valid

    other_workflow = {**approval, "decision_id": "decision-2", "workflow_id": "workflow-2"}
    with pytest.raises(ApprovalSubjectNotBoundError):
        store.record_approval(other_workflow)


def test_http_approval_only_forwards_to_bound_subject(tmp_path: Path) -> None:
    approval = {
        "decision_id": "decision-http-1",
        "workflow_id": "workflow-http-1",
        "release_digest": "release-A",
        "choice": "approved",
    }
    with EffectServer(tmp_path) as server:
        unbound = request_json("POST", f"{server.base_url}/approvals", approval)
        assert unbound == (409, {"error_code": "APPROVAL_SUBJECT_NOT_BOUND"})

        server.store.bind_approval_subject("workflow-http-1", "release-A")
        wrong = request_json(
            "POST",
            f"{server.base_url}/approvals",
            {**approval, "release_digest": "release-B"},
        )
        assert wrong == (409, {"error_code": "APPROVAL_SUBJECT_CONFLICT"})

        valid_status, valid = request_json("POST", f"{server.base_url}/approvals", approval)
        replay_status, replay = request_json("POST", f"{server.base_url}/approvals", approval)
        assert valid_status == replay_status == 201
        assert replay == valid

        server.stop()
        server.start()
        restarted_status, restarted = request_json("POST", f"{server.base_url}/approvals", approval)
        assert restarted_status == 201
        assert restarted == valid


def test_http_rejects_payloads_over_the_lab_limit_without_persisting_them(tmp_path: Path) -> None:
    with EffectServer(tmp_path) as server:
        oversized = {
            "workflow_id": "workflow-large",
            "kind": "step_observed",
            "details": {"blob": "x" * MAX_REQUEST_BYTES},
        }
        status, body = request_json("POST", f"{server.base_url}/observations", oversized)
        assert (status, body) == (413, {"error_code": "REQUEST_TOO_LARGE"})

        count_status, count = request_json(
            "GET", f"{server.base_url}/observations/count?workflow_id=workflow-large"
        )
        assert (count_status, count) == (200, {"count": 0})
