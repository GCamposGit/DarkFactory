"""Tests for scripts/dokploy_redeploy.py (Dokploy "deploy pos-merge" CLI).

No real network: tests/conftest.py's `offline_test_environment` fixture
blocks `urllib.request.urlopen` and non-local `socket.connect` for every
test by default. All HTTP is exercised through an injected fake `Transport`
callable (see QueueTransport below), following the same convention as
core/orchestrator/deployment_adapter.py's DokployDeploymentAdapter tests.
`main()` also accepts an injectable `registry_reader`, which every test here
overrides explicitly (even the "credentials missing" ones): the Owner's own
machine legitimately has DOKPLOY_API_URL/DOKPLOY_API_KEY set in the Windows
User registry, so relying on the real registry_reader would make these tests
depend on host state instead of being deterministic.
"""

from __future__ import annotations

import io
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from scripts import dokploy_redeploy as mod

SENTINEL_KEY = "sk-sentinel-super-secret-dokploy-key-do-not-leak-12345"


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _no_registry(_name: str) -> Optional[str]:
    """registry_reader stub that never finds anything -- used by every test
    so results never depend on the real Windows registry of the machine
    running the suite."""
    return None


class QueueTransport:
    """Fake Transport. GET responses for a given path are scripted via
    `program_get`: each call pops the next queued value, except the last
    queued value repeats forever once reached (so a wait loop that keeps
    polling past the scripted transitions still gets a stable answer).
    POST calls are just recorded and return `post_response` (default {})."""

    def __init__(self) -> None:
        self.calls: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []
        self._get_queues: Dict[str, List[Any]] = {}
        self.post_response: Any = {}

    def program_get(self, path: str, responses: Sequence[Any]) -> None:
        self._get_queues[path] = list(responses)

    def __call__(self, method: str, path: str, body: Optional[Dict[str, Any]]) -> Any:
        self.calls.append((method, path, body))
        if method == "GET":
            queue = self._get_queues.get(path)
            if queue is None:
                return {}
            if len(queue) > 1:
                return queue.pop(0)
            return queue[0]
        return self.post_response

    @property
    def post_calls(self) -> List[Tuple[str, str, Optional[Dict[str, Any]]]]:
        return [c for c in self.calls if c[0] == "POST"]


class FakeClock:
    """Deterministic clock/sleep pair for wait-loop tests: sleep() advances
    the clock instead of blocking, so tests run instantly."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start
        self.sleep_calls: List[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self.t += seconds


# ---------------------------------------------------------------------------
# Sample project.all payloads
# ---------------------------------------------------------------------------


def _new_shape_projects() -> List[Dict[str, Any]]:
    return [
        {
            "projectId": "proj_darkfac",
            "name": "darkfac-core",
            "environments": [
                {
                    "name": "production",
                    "environmentId": "env_prod",
                    "compose": [
                        {"composeId": "compose_cloud", "name": "darkfac-cloud", "composeStatus": "done"},
                        {"composeId": "compose_hub", "name": "Darkhub", "composeStatus": "done"},
                        {"composeId": "compose_n8n_darkfac", "name": "darkfac-n8n", "composeStatus": "done"},
                    ],
                    "applications": [
                        {"applicationId": "app_canary", "name": "darkfac-canary", "applicationStatus": "done"},
                    ],
                },
                {
                    "name": "staging",
                    "environmentId": "env_staging",
                    "compose": [
                        {"composeId": "compose_staging_thing", "name": "staging-thing", "composeStatus": "done"},
                    ],
                    "applications": [],
                },
            ],
        },
        {
            "projectId": "proj_other",
            "name": "My First Project",
            "environments": [
                {
                    "name": "production",
                    "environmentId": "env_other_prod",
                    # Decoy: same-shaped n8n compose in an unrelated project.
                    # Must never be selected, even by --only n8n, since the
                    # project name does not match darkfac-core.
                    "compose": [
                        {"composeId": "compose_other_n8n", "name": "n8n", "composeStatus": "done"},
                    ],
                    "applications": [],
                },
            ],
        },
    ]


def _old_shape_projects() -> List[Dict[str, Any]]:
    return [
        {
            "projectId": "proj_darkfac",
            "name": "darkfac-core",
            "compose": [
                {"composeId": "compose_cloud", "name": "darkfac-cloud", "composeStatus": "done"},
            ],
            "applications": [
                {"applicationId": "app_canary", "name": "darkfac-canary", "applicationStatus": "done"},
            ],
        },
    ]


# ---------------------------------------------------------------------------
# normalize_project_services
# ---------------------------------------------------------------------------


def test_normalize_project_services_new_shape() -> None:
    services = mod.normalize_project_services(_new_shape_projects(), "darkfac-core", "production")
    names = {s.name for s in services}
    assert names == {"darkfac-cloud", "Darkhub", "darkfac-n8n", "darkfac-canary"}
    kinds = {s.name: s.kind for s in services}
    assert kinds["darkfac-cloud"] == "compose"
    assert kinds["darkfac-canary"] == "application"


def test_normalize_project_services_old_shape() -> None:
    services = mod.normalize_project_services(_old_shape_projects(), "darkfac-core", "production")
    names = {s.name for s in services}
    assert names == {"darkfac-cloud", "darkfac-canary"}


def test_normalize_project_services_old_shape_ignored_for_non_default_environment() -> None:
    services = mod.normalize_project_services(_old_shape_projects(), "darkfac-core", "staging")
    assert services == []


def test_normalize_project_services_hard_guard_excludes_other_project_n8n() -> None:
    services = mod.normalize_project_services(_new_shape_projects(), "darkfac-core", "production")
    assert all(s.name != "n8n" for s in services)
    # And selecting the unrelated project directly must never surface into
    # a darkfac-core call -- it is simply a different discovery result.
    other = mod.normalize_project_services(_new_shape_projects(), "My First Project", "production")
    assert [s.name for s in other] == ["n8n"]


def test_normalize_project_services_respects_environment_scoping() -> None:
    prod = mod.normalize_project_services(_new_shape_projects(), "darkfac-core", "production")
    staging = mod.normalize_project_services(_new_shape_projects(), "darkfac-core", "staging")
    assert {s.name for s in prod}.isdisjoint({s.name for s in staging})
    assert [s.name for s in staging] == ["staging-thing"]


def test_normalize_project_services_unknown_project_returns_empty() -> None:
    assert mod.normalize_project_services(_new_shape_projects(), "does-not-exist", "production") == []


# ---------------------------------------------------------------------------
# ALLOWED_PROJECTS hard guard: --project is not a free-form argument. Claude
# Code's own permission config allows `python scripts/dokploy_redeploy.py *`
# with any args without a prompt (see docs/HARNESS_INTEROP.md), so this
# check is the only thing standing between a stray/careless/malicious
# --project value and an unrelated Dokploy project actually being
# redeployed. Case-sensitive exact match; nothing "close" to darkfac-core
# should ever pass.
# ---------------------------------------------------------------------------


def test_check_project_allowed_accepts_darkfac_core() -> None:
    mod.check_project_allowed("darkfac-core")  # must not raise


@pytest.mark.parametrize(
    "project",
    ["My First Project", "darkfac-core-evil", "DARKFAC-CORE", "darkfac-core ", " darkfac-core", ""],
)
def test_check_project_allowed_rejects_everything_else(project: str) -> None:
    with pytest.raises(mod.DokployUsageError) as excinfo:
        mod.check_project_allowed(project)
    assert "darkfac-core" in str(excinfo.value)


def test_resolve_services_defense_in_depth_rejects_disallowed_project_before_any_http_call() -> None:
    transport = QueueTransport()
    # Deliberately do NOT program a /api/project.all response: if the guard
    # were bypassed, fetch_project_all() would raise a different, confusing
    # error instead of the clear DokployUsageError this test expects, which
    # would itself prove the guard ran too late (or not at all).
    with pytest.raises(mod.DokployUsageError, match="darkfac-core"):
        mod._resolve_services(transport, "My First Project", "production", None)
    assert transport.calls == []  # no HTTP call of any kind was made


@pytest.mark.parametrize("project", ["My First Project", "darkfac-core-evil", "DARKFAC-CORE"])
def test_main_rejects_disallowed_project_exit_2_no_deploy_call(project: str) -> None:
    def _forbidden_transport_factory(url: str, key: str) -> mod.Transport:
        raise AssertionError("transport must never be built for a disallowed --project")

    out, err = io.StringIO(), io.StringIO()
    exit_code = mod.main(
        ["--project", project],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=_forbidden_transport_factory,
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_USAGE_ERROR
    assert "darkfac-core" in err.getvalue()
    assert SENTINEL_KEY not in (out.getvalue() + err.getvalue())


def test_main_rejects_disallowed_project_even_with_list_flag() -> None:
    """--list must not be a way around the guard: it still means "list a
    Dokploy project's services", so it is scoped by the same allowlist."""
    transport = QueueTransport()  # would blow up on first call if reached
    out, err = io.StringIO(), io.StringIO()
    exit_code = mod.main(
        ["--list", "--project", "My First Project"],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: transport,
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_USAGE_ERROR
    assert transport.calls == []
    assert transport.post_calls == []


def test_main_allows_default_project_explicitly() -> None:
    transport = _make_transport_with_full_project()
    for path in (
        "/api/compose.one?composeId=compose_cloud",
        "/api/compose.one?composeId=compose_hub",
        "/api/compose.one?composeId=compose_n8n_darkfac",
        "/api/application.one?applicationId=app_canary",
    ):
        transport.program_get(path, [{"deployments": []}])
    out, err = io.StringIO(), io.StringIO()
    exit_code = mod.main(
        ["--list", "--project", "darkfac-core"],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: transport,
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_OK
    assert "darkfac-cloud" in out.getvalue()


# ---------------------------------------------------------------------------
# select_services (--only)
# ---------------------------------------------------------------------------


def test_select_services_no_filter_returns_all() -> None:
    services = mod.normalize_project_services(_new_shape_projects(), "darkfac-core", "production")
    assert mod.select_services(services, None) == services
    assert mod.select_services(services, []) == services


def test_select_services_only_case_insensitive() -> None:
    services = mod.normalize_project_services(_new_shape_projects(), "darkfac-core", "production")
    selected = mod.select_services(services, ["DARKFAC-CLOUD", "darkhub"])
    assert [s.name for s in selected] == ["darkfac-cloud", "Darkhub"]


def test_select_services_unknown_name_raises_with_valid_list() -> None:
    services = mod.normalize_project_services(_new_shape_projects(), "darkfac-core", "production")
    with pytest.raises(mod.DokployUsageError) as excinfo:
        mod.select_services(services, ["darkfac-cloud", "totally-bogus-service"])
    message = str(excinfo.value)
    assert "totally-bogus-service" in message
    assert "darkfac-cloud" in message  # valid names listed for the user


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_resolve_credentials_missing_both_raises_usage_error_without_leaking() -> None:
    with pytest.raises(mod.DokployUsageError) as excinfo:
        mod.resolve_credentials({}, registry_reader=_no_registry)
    message = str(excinfo.value)
    assert "DOKPLOY_API_URL" in message
    assert "DOKPLOY_API_KEY" in message
    assert SENTINEL_KEY not in message


def test_resolve_credentials_env_takes_precedence_over_registry() -> None:
    calls: List[str] = []

    def registry_reader(name: str) -> Optional[str]:
        calls.append(name)
        return "should-not-be-used"

    url, key = mod.resolve_credentials(
        {"DOKPLOY_API_URL": "https://dokploy.ggcampos.com/", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=registry_reader,
    )
    assert url == "https://dokploy.ggcampos.com"  # trailing slash stripped
    assert key == SENTINEL_KEY
    assert calls == []  # registry never consulted when env already has both


def test_resolve_credentials_falls_back_to_registry_when_env_missing() -> None:
    def registry_reader(name: str) -> Optional[str]:
        return {"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY}.get(name)

    url, key = mod.resolve_credentials({}, registry_reader=registry_reader)
    assert url == "https://dokploy.ggcampos.com"
    assert key == SENTINEL_KEY


def test_read_windows_user_env_returns_none_off_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        pytest.skip("this test targets the non-win32 short-circuit")
    assert mod._read_windows_user_env("DOKPLOY_API_URL") is None


def test_read_windows_user_env_mocks_winreg_on_any_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercises the winreg code path deterministically (independent of
    whether this host is Windows, and independent of whatever the Owner's
    own machine actually has configured) by forcing sys.platform and
    injecting a fake `winreg` module."""

    class _FakeKey:
        def __enter__(self) -> "_FakeKey":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

    class _FakeWinreg:
        HKEY_CURRENT_USER = object()

        @staticmethod
        def OpenKey(_hive: Any, _subkey: str) -> _FakeKey:
            return _FakeKey()

        @staticmethod
        def QueryValueEx(_key: _FakeKey, name: str) -> Tuple[str, int]:
            if name == "DOKPLOY_API_URL":
                return "https://dokploy.ggcampos.com", 1
            raise FileNotFoundError(name)

    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg())
    try:
        assert mod._read_windows_user_env("DOKPLOY_API_URL") == "https://dokploy.ggcampos.com"
        assert mod._read_windows_user_env("DOES_NOT_EXIST") is None
    finally:
        sys.modules.pop("winreg", None)


# ---------------------------------------------------------------------------
# Deploy / status request shaping
# ---------------------------------------------------------------------------


def test_deploy_request_for_compose() -> None:
    svc = mod.Service(name="darkfac-cloud", kind="compose", service_id="compose_cloud")
    method, path, body = mod.deploy_request_for(svc)
    assert (method, path) == ("POST", "/api/compose.deploy")
    assert body == {"composeId": "compose_cloud"}


def test_deploy_request_for_application() -> None:
    svc = mod.Service(name="darkfac-canary", kind="application", service_id="app_canary")
    method, path, body = mod.deploy_request_for(svc)
    assert (method, path) == ("POST", "/api/application.deploy")
    assert body == {"applicationId": "app_canary"}


def test_status_request_for_compose_and_application() -> None:
    compose = mod.Service(name="darkfac-cloud", kind="compose", service_id="compose_cloud")
    app = mod.Service(name="darkfac-canary", kind="application", service_id="app_canary")
    assert mod.status_request_for(compose) == ("GET", "/api/compose.one?composeId=compose_cloud")
    assert mod.status_request_for(app) == ("GET", "/api/application.one?applicationId=app_canary")


# ---------------------------------------------------------------------------
# latest_deployment
# ---------------------------------------------------------------------------


def test_latest_deployment_none_when_no_history() -> None:
    assert mod.latest_deployment({}) is None
    assert mod.latest_deployment({"deployments": []}) is None


def test_latest_deployment_picks_max_created_at_regardless_of_order() -> None:
    payload = {
        "deployments": [
            {"deploymentId": "d2", "status": "done", "title": "second", "createdAt": "2026-09-12T10:15:00.000Z"},
            {"deploymentId": "d1", "status": "error", "title": "first", "createdAt": "2026-09-12T09:54:00.000Z"},
        ]
    }
    latest = mod.latest_deployment(payload)
    assert latest is not None
    assert latest.deployment_id == "d2"
    assert latest.title == "second"


def test_latest_deployment_falls_back_to_list_order_without_timestamps() -> None:
    payload = {"deployments": [{"deploymentId": "d1", "status": "done"}, {"deploymentId": "d2", "status": "done"}]}
    latest = mod.latest_deployment(payload)
    assert latest is not None
    assert latest.deployment_id == "d2"


# ---------------------------------------------------------------------------
# evaluate_wait_state
# ---------------------------------------------------------------------------


def _deployment(deployment_id: str, status: str, created_at: str) -> mod.Deployment:
    return mod.Deployment(
        deployment_id=deployment_id, status=status, title="t", created_at=mod._parse_iso8601(created_at)
    )


def test_evaluate_wait_state_continue_while_old_deployment_unchanged() -> None:
    baseline = _deployment("d1", "done", "2026-09-12T09:00:00Z")
    outcome, _ = mod.evaluate_wait_state(baseline, baseline, elapsed_seconds=1.0, timeout_seconds=900.0)
    assert outcome == mod.WaitOutcome.CONTINUE


def test_evaluate_wait_state_continue_while_new_deployment_in_progress() -> None:
    baseline = _deployment("d1", "done", "2026-09-12T09:00:00Z")
    current = _deployment("d2", "running", "2026-09-12T09:05:00Z")
    outcome, considered = mod.evaluate_wait_state(baseline, current, elapsed_seconds=1.0, timeout_seconds=900.0)
    assert outcome == mod.WaitOutcome.CONTINUE
    assert considered is current


def test_evaluate_wait_state_done_on_new_deployment() -> None:
    baseline = _deployment("d1", "done", "2026-09-12T09:00:00Z")
    current = _deployment("d2", "done", "2026-09-12T09:05:00Z")
    outcome, considered = mod.evaluate_wait_state(baseline, current, elapsed_seconds=1.0, timeout_seconds=900.0)
    assert outcome == mod.WaitOutcome.DONE
    assert considered is current


def test_evaluate_wait_state_error_on_new_deployment() -> None:
    baseline = _deployment("d1", "done", "2026-09-12T09:00:00Z")
    current = _deployment("d2", "error", "2026-09-12T09:05:00Z")
    outcome, _ = mod.evaluate_wait_state(baseline, current, elapsed_seconds=1.0, timeout_seconds=900.0)
    assert outcome == mod.WaitOutcome.ERROR


def test_evaluate_wait_state_baseline_none_new_deployment_done() -> None:
    current = _deployment("d1", "done", "2026-09-12T09:00:00Z")
    outcome, _ = mod.evaluate_wait_state(None, current, elapsed_seconds=0.0, timeout_seconds=900.0)
    assert outcome == mod.WaitOutcome.DONE


def test_evaluate_wait_state_timeout_when_still_old_deployment() -> None:
    baseline = _deployment("d1", "done", "2026-09-12T09:00:00Z")
    outcome, _ = mod.evaluate_wait_state(baseline, baseline, elapsed_seconds=901.0, timeout_seconds=900.0)
    assert outcome == mod.WaitOutcome.TIMEOUT


def test_evaluate_wait_state_timeout_when_new_deployment_stuck_in_progress() -> None:
    baseline = _deployment("d1", "done", "2026-09-12T09:00:00Z")
    current = _deployment("d2", "running", "2026-09-12T09:05:00Z")
    outcome, _ = mod.evaluate_wait_state(baseline, current, elapsed_seconds=901.0, timeout_seconds=900.0)
    assert outcome == mod.WaitOutcome.TIMEOUT


# ---------------------------------------------------------------------------
# wait_for_service (fake clock/sleep + queued transport)
# ---------------------------------------------------------------------------


def test_wait_for_service_detects_new_deployment_and_stops_on_done() -> None:
    svc = mod.Service(name="darkfac-cloud", kind="compose", service_id="compose_cloud")
    baseline = _deployment("d1", "done", "2026-09-12T09:00:00Z")
    transport = QueueTransport()
    transport.program_get(
        "/api/compose.one?composeId=compose_cloud",
        [
            {"deployments": [{"deploymentId": "d1", "status": "done", "title": "old", "createdAt": "2026-09-12T09:00:00Z"}]},
            {"deployments": [{"deploymentId": "d1", "status": "done", "title": "old", "createdAt": "2026-09-12T09:00:00Z"}]},
            {"deployments": [{"deploymentId": "d2", "status": "done", "title": "new", "createdAt": "2026-09-12T09:10:00Z"}]},
        ],
    )
    clock = FakeClock()
    result = mod.wait_for_service(
        transport, svc, baseline, timeout_seconds=900.0, poll_interval_seconds=5.0,
        sleep_fn=clock.sleep, clock_fn=clock.now,
    )
    assert result.outcome == mod.WaitOutcome.DONE
    assert result.deployment is not None
    assert result.deployment.deployment_id == "d2"
    assert result.deployment.title == "new"
    # Two CONTINUE ticks before the DONE tick -> two sleeps.
    assert len(clock.sleep_calls) == 2


def test_wait_for_service_stops_on_error() -> None:
    svc = mod.Service(name="darkfac-canary", kind="application", service_id="app_canary")
    baseline = _deployment("d1", "done", "2026-09-12T09:00:00Z")
    transport = QueueTransport()
    transport.program_get(
        "/api/application.one?applicationId=app_canary",
        [{"deployments": [{"deploymentId": "d2", "status": "error", "title": "boom", "createdAt": "2026-09-12T09:10:00Z"}]}],
    )
    clock = FakeClock()
    result = mod.wait_for_service(
        transport, svc, baseline, timeout_seconds=900.0, poll_interval_seconds=5.0,
        sleep_fn=clock.sleep, clock_fn=clock.now,
    )
    assert result.outcome == mod.WaitOutcome.ERROR
    assert result.deployment is not None
    assert result.deployment.title == "boom"


def test_wait_for_service_times_out_when_stuck_in_progress() -> None:
    svc = mod.Service(name="darkfac-cloud", kind="compose", service_id="compose_cloud")
    baseline = _deployment("d1", "done", "2026-09-12T09:00:00Z")
    transport = QueueTransport()
    transport.program_get(
        "/api/compose.one?composeId=compose_cloud",
        [{"deployments": [{"deploymentId": "d2", "status": "running", "title": "stuck", "createdAt": "2026-09-12T09:10:00Z"}]}],
    )
    clock = FakeClock()
    result = mod.wait_for_service(
        transport, svc, baseline, timeout_seconds=9.0, poll_interval_seconds=5.0,
        sleep_fn=clock.sleep, clock_fn=clock.now,
    )
    assert result.outcome == mod.WaitOutcome.TIMEOUT
    assert clock.now() >= 9.0


# ---------------------------------------------------------------------------
# main(): end-to-end with QueueTransport
# ---------------------------------------------------------------------------


def _make_transport_with_full_project(project_path: str = "/api/project.all") -> QueueTransport:
    transport = QueueTransport()
    transport.program_get(project_path, [_new_shape_projects()])
    return transport


def _program_status(
    transport: QueueTransport, path: str, deployments_sequence: Sequence[List[Dict[str, Any]]]
) -> None:
    transport.program_get(path, [{"deployments": d} for d in deployments_sequence])


def test_main_missing_credentials_exit_2_no_leak() -> None:
    out, err = io.StringIO(), io.StringIO()
    exit_code = mod.main(
        ["--list"],
        env={},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: (_ for _ in ()).throw(AssertionError("transport must not be built")),
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_USAGE_ERROR
    combined = out.getvalue() + err.getvalue()
    assert "DOKPLOY_API_URL" in combined
    assert "DOKPLOY_API_KEY" in combined
    assert SENTINEL_KEY not in combined


def test_main_list_prints_services_and_exits_0() -> None:
    transport = _make_transport_with_full_project()
    for path in (
        "/api/compose.one?composeId=compose_cloud",
        "/api/compose.one?composeId=compose_hub",
        "/api/compose.one?composeId=compose_n8n_darkfac",
        "/api/application.one?applicationId=app_canary",
    ):
        transport.program_get(path, [{"deployments": [{"deploymentId": "d1", "status": "done", "title": "ok", "createdAt": "2026-09-12T09:00:00Z"}]}])

    out, err = io.StringIO(), io.StringIO()
    exit_code = mod.main(
        ["--list"],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: transport,
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_OK
    listing = out.getvalue()
    assert "darkfac-cloud" in listing
    assert "darkfac-canary" in listing
    assert "darkfac-n8n" in listing  # the real darkfac-core compose service
    assert "compose_other_n8n" not in listing  # never the other project's decoy "n8n"
    assert "My First Project" not in listing
    assert not transport.post_calls  # --list never triggers a deploy
    assert SENTINEL_KEY not in listing


def test_main_dry_run_does_not_trigger_and_exits_0() -> None:
    transport = _make_transport_with_full_project()
    out, err = io.StringIO(), io.StringIO()
    exit_code = mod.main(
        ["--dry-run", "--only", "darkfac-cloud"],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: transport,
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_OK
    assert "darkfac-cloud" in out.getvalue()
    assert not transport.post_calls


def test_main_only_unknown_name_exit_2() -> None:
    transport = _make_transport_with_full_project()
    out, err = io.StringIO(), io.StringIO()
    exit_code = mod.main(
        ["--list", "--only", "not-a-real-service"],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: transport,
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_USAGE_ERROR
    assert "not-a-real-service" in err.getvalue()


def test_main_deploy_and_wait_all_done_exit_0() -> None:
    transport = _make_transport_with_full_project()
    for path in (
        "/api/compose.one?composeId=compose_cloud",
        "/api/compose.one?composeId=compose_hub",
        "/api/compose.one?composeId=compose_n8n_darkfac",
        "/api/application.one?applicationId=app_canary",
    ):
        _program_status(
            transport,
            path,
            [
                [{"deploymentId": "old", "status": "done", "title": "old", "createdAt": "2026-09-12T09:00:00Z"}],
                [{"deploymentId": "new", "status": "done", "title": "new deploy", "createdAt": "2026-09-12T09:10:00Z"}],
            ],
        )

    out, err = io.StringIO(), io.StringIO()
    clock = FakeClock()
    exit_code = mod.main(
        [],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: transport,
        sleep_fn=clock.sleep,
        clock_fn=clock.now,
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_OK
    # 4 services triggered: compose.deploy x3 + application.deploy x1.
    assert len(transport.post_calls) == 4
    assert {c[1] for c in transport.post_calls} == {"/api/compose.deploy", "/api/application.deploy"}
    report = out.getvalue()
    assert "status=done" in report
    assert SENTINEL_KEY not in report


def test_main_deploy_one_service_errors_exit_1() -> None:
    transport = _make_transport_with_full_project()
    ok_deployments = [
        [{"deploymentId": "old", "status": "done", "title": "old", "createdAt": "2026-09-12T09:00:00Z"}],
        [{"deploymentId": "new", "status": "done", "title": "new", "createdAt": "2026-09-12T09:10:00Z"}],
    ]
    for path in (
        "/api/compose.one?composeId=compose_hub",
        "/api/compose.one?composeId=compose_n8n_darkfac",
        "/api/application.one?applicationId=app_canary",
    ):
        _program_status(transport, path, ok_deployments)
    _program_status(
        transport,
        "/api/compose.one?composeId=compose_cloud",
        [
            [{"deploymentId": "old", "status": "done", "title": "old", "createdAt": "2026-09-12T09:00:00Z"}],
            [{"deploymentId": "new", "status": "error", "title": "broke", "createdAt": "2026-09-12T09:10:00Z"}],
        ],
    )

    out, err = io.StringIO(), io.StringIO()
    clock = FakeClock()
    exit_code = mod.main(
        [],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: transport,
        sleep_fn=clock.sleep,
        clock_fn=clock.now,
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_DEPLOY_FAILED
    assert "darkfac-cloud" in out.getvalue()
    assert "status=error" in out.getvalue()


def test_main_no_wait_triggers_and_exits_0_without_polling() -> None:
    transport = _make_transport_with_full_project()
    for path in (
        "/api/compose.one?composeId=compose_cloud",
        "/api/compose.one?composeId=compose_hub",
        "/api/compose.one?composeId=compose_n8n_darkfac",
        "/api/application.one?applicationId=app_canary",
    ):
        transport.program_get(path, [{"deployments": []}])

    out, err = io.StringIO(), io.StringIO()
    exit_code = mod.main(
        ["--no-wait"],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: transport,
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_OK
    assert len(transport.post_calls) == 4
    assert "Not waiting" in out.getvalue()


def test_main_only_scopes_deploy_to_selected_services() -> None:
    transport = _make_transport_with_full_project()
    _program_status(
        transport,
        "/api/compose.one?composeId=compose_cloud",
        [
            [{"deploymentId": "old", "status": "done", "title": "old", "createdAt": "2026-09-12T09:00:00Z"}],
            [{"deploymentId": "new", "status": "done", "title": "new", "createdAt": "2026-09-12T09:10:00Z"}],
        ],
    )
    out, err = io.StringIO(), io.StringIO()
    clock = FakeClock()
    exit_code = mod.main(
        ["--only", "darkfac-cloud"],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: transport,
        sleep_fn=clock.sleep,
        clock_fn=clock.now,
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_OK
    assert transport.post_calls == [("POST", "/api/compose.deploy", {"composeId": "compose_cloud"})]


# ---------------------------------------------------------------------------
# make_urllib_transport: HTTP error sanitization (no real network -- urlopen
# is monkeypatched to a fake that raises synthetically, never opening a
# socket; the session-wide network block in conftest.py is simply shadowed
# for the duration of this one call).
# ---------------------------------------------------------------------------


def test_urllib_transport_sanitizes_http_error_body(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def fake_urlopen(request: Any, timeout: float | None = None) -> Any:
        assert request.get_header("X-api-key") == SENTINEL_KEY
        body = f"Unauthorized: bad key {SENTINEL_KEY} rejected".encode("utf-8")
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(body))

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    transport = mod.make_urllib_transport("https://dokploy.ggcampos.com", SENTINEL_KEY)

    with pytest.raises(mod.DokployUsageError) as excinfo:
        transport("GET", "/api/project.all", None)
    message = str(excinfo.value)
    assert "401" in message
    assert SENTINEL_KEY not in message
    assert "***" in message


def test_urllib_transport_sanitizes_url_error_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    def fake_urlopen(request: Any, timeout: float | None = None) -> Any:
        raise urllib.error.URLError(f"connection refused, key={SENTINEL_KEY}")

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    transport = mod.make_urllib_transport("https://dokploy.ggcampos.com", SENTINEL_KEY)

    with pytest.raises(mod.DokployUsageError) as excinfo:
        transport("POST", "/api/compose.deploy", {"composeId": "x"})
    message = str(excinfo.value)
    assert SENTINEL_KEY not in message


def test_urllib_transport_success_parses_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def read(self) -> bytes:
            return b'[{"name": "darkfac-core"}]'

    def fake_urlopen(request: Any, timeout: float | None = None) -> Any:
        assert request.full_url == "https://dokploy.ggcampos.com/api/project.all"
        return FakeResponse()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    transport = mod.make_urllib_transport("https://dokploy.ggcampos.com", SENTINEL_KEY)
    result = transport("GET", "/api/project.all", None)
    assert result == [{"name": "darkfac-core"}]


# ---------------------------------------------------------------------------
# fetch_project_all / fetch_service_status error shape handling
# ---------------------------------------------------------------------------


def test_fetch_project_all_rejects_unexpected_shape() -> None:
    transport = QueueTransport()
    transport.program_get("/api/project.all", [{"unexpected": "shape"}])
    with pytest.raises(mod.DokployUsageError):
        mod.fetch_project_all(transport)


def test_fetch_service_status_rejects_unexpected_shape() -> None:
    transport = QueueTransport()
    svc = mod.Service(name="darkfac-cloud", kind="compose", service_id="compose_cloud")
    transport.program_get("/api/compose.one?composeId=compose_cloud", [["not", "a", "dict"]])
    with pytest.raises(mod.DokployUsageError):
        mod.fetch_service_status(transport, svc)


def test_resolve_services_no_matching_project_raises_usage_error() -> None:
    transport = QueueTransport()
    transport.program_get("/api/project.all", [_new_shape_projects()])
    with pytest.raises(mod.DokployUsageError):
        mod._resolve_services(transport, "not-a-project", "production", None)


def test_main_title_match_uses_commit_subject_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dokploy titles git deployments with the full commit message (subject +
    body); comparing it whole against origin/main's subject printed a false
    '[does not match local origin/main]' on a correct deploy."""
    subject = "fix(harness): subject line"
    full_title = subject + "\n\nBody paragraph.\n\nCo-Authored-By: X <x@example.com>"
    monkeypatch.setattr(mod, "get_local_origin_main_subject", lambda: subject)
    transport = _make_transport_with_full_project()
    _program_status(
        transport,
        "/api/compose.one?composeId=compose_cloud",
        [
            [{"deploymentId": "old", "status": "done", "title": "old", "createdAt": "2026-09-12T09:00:00Z"}],
            [{"deploymentId": "new", "status": "done", "title": full_title, "createdAt": "2026-09-12T09:10:00Z"}],
        ],
    )
    out, err = io.StringIO(), io.StringIO()
    clock = FakeClock()
    exit_code = mod.main(
        ["--only", "darkfac-cloud"],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: transport,
        sleep_fn=clock.sleep,
        clock_fn=clock.now,
        stdout=out,
        stderr=err,
    )
    report = out.getvalue()
    assert exit_code == mod.EXIT_OK
    assert "[matches local origin/main]" in report
    assert "does not match" not in report
    assert "Body paragraph" not in report


def test_main_executes_autonomous_post_deploy_backup_on_success() -> None:
    transport = _make_transport_with_full_project()
    _program_status(
        transport,
        "/api/compose.one?composeId=compose_cloud",
        [
            [{"deploymentId": "old", "status": "done", "title": "old", "createdAt": "2026-09-12T09:00:00Z"}],
            [{"deploymentId": "new", "status": "done", "title": "new", "createdAt": "2026-09-12T09:10:00Z"}],
        ],
    )
    backup_calls: list[str] = []

    def mock_backup_runner(project_id: str = "darkfac") -> dict[str, Any]:
        backup_calls.append(project_id)
        return {"snapshot_id": "snp_mock_123", "drill_verified": True}

    out, err = io.StringIO(), io.StringIO()
    clock = FakeClock()
    exit_code = mod.main(
        ["--only", "darkfac-cloud"],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: transport,
        sleep_fn=clock.sleep,
        clock_fn=clock.now,
        backup_runner=mock_backup_runner,
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_OK
    assert backup_calls == ["darkfac"]
    assert "[AUTONOMOUS POST-DEPLOY BACKUP] Done: snapshot=snp_mock_123, drill_verified=True" in out.getvalue()


def test_main_skips_autonomous_post_deploy_backup_with_flag() -> None:
    transport = _make_transport_with_full_project()
    _program_status(
        transport,
        "/api/compose.one?composeId=compose_cloud",
        [
            [{"deploymentId": "old", "status": "done", "title": "old", "createdAt": "2026-09-12T09:00:00Z"}],
            [{"deploymentId": "new", "status": "done", "title": "new", "createdAt": "2026-09-12T09:10:00Z"}],
        ],
    )
    backup_calls: list[str] = []

    def mock_backup_runner(project_id: str = "darkfac") -> dict[str, Any]:
        backup_calls.append(project_id)
        return {"snapshot_id": "snp_mock_123", "drill_verified": True}

    out, err = io.StringIO(), io.StringIO()
    clock = FakeClock()
    exit_code = mod.main(
        ["--only", "darkfac-cloud", "--skip-backup"],
        env={"DOKPLOY_API_URL": "https://dokploy.ggcampos.com", "DOKPLOY_API_KEY": SENTINEL_KEY},
        registry_reader=_no_registry,
        transport_factory=lambda url, key: transport,
        sleep_fn=clock.sleep,
        clock_fn=clock.now,
        backup_runner=mock_backup_runner,
        stdout=out,
        stderr=err,
    )
    assert exit_code == mod.EXIT_OK
    assert backup_calls == []
    assert "[AUTONOMOUS POST-DEPLOY BACKUP]" not in out.getvalue()

