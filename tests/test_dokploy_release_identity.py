"""Live deploy proof for the line's Dokploy targets (canary + DarkHub).

Regression for the 2026-10-04/05 canary failures ``installed_digest_unverified:
active=missing``: after USR-92 the adapter stopped faking the installed digest,
but (a) the probe ignored the ``sha`` / ``git_sha`` keys the canary ``/version``
and the DarkHub ``/health`` actually expose, (b) nothing made those services
report the commit being deployed (static ``GIT_SHA=initial`` env, previously
rendered compose SHA) and (c) the stage failed on the first probe instead of
waiting for the new container. The proof stays fail-closed: a stale or foreign
digest is still a mismatch.

No network: ``urllib.request.urlopen`` is monkeypatched.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

import core.orchestrator.deployment_adapter as mod
from core.line.stage_release import ReleaseStageHandler, ReleaseStateStore
from core.orchestrator.build_artifacts import ArtifactRef
from core.orchestrator.compose_render import ComposeSourceError, render_darkhub_compose
from core.orchestrator.deployment_adapter import (
    DeploymentOperation,
    DeploymentStatus,
    DokployDeploymentAdapter,
    RollbackResult,
    TargetConfig,
)
from core.projects.models import DeployConfig, DeployTargetType, ProjectDescriptor, SmokeCheck
from tests.line.test_stage_release import _context

SHA = "a" * 40
OLD_SHA = "b" * 40
API_KEY = "s3cr3t-dokploy-key-000000000000"


@pytest.fixture(autouse=True)
def _no_ambient_dokploy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DOKPLOY_API_URL", "DOKPLOY_API_KEY", "DOKPLOY_DEPLOY_URL"):
        monkeypatch.delenv(name, raising=False)


class _Resp:
    def __init__(self, body: Any, headers: Optional[Dict[str, str]] = None) -> None:
        self._raw = json.dumps(body).encode("utf-8")
        self.headers = headers or {}

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def _install_urlopen(monkeypatch: pytest.MonkeyPatch, script: List[Any], calls: List[Dict[str, Any]]) -> None:
    def fake(request: Any, timeout: float | None = None) -> Any:
        calls.append(
            {
                "method": request.get_method(),
                "url": request.full_url,
                "body": json.loads(request.data.decode("utf-8")) if request.data else None,
            }
        )
        entry = script.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry if isinstance(entry, _Resp) else _Resp(entry)

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake)


def _artifact(sha: str = SHA) -> ArtifactRef:
    return ArtifactRef(artifact_id=sha, source_sha=sha, byte_digest=sha, byte_size=0)


def _target(**overrides: Any) -> TargetConfig:
    base: Dict[str, Any] = dict(
        project_id="darkfac-canary",
        target_type="dokploy",
        api_url="https://dokploy.example",
        api_key=API_KEY,
        service_name="app_canary",
        healthcheck_endpoint="https://canary.example/version",
        metadata={"git_sha_env": "GIT_SHA"},
    )
    base.update(overrides)
    return TargetConfig(**base)


# ---------------------------------------------------------------------------
# installed_digest: where the "active" digest comes from
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["sha", "git_sha", "digest", "artifact_digest", "oci_digest"])
def test_probe_accepts_every_identity_key(monkeypatch: pytest.MonkeyPatch, key: str) -> None:
    _install_urlopen(monkeypatch, [{key: SHA, "canary_day": "2026-10-05"}], [])
    assert DokployDeploymentAdapter().installed_digest(_target()) == SHA


def test_probe_reports_stale_digest_verbatim_so_mismatch_stays_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_urlopen(monkeypatch, [{"sha": "initial"}], [])
    assert DokployDeploymentAdapter().installed_digest(_target()) == "initial"


def test_probe_prefers_header_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_urlopen(monkeypatch, [_Resp({"sha": "initial"}, {"X-Artifact-Digest": SHA})], [])
    assert DokployDeploymentAdapter().installed_digest(_target()) == SHA


def test_unreachable_probe_is_unverified_even_with_recorded_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    adapter = DokployDeploymentAdapter()
    _install_urlopen(
        monkeypatch,
        [
            {"deployments": []},
            {"success": True},
            {"deployments": [{"createdAt": "2026-10-05T01:00:00Z", "status": "done", "description": f"Commit: {SHA}"}]},
            OSError("connection refused"),
        ],
        calls,
    )
    target = _target(metadata={})
    op = adapter.start(_artifact(), target)
    assert adapter.reconcile(op.operation_id) == DeploymentStatus.SUCCEEDED
    assert adapter.installed_digest(target) is None


def test_dokploy_recorded_commit_is_the_digest_when_endpoint_reports_none(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = DokployDeploymentAdapter()
    _install_urlopen(
        monkeypatch,
        [
            {"deployments": []},
            {"success": True},
            {"deployments": [{"createdAt": "2026-10-05T01:00:00Z", "status": "done", "description": f"Commit: {SHA}"}]},
            {"status": "ok"},  # healthcheck answers 200 with no digest key
        ],
        [],
    )
    target = _target(metadata={})
    op = adapter.start(_artifact(), target)
    adapter.reconcile(op.operation_id)
    assert adapter.installed_digest(target) == SHA


def test_dokploy_recorded_commit_of_another_sha_stays_a_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = DokployDeploymentAdapter()
    _install_urlopen(
        monkeypatch,
        [
            {"deployments": []},
            {"success": True},
            {"deployments": [{"createdAt": "2026-10-05T01:00:00Z", "status": "done", "description": f"Commit: {OLD_SHA}"}]},
            {"status": "ok"},
        ],
        [],
    )
    target = _target(metadata={})
    op = adapter.start(_artifact(), target)
    adapter.reconcile(op.operation_id)
    assert adapter.installed_digest(target) == OLD_SHA != SHA


def test_no_evidence_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_urlopen(monkeypatch, [{"status": "ok"}], [])
    assert DokployDeploymentAdapter().installed_digest(_target(metadata={})) is None
    assert DokployDeploymentAdapter().installed_digest(_target(healthcheck_endpoint=None)) is None


# ---------------------------------------------------------------------------
# start(): make the service report the commit being deployed
# ---------------------------------------------------------------------------


def test_application_env_is_bound_to_the_deployed_sha_before_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _install_urlopen(
        monkeypatch,
        [
            {"env": "PORT=8080\nGIT_SHA=initial\nOTHER=keep"},  # application.one (identity)
            True,  # application.update
            {"deployments": []},  # baseline application.one
            {"success": True},  # application.deploy
        ],
        calls,
    )
    op = DokployDeploymentAdapter().start(_artifact(), _target())

    assert op.status == DeploymentStatus.IN_PROGRESS
    assert [c["url"].rsplit("/", 1)[-1].split("?")[0] for c in calls] == [
        "application.one", "application.update", "application.one", "application.deploy",
    ]
    update = calls[1]["body"]
    assert update["applicationId"] == "app_canary"
    assert update["env"].splitlines() == ["PORT=8080", f"GIT_SHA={SHA}", "OTHER=keep"]


def test_application_env_gets_the_variable_appended_when_absent_and_skips_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _install_urlopen(
        monkeypatch,
        [{"env": "PORT=8080"}, True, {"deployments": []}, {"success": True}],
        calls,
    )
    DokployDeploymentAdapter().start(_artifact(), _target())
    assert calls[1]["body"]["env"].splitlines() == ["PORT=8080", f"GIT_SHA={SHA}"]

    calls.clear()
    _install_urlopen(
        monkeypatch,
        [{"env": f"PORT=8080\nGIT_SHA={SHA}"}, {"deployments": []}, {"success": True}],
        calls,
    )
    DokployDeploymentAdapter().start(_artifact(), _target())
    assert [c["url"].rsplit("/", 1)[-1].split("?")[0] for c in calls] == [
        "application.one", "application.one", "application.deploy",
    ]


def test_identity_binding_failure_aborts_before_deploy(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    calls: List[Dict[str, Any]] = []
    _install_urlopen(
        monkeypatch,
        [urllib.error.HTTPError("u", 403, "forbidden", {}, io.BytesIO(b"{}"))],  # type: ignore[arg-type]
        calls,
    )
    op = DokployDeploymentAdapter().start(_artifact(), _target())
    assert op.status == DeploymentStatus.FAILED
    assert op.details["error"] == "release_identity_http_403"
    assert len(calls) == 1  # never reached application.deploy


def test_target_without_binding_params_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _install_urlopen(monkeypatch, [{"deployments": []}, {"success": True}], calls)
    DokployDeploymentAdapter().start(_artifact(), _target(metadata={}))
    assert [c["url"].rsplit("/", 1)[-1].split("?")[0] for c in calls] == ["application.one", "application.deploy"]


def _hub_template(*extra_env: str) -> str:
    """The repo's docker-compose.hub.yml shape: SHA placeholders, not pinned values."""
    env = "".join(f"      - {line}\n" for line in extra_env)
    return (
        "services:\n  darkhub:\n    build:\n"
        "      context: https://${GITHUB_PAT:-}@github.com/Org/DarkFactory.git#${DARKFAC_GIT_SHA}\n"
        "      no_cache: true\n"
        "      args:\n        DARKFAC_GIT_SHA: ${DARKFAC_GIT_SHA}\n"
        "    pull_policy: build\n"
        f"    environment:\n      - DARKFAC_GIT_SHA=${{DARKFAC_GIT_SHA}}\n      - PORT=8000\n{env}"
    )


# Commit A of the repo; commit B adds an environment variable to the compose.
TEMPLATE_A = _hub_template()
TEMPLATE_B = _hub_template("NEW_FEATURE_FLAG=on")
# What Dokploy stores today: the render of commit A at the previous release.
HUB_COMPOSE = render_darkhub_compose(TEMPLATE_A, OLD_SHA)
SHA_B = "c" * 40


class _FakeFetcher:
    """In-memory GitHub: {sha: compose text}. Records every request."""

    def __init__(self, files: Dict[str, Any]) -> None:
        self.files = files
        self.requests: List[tuple[str, str, str]] = []

    def __call__(self, repository: str, sha: str, path: str) -> str:
        self.requests.append((repository, sha, path))
        value = self.files[sha]
        if isinstance(value, Exception):
            raise value
        return value


def _hub_adapter(files: Dict[str, Any]) -> tuple[DokployDeploymentAdapter, _FakeFetcher]:
    fetcher = _FakeFetcher(files)
    return DokployDeploymentAdapter(compose_fetcher=fetcher), fetcher


def _hub_target(**overrides: Any) -> TargetConfig:
    metadata = {"pin_compose_sha": "true", "compose_repo": "Org/DarkFactory"}
    metadata.update(overrides.pop("metadata", {}))
    return _target(
        project_id="darkfac",
        service_name="compose_hub",
        service_type="compose",
        healthcheck_endpoint="https://hub.example/health",
        metadata=metadata,
        **overrides,
    )


def _procedures(calls: List[Dict[str, Any]]) -> List[str]:
    return [c["url"].rsplit("/", 1)[-1].split("?")[0] for c in calls]


def test_compose_is_rendered_from_the_merged_commit_and_installed_before_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: List[Dict[str, Any]] = []
    _install_urlopen(
        monkeypatch,
        [
            {"sourceType": "raw", "composePath": "docker-compose.yml", "composeFile": HUB_COMPOSE},
            True,
            {"deployments": []},
            {"success": True},
        ],
        calls,
    )
    adapter, fetcher = _hub_adapter({SHA: TEMPLATE_A})
    op = adapter.start(_artifact(), _hub_target())

    assert op.status == DeploymentStatus.IN_PROGRESS
    assert fetcher.requests == [("Org/DarkFactory", SHA, "deploy/dokploy/docker-compose.hub.yml")]
    assert _procedures(calls) == ["compose.one", "compose.update", "compose.one", "compose.deploy"]
    body = calls[1]["body"]
    assert body["composeId"] == "compose_hub" and body["sourceType"] == "raw"
    assert body["composePath"] == "docker-compose.yml"
    assert body["composeFile"] == render_darkhub_compose(TEMPLATE_A, SHA)
    assert OLD_SHA not in body["composeFile"] and "${GITHUB_PAT:-}" in body["composeFile"]


def test_compose_changed_between_commits_sends_the_render_of_the_merged_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commit B edited the compose: Dokploy must receive B's render, not A's re-pinned."""
    adapter, fetcher = _hub_adapter({SHA: TEMPLATE_A, SHA_B: TEMPLATE_B})
    stored = render_darkhub_compose(TEMPLATE_A, SHA)  # Dokploy already runs commit A
    calls: List[Dict[str, Any]] = []
    _install_urlopen(
        monkeypatch,
        [
            {"sourceType": "raw", "composeFile": stored},
            True,
            {"deployments": []},
            {"success": True},
        ],
        calls,
    )
    op = adapter.start(_artifact(SHA_B), _hub_target())

    assert op.status == DeploymentStatus.IN_PROGRESS
    assert [r[1] for r in fetcher.requests] == [SHA_B]
    sent = calls[1]["body"]["composeFile"]
    assert sent == render_darkhub_compose(TEMPLATE_B, SHA_B)
    assert "NEW_FEATURE_FLAG=on" in sent and "NEW_FEATURE_FLAG" not in stored
    assert SHA not in sent and sent.count(SHA_B) == 3


def test_compose_already_identical_to_the_render_is_not_rewritten(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _install_urlopen(
        monkeypatch,
        [
            {"sourceType": "raw", "composeFile": render_darkhub_compose(TEMPLATE_B, SHA)},
            {"deployments": []},
            {"success": True},
        ],
        calls,
    )
    adapter, _ = _hub_adapter({SHA: TEMPLATE_B})
    adapter.start(_artifact(), _hub_target())
    assert "compose.update" not in _procedures(calls)


def test_compose_with_same_pins_but_edited_body_is_still_updated(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old re-pin logic saw 'same SHA, nothing to do'; the render must still differ and be sent."""
    calls: List[Dict[str, Any]] = []
    _install_urlopen(
        monkeypatch,
        [
            {"sourceType": "raw", "composeFile": render_darkhub_compose(TEMPLATE_A, SHA)},
            True,
            {"deployments": []},
            {"success": True},
        ],
        calls,
    )
    adapter, _ = _hub_adapter({SHA: TEMPLATE_B})
    adapter.start(_artifact(), _hub_target())
    assert _procedures(calls)[:2] == ["compose.one", "compose.update"]
    assert "NEW_FEATURE_FLAG=on" in calls[1]["body"]["composeFile"]


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("gh api exited with code 1"),  # 404 / auth failure surfaced by gh
        FileNotFoundError("gh"),
        ComposeSourceError("compose file is empty at the requested commit"),
        "",  # unreadable: empty body
    ],
)
def test_unreadable_compose_source_fails_closed_without_touching_dokploy(
    monkeypatch: pytest.MonkeyPatch, failure: Any
) -> None:
    calls: List[Dict[str, Any]] = []
    _install_urlopen(monkeypatch, [], calls)
    adapter, fetcher = _hub_adapter({SHA: failure})
    op = adapter.start(_artifact(), _hub_target())

    assert op.status == DeploymentStatus.FAILED
    assert op.details == {"error": "release_identity_compose_unreadable"}
    assert len(fetcher.requests) == 1
    assert calls == []  # no compose.one / compose.update / compose.deploy


def test_compose_source_violating_the_pinned_build_contract_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _install_urlopen(monkeypatch, [], calls)
    adapter, _ = _hub_adapter({SHA: "services:\n  darkhub:\n    image: x\n"})
    op = adapter.start(_artifact(), _hub_target())
    assert op.status == DeploymentStatus.FAILED
    assert op.details["error"] == "release_identity_compose_contract_invalid"
    assert calls == []


def test_compose_repository_must_be_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _install_urlopen(monkeypatch, [], calls)
    adapter, fetcher = _hub_adapter({SHA: TEMPLATE_A})
    target = _hub_target()
    target.metadata.pop("compose_repo")
    op = adapter.start(_artifact(), target)
    assert op.status == DeploymentStatus.FAILED
    assert op.details["error"] == "release_identity_compose_repo_missing"
    assert fetcher.requests == [] and calls == []


def test_compose_not_raw_in_dokploy_fails_closed_before_update(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _install_urlopen(monkeypatch, [{"sourceType": "github", "composeFile": HUB_COMPOSE}], calls)
    adapter, _ = _hub_adapter({SHA: TEMPLATE_A})
    op = adapter.start(_artifact(), _hub_target())
    assert op.status == DeploymentStatus.FAILED
    assert op.details["error"] == "release_identity_compose_not_raw"
    assert _procedures(calls) == ["compose.one"]


def test_binding_requires_a_full_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[Dict[str, Any]] = []
    _install_urlopen(monkeypatch, [], calls)
    adapter, fetcher = _hub_adapter({})
    op = adapter.start(_artifact("short"), _hub_target())
    assert op.status == DeploymentStatus.FAILED
    assert op.details["error"] == "release_identity_sha_not_full"
    assert calls == [] and fetcher.requests == []


def test_release_stage_derives_the_compose_repository_from_the_project_repo_url(tmp_path: Path) -> None:
    def config(params: Dict[str, str], repo_url: str | None) -> TargetConfig:
        project = ProjectDescriptor(
            id="darkfac",
            name="Dark Factory",
            repo_url=repo_url,
            deploy=DeployConfig(type=DeployTargetType.DOKPLOY, params=params),
        )
        handler = ReleaseStageHandler(
            project,
            dokploy_adapter=_SlowAdapter(0),  # type: ignore[arg-type]
            state_store=ReleaseStateStore(state_dir=tmp_path / "state"),
        )
        return handler._target_config(SHA, None)

    pinned = {"service_type": "compose", "service_name": "c1", "pin_compose_sha": "true"}
    url = "https://github.com/GCamposGit/DarkFactory.git"
    assert config(pinned, url).metadata["compose_repo"] == "GCamposGit/DarkFactory"
    assert config({**pinned, "compose_repo": "Other/Repo"}, url).metadata["compose_repo"] == "Other/Repo"
    assert "compose_repo" not in config(pinned, None).metadata
    assert "compose_repo" not in config({"service_type": "compose", "service_name": "c1"}, url).metadata


def test_hub_health_probe_matches_pinned_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_urlopen(monkeypatch, [{"status": "ok", "git_sha": SHA}], [])
    assert DokployDeploymentAdapter().installed_digest(_hub_target()) == SHA


# ---------------------------------------------------------------------------
# Stage: wait for the new container instead of failing on the first probe
# ---------------------------------------------------------------------------


class _SlowAdapter:
    """Reports the new digest only after `ready_after` probes."""

    def __init__(self, ready_after: int) -> None:
        self.ready_after = ready_after
        self.probes = 0
        self.starts = 0

    def start(self, artifact_ref: ArtifactRef, target_config: TargetConfig, claim: Any = None) -> DeploymentOperation:
        self.starts += 1
        return DeploymentOperation(
            operation_id="op1",
            external_operation_id="ext1",
            project_id=target_config.project_id,
            target_type=target_config.target_type,
            artifact_ref=artifact_ref,
            status=DeploymentStatus.SUCCEEDED,
        )

    def reconcile(self, operation_id: str) -> DeploymentStatus:
        return DeploymentStatus.SUCCEEDED

    def installed_digest(self, target_config: TargetConfig) -> Optional[str]:
        self.probes += 1
        return SHA if self.probes > self.ready_after else None

    def rollback(self, target_config: TargetConfig, failed_digest: str, reason: str, claim: Any = None) -> RollbackResult:
        raise AssertionError("rollback not expected")


def _handler(adapter: _SlowAdapter, tmp_path: Path, digest_polls: int) -> ReleaseStageHandler:
    project = ProjectDescriptor(
        id="acme",
        name="Acme Project",
        deploy=DeployConfig(type=DeployTargetType.DOKPLOY, params={}),
        smoke=[SmokeCheck(url="https://acme.example/healthz", expect_status=200)],
    )
    return ReleaseStageHandler(
        project,
        dokploy_adapter=adapter,  # type: ignore[arg-type]
        state_store=ReleaseStateStore(state_dir=tmp_path / "state"),
        smoke_opener=lambda _u, _t: (200, b"ok"),
        smoke_retries=1,
        smoke_delay_s=0.0,
        reconcile_polls=1,
        reconcile_delay_s=0.0,
        digest_polls=digest_polls,
        digest_delay_s=0.0,
        sleep=lambda _s: None,
    )


def test_stage_waits_for_the_new_container_to_report_the_sha(tmp_path: Path) -> None:
    adapter = _SlowAdapter(ready_after=3)
    result = _handler(adapter, tmp_path, digest_polls=6).handle(_context("run-slow", "", SHA))
    assert result.outcome == "success"
    assert adapter.starts == 1  # one deploy, several probes


def test_stage_still_fails_closed_when_digest_never_converges(tmp_path: Path) -> None:
    adapter = _SlowAdapter(ready_after=10**6)
    result = _handler(adapter, tmp_path, digest_polls=4).handle(_context("run-never", "", SHA))
    assert result.outcome == "retry"
    assert (result.cause_code or "").startswith("installed_digest_unverified:active=missing")
    assert adapter.starts == 1
    # 4 polls in _deploy; no extra probe because the stage returned before smoke.
    assert adapter.probes == 4
