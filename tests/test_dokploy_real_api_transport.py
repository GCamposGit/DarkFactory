"""HF-27-07 closeout -- real Dokploy API transport for `DokployDeploymentAdapter`.

No real network: `urllib.request.urlopen` is monkeypatched to a fake that
returns/raises synthetically (same pattern as `tests/test_dokploy_redeploy.py`).
Covers the gap that left `core/line/stage_release.py::_deploy` always
returning `retry deploy_timed_out` for `deploy.type == "dokploy"` projects:
without a real transport, `reconcile()` never left `IN_PROGRESS`.
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any, Callable, Dict, List, Optional

import pytest

from core.orchestrator.build_artifacts import ArtifactRef
from core.orchestrator.deployment_adapter import (
    DeploymentStatus,
    DokployDeploymentAdapter,
    TargetConfig,
)

API_KEY = "s3cr3t-dokploy-key-000000000000"


@pytest.fixture(autouse=True)
def _no_ambient_dokploy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hosts that deploy (the cloud worker, the owner's Notebook) export real
    DOKPLOY_* credentials; every test here configures the adapter explicitly."""
    for name in ("DOKPLOY_API_URL", "DOKPLOY_API_KEY", "DOKPLOY_DEPLOY_URL"):
        monkeypatch.delenv(name, raising=False)


class FakeResponse:
    """Minimal stand-in for `http.client.HTTPResponse` used as a context manager."""

    def __init__(self, body: Dict[str, Any]) -> None:
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _artifact(sha: str = "a" * 40) -> ArtifactRef:
    return ArtifactRef(artifact_id=sha, source_sha=sha, byte_digest=sha, byte_size=0)


def _queue_urlopen(
    monkeypatch: pytest.MonkeyPatch, script: List[Any], calls: List[Dict[str, Any]]
) -> None:
    """`script` entries are consumed FIFO: a dict body -> FakeResponse(body);
    an Exception instance -> raised. `calls` records each request for assertions."""

    def fake_urlopen(request: Any, timeout: float | None = None) -> Any:
        calls.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "api_key_header": request.get_header("X-api-key"),
                "body": json.loads(request.data.decode("utf-8")) if request.data else None,
            }
        )
        entry = script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return FakeResponse(entry)

    import core.orchestrator.deployment_adapter as mod

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)


def _target_config(**overrides: Any) -> TargetConfig:
    base = dict(
        project_id="darkfac-canary",
        target_type="dokploy",
        api_url="https://dokploy.ggcampos.com",
        api_key=API_KEY,
        service_name="app_abc123",
    )
    base.update(overrides)
    return TargetConfig(**base)


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_prefix",
    [
        ("https://dokploy.ggcampos.com", "https://dokploy.ggcampos.com/api/"),
        ("https://dokploy.ggcampos.com/", "https://dokploy.ggcampos.com/api/"),
        ("https://dokploy.ggcampos.com/api", "https://dokploy.ggcampos.com/api/"),
        ("https://dokploy.ggcampos.com/api/", "https://dokploy.ggcampos.com/api/"),
    ],
)
def test_start_normalizes_api_url_with_or_without_trailing_api(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected_prefix: str
) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(
        monkeypatch,
        [
            {"deployments": []},  # baseline probe (application.one)
            {"success": True, "message": "Deployment queued"},  # application.deploy
        ],
        calls,
    )
    adapter = DokployDeploymentAdapter()
    target_config = _target_config(api_url=raw)
    adapter.start(_artifact(), target_config)

    assert calls[0]["url"].startswith(expected_prefix)
    assert calls[1]["url"] == f"{expected_prefix}application.deploy"


# ---------------------------------------------------------------------------
# Compose vs application routing
# ---------------------------------------------------------------------------


def test_start_routes_to_application_deploy_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(monkeypatch, [{"deployments": []}, {"success": True}], calls)
    adapter = DokployDeploymentAdapter()
    adapter.start(_artifact(), _target_config(service_name="app_xyz"))

    assert calls[0]["url"].endswith("application.one?applicationId=app_xyz")
    assert calls[1]["url"].endswith("application.deploy")
    assert calls[1]["body"] == {"applicationId": "app_xyz"}


def test_start_routes_to_compose_deploy_when_service_type_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(monkeypatch, [{"deployments": []}, {"success": True}], calls)
    adapter = DokployDeploymentAdapter()
    target_config = _target_config(service_name="compose_xyz", service_type="compose")
    adapter.start(_artifact(), target_config)

    assert calls[0]["url"].endswith("compose.one?composeId=compose_xyz")
    assert calls[1]["url"].endswith("compose.deploy")
    assert calls[1]["body"] == {"composeId": "compose_xyz"}


# ---------------------------------------------------------------------------
# Deploy + reconcile: done / error / running / no-new-deployment
# ---------------------------------------------------------------------------


def test_reconcile_succeeds_when_a_newer_deployment_is_done(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(
        monkeypatch,
        [
            {"deployments": [{"createdAt": "2026-09-28T10:00:00.000Z", "status": "done"}]},  # baseline
            {"success": True},  # deploy trigger
            {
                "deployments": [
                    {"createdAt": "2026-09-28T10:00:00.000Z", "status": "done"},  # old, == baseline
                    {"createdAt": "2026-09-28T10:05:00.000Z", "status": "done"},  # new, ours
                ]
            },  # reconcile GET
        ],
        calls,
    )
    adapter = DokployDeploymentAdapter()
    target_config = _target_config()
    op = adapter.start(_artifact(sha="b" * 40), target_config)
    assert op.status == DeploymentStatus.IN_PROGRESS

    status = adapter.reconcile(op.operation_id)
    assert status == DeploymentStatus.SUCCEEDED
    assert adapter.installed_digest(target_config) == "b" * 40


def test_reconcile_fails_when_a_newer_deployment_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(
        monkeypatch,
        [
            {"deployments": []},
            {"success": True},
            {"deployments": [{"createdAt": "2026-09-28T10:05:00.000Z", "status": "error"}]},
        ],
        calls,
    )
    adapter = DokployDeploymentAdapter()
    op = adapter.start(_artifact(), _target_config())
    status = adapter.reconcile(op.operation_id)
    assert status == DeploymentStatus.FAILED


def test_reconcile_stays_in_progress_while_running(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(
        monkeypatch,
        [
            {"deployments": []},
            {"success": True},
            {"deployments": [{"createdAt": "2026-09-28T10:05:00.000Z", "status": "running"}]},
        ],
        calls,
    )
    adapter = DokployDeploymentAdapter()
    op = adapter.start(_artifact(), _target_config())
    status = adapter.reconcile(op.operation_id)
    assert status == DeploymentStatus.IN_PROGRESS


def test_reconcile_stays_in_progress_when_no_new_deployment_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(
        monkeypatch,
        [
            {"deployments": [{"createdAt": "2026-09-28T10:00:00.000Z", "status": "done"}]},
            {"success": True},
            # Dokploy hasn't created the new deployment record yet.
            {"deployments": [{"createdAt": "2026-09-28T10:00:00.000Z", "status": "done"}]},
        ],
        calls,
    )
    adapter = DokployDeploymentAdapter()
    op = adapter.start(_artifact(), _target_config())
    status = adapter.reconcile(op.operation_id)
    assert status == DeploymentStatus.IN_PROGRESS


def test_reconcile_terminal_status_is_sticky_no_further_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(
        monkeypatch,
        [
            {"deployments": []},
            {"success": True},
            {"deployments": [{"createdAt": "2026-09-28T10:05:00.000Z", "status": "done"}]},
        ],
        calls,
    )
    adapter = DokployDeploymentAdapter()
    op = adapter.start(_artifact(), _target_config())
    assert adapter.reconcile(op.operation_id) == DeploymentStatus.SUCCEEDED
    calls_before = len(calls)
    # Second reconcile on a terminal op must not issue another HTTP call.
    assert adapter.reconcile(op.operation_id) == DeploymentStatus.SUCCEEDED
    assert len(calls) == calls_before


# ---------------------------------------------------------------------------
# Transient reconcile failure keeps IN_PROGRESS (never FAILED on a blip)
# ---------------------------------------------------------------------------


def test_reconcile_network_error_keeps_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(
        monkeypatch,
        [
            {"deployments": []},
            {"success": True},
            urllib.error.URLError("connection reset"),
        ],
        calls,
    )
    adapter = DokployDeploymentAdapter()
    op = adapter.start(_artifact(), _target_config())
    status = adapter.reconcile(op.operation_id)
    assert status == DeploymentStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# HTTP 4xx on deploy trigger -> FAILED
# ---------------------------------------------------------------------------


def test_start_http_4xx_on_deploy_is_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    body = f"Unauthorized: key {API_KEY} rejected".encode("utf-8")
    err = urllib.error.HTTPError("https://dokploy.ggcampos.com/api/application.deploy", 401, "Unauthorized", hdrs=None, fp=io.BytesIO(body))
    _queue_urlopen(monkeypatch, [{"deployments": []}, err], calls)
    adapter = DokployDeploymentAdapter()
    op = adapter.start(_artifact(), _target_config())
    assert op.status == DeploymentStatus.FAILED
    assert op.details["error"] == "http_401"


# ---------------------------------------------------------------------------
# Missing service_name -> fail closed, never guesses an id (item 3: darkfac
# project has deploy.params={} and must never probe a bogus application id)
# ---------------------------------------------------------------------------


def test_start_without_service_name_fails_closed_no_http_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(monkeypatch, [], calls)  # any HTTP call here is a bug
    adapter = DokployDeploymentAdapter()
    target_config = _target_config(service_name=None)
    op = adapter.start(_artifact(), target_config)
    assert op.status == DeploymentStatus.FAILED
    assert op.details["error"] == "dokploy_service_name_missing"
    assert calls == []


def test_stage_release_target_config_leaves_service_name_none_when_unset() -> None:
    """core.line.stage_release._target_config must not default service_name
    to the project slug -- that is exactly the "bogus id" the adapter above
    refuses to call the API with."""
    from core.line.stage_release import ReleaseStageHandler
    from core.projects.models import DeployConfig, DeployTargetType, ProjectDescriptor

    project = ProjectDescriptor(
        id="darkfac",
        name="Dark Factory (Core)",
        deploy=DeployConfig(type=DeployTargetType.DOKPLOY, params={}),
    )
    handler = ReleaseStageHandler(project)
    target_config = handler._target_config("sha123", None)
    assert target_config.service_name is None
    assert target_config.service_type == "application"


# ---------------------------------------------------------------------------
# Rollback via the real API: honest failure, never fabricates restored_digest
# ---------------------------------------------------------------------------


def test_rollback_real_api_triggers_redeploy_and_reports_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(monkeypatch, [{"success": True, "message": "Deployment queued"}], calls)
    adapter = DokployDeploymentAdapter()
    target_config = _target_config(last_known_good_digest="c" * 40)

    result = adapter.rollback(target_config, failed_digest="d" * 40, reason="post-deploy smoke check failed")

    assert result.status == DeploymentStatus.FAILED
    assert result.restored_digest is None
    assert "redeploy" in result.reason.lower()
    assert len(calls) == 1
    assert calls[0]["body"] == {"applicationId": "app_abc123"}


def test_rollback_real_api_without_last_known_good_digest_never_calls_api(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(monkeypatch, [], calls)
    adapter = DokployDeploymentAdapter()
    target_config = _target_config(last_known_good_digest=None)

    result = adapter.rollback(target_config, failed_digest="d" * 40, reason="x")
    assert result.status == DeploymentStatus.FAILED
    assert result.restored_digest is None
    assert calls == []


def test_rollback_real_api_without_service_name_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(monkeypatch, [], calls)
    adapter = DokployDeploymentAdapter()
    target_config = _target_config(service_name=None, last_known_good_digest="c" * 40)

    result = adapter.rollback(target_config, failed_digest="d" * 40, reason="x")
    assert result.status == DeploymentStatus.FAILED
    assert result.restored_digest is None
    assert calls == []


def test_rollback_without_real_api_creds_keeps_legacy_simulated_behaviour() -> None:
    """No DOKPLOY_API_URL/KEY (and no injected transport): unchanged
    pre-existing behaviour -- ROLLED_BACK with restored_digest set."""
    adapter = DokployDeploymentAdapter()
    target_config = TargetConfig(
        project_id="darkfac",
        target_type="dokploy_docker_compose",
        last_known_good_digest="sha256_lkg_stable_999",
    )
    result = adapter.rollback(target_config, failed_digest="sha256_broken_digest_888", reason="x")
    assert result.status == DeploymentStatus.ROLLED_BACK
    assert result.restored_digest == "sha256_lkg_stable_999"


# ---------------------------------------------------------------------------
# Missing creds -> previous (legacy webhook/no-op) behaviour unaffected
# ---------------------------------------------------------------------------


def test_start_without_creds_falls_back_to_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: float | None = None) -> Any:
        calls.append({"url": request.full_url, "method": request.get_method()})
        return FakeResponse({"deployment_id": "wh_123"})

    import core.orchestrator.deployment_adapter as mod

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    adapter = DokployDeploymentAdapter()  # no env creds, no injected transport
    target_config = TargetConfig(
        project_id="site-ggcampos",
        target_type="static_web",
        deploy_url="https://dokploy.internal/webhook/deploy",
    )
    op = adapter.start(_artifact(), target_config)
    assert op.status == DeploymentStatus.IN_PROGRESS
    assert op.external_operation_id == "wh_123"
    assert calls == [{"url": "https://dokploy.internal/webhook/deploy", "method": "POST"}]


def test_start_with_injected_transport_takes_priority_over_real_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """An injected transport must win even when DOKPLOY_API_URL/KEY are also
    configured -- no real HTTP call should ever be attempted."""
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(monkeypatch, [], calls)  # any real HTTP call here is a bug

    transport_calls: List[Any] = []

    def fake_transport(method: str, url: str, data: Any, headers: Any) -> Dict[str, Any]:
        transport_calls.append((method, url, data))
        return {"operationId": "injected_op_1", "status": "IN_PROGRESS"}

    adapter = DokployDeploymentAdapter(transport=fake_transport)
    op = adapter.start(_artifact(), _target_config())
    assert op.external_operation_id == "injected_op_1"
    assert len(transport_calls) == 1
    assert calls == []


# ---------------------------------------------------------------------------
# API key is never logged
# ---------------------------------------------------------------------------


def test_api_key_never_appears_in_logs_on_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: List[Dict[str, Any]] = []
    body = f"Unauthorized: key {API_KEY} rejected".encode("utf-8")
    err = urllib.error.HTTPError("https://dokploy.ggcampos.com/api/application.deploy", 401, "Unauthorized", hdrs=None, fp=io.BytesIO(body))
    _queue_urlopen(monkeypatch, [{"deployments": []}, err], calls)
    adapter = DokployDeploymentAdapter()

    with caplog.at_level("WARNING"):
        adapter.start(_artifact(), _target_config())

    assert API_KEY not in caplog.text
    # The header itself carried it (that's the only place it may appear).
    assert calls[1]["api_key_header"] == API_KEY


# ---------------------------------------------------------------------------
# Self-restart guard: never deploy/rollback the worker's own compose
# ---------------------------------------------------------------------------


def test_adapter_refuses_the_workers_own_compose_without_any_http_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(monkeypatch, [], calls)  # any HTTP call here is a bug
    adapter = DokployDeploymentAdapter()
    target_config = _target_config(service_name="QJK0YXPQvCgjrpdgWH0uo", service_type="compose")

    op = adapter.start(_artifact(), target_config)

    assert op.status == DeploymentStatus.FAILED
    assert op.details["error"] == "deploy_target_forbidden_self_restart"
    assert adapter.reconcile(op.operation_id) == DeploymentStatus.FAILED
    assert calls == []


def test_adapter_rollback_also_refuses_the_workers_own_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(monkeypatch, [], calls)
    adapter = DokployDeploymentAdapter()
    target_config = _target_config(
        service_name="QJK0YXPQvCgjrpdgWH0uo", service_type="compose", last_known_good_digest="c" * 40
    )

    result = adapter.rollback(target_config, failed_digest="d" * 40, reason="smoke failed")

    assert result.status == DeploymentStatus.FAILED
    assert result.restored_digest is None
    assert "deploy_target_forbidden_self_restart" in result.reason
    assert calls == []


def test_adapter_deploys_the_darkhub_compose_via_compose_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _queue_urlopen(
        monkeypatch,
        [{"deployments": []}, {"success": True}],
        calls,
    )
    adapter = DokployDeploymentAdapter()
    target_config = _target_config(service_name="wuH-sjZBig74xFGdk4IsL", service_type="compose")

    op = adapter.start(_artifact(), target_config)

    assert op.status == DeploymentStatus.IN_PROGRESS
    assert calls[-1]["body"] == {"composeId": "wuH-sjZBig74xFGdk4IsL"}
    assert "compose.deploy" in calls[-1]["url"]
