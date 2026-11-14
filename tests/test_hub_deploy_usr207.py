"""USR-207: Hub availability during merges (classifier, single deploy, watchdog, frontend retry)."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from core.infra import deploy_classifier as dc
from scripts import dokploy_redeploy as mod


@pytest.fixture(autouse=True)
def _legacy_build_image_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # USR-365: ghcr is the default now; these scenarios exercise the legacy build layout.
    monkeypatch.setenv("DARKHUB_IMAGE_SOURCE", "build")

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "hub" / "frontend"
A, B = "a" * 40, "b" * 40
# Fixed instant injected through mod._utcnow; never compared with the real clock.
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- classifier


@pytest.mark.parametrize(
    "paths,rebuild",
    [
        (["tests/test_a.py", ".github/workflows/ci.yml", "README.md"], False),
        (["docs/x.md"], True),
        ([".factory/demands/demands.json"], True),
        (["hub/frontend/app.js"], True),
        (["deploy/dokploy/docker-compose.hub.yml"], True),
        ([".factory/config/subscription_prices.json"], True),
        (["requirements.txt"], True),
        (["scripts/dokploy_redeploy.py"], True),
        ([], True),
        (None, True),
    ],
)
def test_needs_rebuild(paths: Optional[List[str]], rebuild: bool) -> None:
    assert dc.needs_rebuild(paths) is rebuild


def test_classify_same_sha_never_rebuilds_and_windows_paths_normalised() -> None:
    assert dc.classify(A, A, None) == (False, dc.REASON_SAME_SHA)
    assert dc.classify(A, B, ["core\\x.py"])[0] is True
    assert dc.classify(A, B, ["tests\\test_a.py"])[0] is False


def test_run_hub_only_merge_is_data_only() -> None:
    assert dc.classify(A, B, ["run_hub.py"]) == (False, dc.REASON_DATA_ONLY)


# USR-515: tip diff and accumulated image lag are separate decisions. No network.


RUNTIME = "c" * 40
OLDER = "d" * 40
BEHIND = "e" * 40
AFTER = "f" * 40


def _lag(*commits: dc.CommitTouch, live: str = A, target: str = B) -> dc.ImageLagDecision:
    return dc.classify_image_lag(live_sha=live, target_sha=target, commits=commits)


def test_agents_tip_with_pending_runtime_pins_that_ancestor() -> None:
    """Tip is only AGENTS.md; one core commit between live and the tip is the pin."""
    commits = (
        dc.CommitTouch(BEHIND, ("core/old.py",)),
        dc.CommitTouch(A, ()),
        dc.CommitTouch(RUNTIME, ("core/pending.py",)),
        dc.CommitTouch(B, ("AGENTS.md",)),
        dc.CommitTouch(AFTER, ("core/future.py",)),
    )
    decision = dc.classify_image_lag(live_sha=A, target_sha=B, commits=commits)
    order = [commit.sha for commit in commits]
    patterns = dc.hub_image_paths()

    assert decision.action == dc.ACTION_PIN_PUBLISHED
    assert decision.pin_sha == RUNTIME
    assert decision.published_sha == RUNTIME
    assert order.index(A) < order.index(decision.published_sha) < order.index(B)
    assert decision.published_sha not in {BEHIND, AFTER, A, B}
    assert decision.reach == ("AGENTS.md",)
    assert not dc.touches_hub_image(decision.reach, patterns)
    assert dc.touches_hub_image(["core/pending.py"], patterns)


def test_two_image_ancestors_pin_the_latest_one() -> None:
    decision = _lag(
        dc.CommitTouch(A, ()),
        dc.CommitTouch(OLDER, ("core/first.py",)),
        dc.CommitTouch(RUNTIME, ("hub/backend/main.py", "tests/test_a.py")),
        dc.CommitTouch(B, ("AGENTS.md",)),
    )
    assert decision.action == dc.ACTION_PIN_PUBLISHED
    assert decision.published_sha == RUNTIME
    assert decision.reach == ("AGENTS.md",)
    assert not dc.touches_hub_image(decision.reach, dc.hub_image_paths())


def test_agents_only_interval_skips_and_reports_live_as_published() -> None:
    decision = _lag(
        dc.CommitTouch(A, ()),
        dc.CommitTouch(B, ("AGENTS.md",)),
    )
    patterns = dc.hub_image_paths()
    assert decision.action == dc.ACTION_SKIP
    assert decision.pin_sha is None
    assert decision.published_sha == A
    assert decision.reach == ("AGENTS.md",)
    assert not dc.touches_hub_image(decision.reach, patterns)


def test_image_commit_behind_live_is_not_selected_as_pin() -> None:
    decision = _lag(
        dc.CommitTouch(BEHIND, ("core/old.py",)),
        dc.CommitTouch(A, ("README.md",)),
        dc.CommitTouch(B, ("AGENTS.md",)),
    )
    assert decision.action == dc.ACTION_SKIP
    assert decision.pin_sha is None
    assert decision.published_sha == A
    assert decision.published_sha != BEHIND
    assert "core/old.py" not in decision.reach
    assert not dc.touches_hub_image(decision.reach, dc.hub_image_paths())


def test_absent_live_selects_no_pin_even_when_target_publishes() -> None:
    decision = dc.classify_image_lag(
        live_sha=None,
        target_sha=B,
        commits=(
            dc.CommitTouch(BEHIND, ("core/old.py",)),
            dc.CommitTouch(B, ("core/pending.py",)),
        ),
    )
    assert decision.action == dc.ACTION_HOLD
    assert decision.pin_sha is None
    assert decision.published_sha is None
    assert decision.published_sha != BEHIND


def test_target_behind_live_is_not_a_pin() -> None:
    decision = _lag(
        dc.CommitTouch(BEHIND, ("core/old.py",)),
        dc.CommitTouch(A, ()),
        live=A,
        target=BEHIND,
    )
    assert decision.action == dc.ACTION_HOLD
    assert decision.pin_sha is None
    assert decision.published_sha == A
    assert decision.published_sha != BEHIND


def test_image_target_pins_the_target_sha() -> None:
    decision = _lag(
        dc.CommitTouch(A, ()),
        dc.CommitTouch(OLDER, ("core/old.py",)),
        dc.CommitTouch(B, ("requirements.txt",)),
    )
    assert decision.action == dc.ACTION_PIN_TARGET
    assert decision.pin_sha == B
    assert decision.published_sha == B
    assert decision.reach == ()


def test_image_lag_follows_hub_image_paths_and_does_not_query_ghcr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def patterns(workflow: Optional[Path] = None) -> list[str]:
        calls.append("paths")
        return ["special.txt"]

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("consulta de rede")

    monkeypatch.setattr(dc, "hub_image_paths", patterns)
    monkeypatch.setattr(mod, "wait_for_ghcr_tag", boom)
    monkeypatch.setattr(mod, "fetch_darkhub_health", boom)
    monkeypatch.setattr(mod, "probe_deployed_sha", boom)

    outside = _lag(
        dc.CommitTouch(A, ()),
        dc.CommitTouch(RUNTIME, ("core/a.py",)),
        dc.CommitTouch(B, ("AGENTS.md",)),
    )
    assert calls
    assert outside.action == dc.ACTION_SKIP
    assert outside.published_sha == A

    def core_patterns(workflow: Optional[Path] = None) -> list[str]:
        calls.append("core")
        return ["core/**"]

    monkeypatch.setattr(dc, "hub_image_paths", core_patterns)
    pinned = _lag(
        dc.CommitTouch(A, ()),
        dc.CommitTouch(RUNTIME, ("core/a.py",)),
        dc.CommitTouch(B, ("AGENTS.md",)),
    )
    assert pinned.action == dc.ACTION_PIN_PUBLISHED
    assert pinned.published_sha == RUNTIME
    assert pinned.reach == ("AGENTS.md",)

    monkeypatch.setattr(dc, "hub_image_paths", lambda workflow=None: [])
    closed = _lag(
        dc.CommitTouch(A, ()),
        dc.CommitTouch(B, ("AGENTS.md",)),
    )
    assert closed.action == dc.ACTION_PIN_TARGET
    assert closed.published_sha == B


def test_availability_probe_schedule_uses_absolute_pythonw_and_safe_powershell_arguments() -> None:
    proposal = (ROOT / "docs" / "proposals" / "USR-207-zero-downtime.md").read_text(encoding="utf-8")
    section = proposal.split("### Agendar a sonda", maxsplit=1)[1].split("## 2.", maxsplit=1)[0]
    assert '$py = "C:\\Users\\guigc\\AppData\\Local\\Programs\\Python\\Python312\\pythonw.exe"' in section
    assert "Test-Path $py" in section and "throw" in section
    assert "New-ScheduledTaskAction -Execute $py" in section
    assert 'Register-ScheduledTask -TaskName "DarkFac-Hub-Availability"' in section
    assert "-WorkingDirectory \"C:\\dev\\DarkFac\"" in section
    assert "Start-Process" not in section
    assert "Invoke-Expression" not in section

# --------------------------------------------------------------------------- plan


def _dep(status: str, age_s: float, dep_id: str = "d1") -> mod.Deployment:
    return mod.Deployment(dep_id, status, "t", NOW - timedelta(seconds=age_s))


def test_plan_joins_inflight_autodeploy_instead_of_triggering_second() -> None:
    action, _ = mod.plan_darkhub_deploy(
        expected_sha=B, deployed_sha=A, baseline=_dep("running", 30),
        changed_paths_fn=lambda a, b: ["core/x.py"], now=NOW,
    )
    assert action == mod.DEPLOY_JOIN


def test_plan_stale_running_deployment_is_not_joined() -> None:
    action, _ = mod.plan_darkhub_deploy(
        expected_sha=B, deployed_sha=A, baseline=_dep("running", 3 * 3600),
        changed_paths_fn=lambda a, b: ["core/x.py"], now=NOW,
    )
    assert action == mod.DEPLOY_TRIGGER


def test_plan_skips_same_sha_and_data_only_but_force_triggers() -> None:
    base = _dep("done", 600)

    def plan(deployed: Optional[str], paths: Optional[List[str]], expected: str = B, force: bool = False) -> str:
        return mod.plan_darkhub_deploy(
            expected_sha=expected, deployed_sha=deployed, baseline=base,
            changed_paths_fn=lambda a, b: paths, now=NOW, force=force,
        )[0]

    assert plan(A, [], expected=A) == mod.DEPLOY_SKIP
    assert plan(A, ["tests/test_a.py"]) == mod.DEPLOY_SKIP
    assert plan(A, ["core/a.py"]) == mod.DEPLOY_TRIGGER
    assert plan(A, None) == mod.DEPLOY_TRIGGER  # unknown diff fails closed
    assert plan(None, []) == mod.DEPLOY_TRIGGER  # unknown deployed sha fails closed
    assert plan(A, [], expected=A, force=True) == mod.DEPLOY_TRIGGER


def test_is_deployment_inflight_requires_running_and_recent() -> None:
    assert mod.is_deployment_inflight(_dep("running", 10), NOW)
    assert not mod.is_deployment_inflight(_dep("done", 10), NOW)
    assert not mod.is_deployment_inflight(None, NOW)


# --------------------------------------------------------------------------- end to end with a fake Dokploy


class FakeDokploy:
    """Fake transport: `status_for(n)` returns the deployments list for the n-th compose.one read."""

    def __init__(self, status_for: Callable[[int], List[Dict[str, Any]]]) -> None:
        self.status_for = status_for
        self.reads = 0
        self.posts: List[str] = []

    def __call__(self, method: str, path: str, body: Any) -> Any:
        if method == "GET" and path == "/api/project.all":
            return [{
                "name": "darkfac-core",
                "environments": [{"name": "production", "compose": [
                    {"name": "Darkhub", "composeId": "compose_hub", "composeStatus": "done"}
                ], "applications": []}],
            }]
        if method == "GET":
            self.reads += 1
            return {"deployments": self.status_for(self.reads), "env": "K=v\n"}
        self.posts.append(path)
        return {}


def _d(dep_id: str, status: str, when: datetime) -> Dict[str, Any]:
    return {"deploymentId": dep_id, "status": status, "title": "t",
            "createdAt": when.isoformat().replace("+00:00", "Z")}


def _run(transport: FakeDokploy, monkeypatch: pytest.MonkeyPatch, deployed: Optional[str],
         changed: Optional[List[str]]) -> tuple[int, str]:
    monkeypatch.setattr(mod, "get_origin_main_sha", lambda: B)
    monkeypatch.setattr(
        mod, "get_origin_main_compose",
        lambda sha: (mod.REPO_ROOT / "deploy/dokploy/docker-compose.hub.yml").read_text(encoding="utf-8"),
    )
    monkeypatch.setattr(mod, "get_local_origin_main_subject", lambda: None)
    monkeypatch.setattr(mod, "probe_deployed_sha", lambda: deployed)
    monkeypatch.setattr(mod, "get_changed_paths", lambda a, b: changed)
    monkeypatch.setattr(mod, "_utcnow", lambda: NOW)
    ticks = iter(range(0, 100_000))
    out, err = io.StringIO(), io.StringIO()
    code = mod.main(
        ["--only", "Darkhub", "--skip-node-sync", "--skip-backup", "--skip-check-main", "--skip-disk-hygiene",
         "--timeout", "50", "--poll-interval", "1"],
        env={"DOKPLOY_API_URL": "https://dokploy.example", "DOKPLOY_API_KEY": "k", "DARKHUB_IMAGE_SOURCE": "build"},
        registry_reader=lambda _n: None, transport_factory=lambda u, k: transport,
        health_client=lambda: {"git_sha": B}, sleep_fn=lambda s: None, clock_fn=lambda: float(next(ticks)),
        stdout=out, stderr=err,
    )
    return code, out.getvalue() + err.getvalue()


def test_data_only_merge_triggers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    t = FakeDokploy(lambda n: [_d("old", "done", NOW - timedelta(hours=1))])
    code, text = _run(t, monkeypatch, A, ["tests/test_a.py", "README.md"])
    assert code == mod.EXIT_OK, text
    assert t.posts == []
    assert "deploy skipped" in text


@pytest.mark.parametrize(
    "runtime_path",
    [
        "core/infra/node_sync.py",
        "hub/backend/main.py",
        "scripts/dokploy_redeploy.py",
        "deploy/dokploy/docker-compose.hub.yml",
        "requirements.txt",
        "harness.config.json",
    ],
)
def test_runtime_change_triggers_exactly_one_deploy(
    monkeypatch: pytest.MonkeyPatch, runtime_path: str
) -> None:
    def status(n: int) -> List[Dict[str, Any]]:
        if n == 1:
            return [_d("old", "done", NOW - timedelta(hours=1))]
        return [_d("new", "done", NOW + timedelta(seconds=30))]

    t = FakeDokploy(status)
    code, text = _run(t, monkeypatch, A, [runtime_path])
    assert code == mod.EXIT_OK, text
    assert t.posts.count("/api/compose.deploy") == 1


def test_inflight_autodeploy_is_joined_not_retriggered(monkeypatch: pytest.MonkeyPatch) -> None:
    def status(n: int) -> List[Dict[str, Any]]:
        return [_d("auto", "running" if n < 3 else "done", NOW - timedelta(seconds=20))]

    t = FakeDokploy(status)
    code, text = _run(t, monkeypatch, A, ["core/x.py"])
    assert code == mod.EXIT_OK, text
    assert "joining in-flight" in text
    assert t.posts == []


# --------------------------------------------------------------------------- watchdog








# --------------------------------------------------------------------------- frontend (static)

# live.js belongs to the /live page (USR-208), which has its own polling/backoff; every other module uses hub_net.js.
RAW_FETCH_ALLOWED = {"hub_net.js", "live.js"}


def test_no_raw_fetch_outside_the_helper() -> None:
    offenders = []
    for js in sorted(FRONTEND.glob("*.js")):
        if js.name in RAW_FETCH_ALLOWED:
            continue
        for n, line in enumerate(js.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"(?<![\w.$])fetch\(", line) and not line.lstrip().startswith("//"):
                offenders.append(f"{js.name}:{n}")
    assert offenders == []


def test_helper_is_loaded_before_every_module_and_shows_banner_text() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="/static/([\w.]+)\?v=', html)
    assert scripts[0] == "hub_net.js"
    assert "Hub atualizando, reconectando..." in (FRONTEND / "hub_net.js").read_text(encoding="utf-8")


def test_hub_net_node_behaviour() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed on this runner")
    result = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "js" / "hub_net.test.cjs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=180, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_cloud_sha_is_pinned_by_the_factory_not_the_owner() -> None:
    posts: List[Any] = []
    svc = mod.Service(name="darkfac-cloud", kind="compose", service_id="compose_cloud")
    mod.pin_cloud_git_sha(lambda m, p, b=None: posts.append((m, p, b)), svc, {"env": "A=1\nDARKFAC_GIT_SHA=old\n"}, A)
    assert posts == [("POST", "/api/compose.update", {"composeId": "compose_cloud", "env": f"A=1\nDARKFAC_GIT_SHA={A}\n"})]
    with pytest.raises(mod.DokployUsageError):
        mod.pin_cloud_git_sha(lambda *a, **k: None, svc, {"env": ""}, None)
    with pytest.raises(mod.DokployUsageError):
        mod.pin_cloud_git_sha(lambda *a, **k: None, svc, {}, A)
