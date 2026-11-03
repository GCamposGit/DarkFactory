"""USR-207: Hub availability during merges (classifier, single deploy, watchdog, frontend retry)."""

from __future__ import annotations

import io
import os
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


def test_proposal_directs_to_run_the_install_script_instead_of_a_manual_snippet() -> None:
    proposal = (ROOT / "docs" / "proposals" / "USR-207-zero-downtime.md").read_text(encoding="utf-8")
    section = proposal.split("### Agendar a sonda", maxsplit=1)[1].split("## 2.", maxsplit=1)[0]
    assert "scripts\\install_availability_probe.ps1" in section
    assert "-Verify" in section


@pytest.mark.skipif(not shutil.which("powershell") and not shutil.which("pwsh"), reason="PowerShell indisponivel")
def test_install_availability_probe_dry_run_resolves_pythonw_outside_worktree(tmp_path: Path) -> None:
    shell = shutil.which("powershell") or shutil.which("pwsh")
    script = ROOT / "scripts" / "install_availability_probe.ps1"
    copy_dir = tmp_path / "repo" / "scripts"
    copy_dir.mkdir(parents=True)
    copy = copy_dir / "install_availability_probe.ps1"
    shutil.copy(script, copy)
    proc = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(copy), "-DryRun"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "DRY-RUN" in proc.stdout
    # DryRun now exits AFTER the pythonw.exe resolution (not before it), so this proves
    # the resolution actually ran instead of being skipped by an early exit.
    assert "pythonw.exe resolvido em" in proc.stdout


@pytest.mark.skipif(not shutil.which("powershell") and not shutil.which("pwsh"), reason="PowerShell indisponivel")
@pytest.mark.parametrize("parts", [(".worktrees", "x"), (".claude", "worktrees", "x"), (".codex", "worktrees", "X")])
def test_install_availability_probe_refuses_inside_worktree_dirs(tmp_path: Path, parts: tuple) -> None:
    shell = shutil.which("powershell") or shutil.which("pwsh")
    script = ROOT / "scripts" / "install_availability_probe.ps1"
    copy_dir = tmp_path.joinpath(*parts, "scripts")
    copy_dir.mkdir(parents=True)
    copy = copy_dir / "install_availability_probe.ps1"
    shutil.copy(script, copy)
    proc = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(copy), "-DryRun"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode != 0
    assert "RECUSADO" in proc.stderr
    assert "registrada" not in proc.stdout


@pytest.mark.skipif(not shutil.which("powershell") and not shutil.which("pwsh"), reason="PowerShell indisponivel")
def test_install_availability_probe_verify_reports_unregistered_task_and_exits_nonzero(tmp_path: Path) -> None:
    # Goes through the mock harness (not the real script) because the real -Verify path calls
    # Get-ScheduledTask, which belongs to the Windows-only ScheduledTasks module: unavailable
    # under pwsh on Linux CI runners, so calling it directly fails closed for the wrong reason.
    proc = _run_probe_harness(
        tmp_path, env={}, extra_args=["-TaskName", "DarkFac-Hub-Availability-Nonexistent-Test-Double", "-Verify"],
    )
    assert proc.returncode != 0
    assert "nao esta registrada" in proc.stdout


_HARNESS = ROOT / "tests" / "ps" / "usr448_probe_mock_harness.ps1"
_INSTALL_SCRIPT = ROOT / "scripts" / "install_availability_probe.ps1"


def _run_probe_harness(tmp_path: Path, env: Dict[str, str], extra_args: Optional[List[str]] = None) -> subprocess.CompletedProcess:
    """Runs install_availability_probe.ps1 under tests/ps/usr448_probe_mock_harness.ps1, which fakes
    Get-Command/Register-ScheduledTask/Get-ScheduledTask so the assertions below exercise the real
    fail-closed and action-wiring logic without touching the real Windows Task Scheduler."""
    shell = shutil.which("powershell") or shutil.which("pwsh")
    copy_dir = tmp_path / "repo" / "scripts"
    copy_dir.mkdir(parents=True, exist_ok=True)
    copy = copy_dir / "install_availability_probe.ps1"
    shutil.copy(_INSTALL_SCRIPT, copy)
    full_env = dict(os.environ)
    full_env.update(env)
    args = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(_HARNESS), "-ScriptPath", str(copy)]
    args.extend(extra_args or [])
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=120, env=full_env)


@pytest.mark.skipif(not shutil.which("powershell") and not shutil.which("pwsh"), reason="PowerShell indisponivel")
def test_install_availability_probe_fails_closed_without_pythonw_or_python(tmp_path: Path) -> None:
    proc = _run_probe_harness(tmp_path, env={})
    assert proc.returncode == 1
    assert "nem pythonw.exe nem python.exe" in proc.stderr
    assert "CALL:" not in proc.stdout


@pytest.mark.skipif(not shutil.which("powershell") and not shutil.which("pwsh"), reason="PowerShell indisponivel")
def test_install_availability_probe_fails_closed_when_pythonw_missing_beside_python(tmp_path: Path) -> None:
    proc = _run_probe_harness(tmp_path, env={"FAKE_PYTHON_PATH": r"C:\FakePython\python.exe"})
    assert proc.returncode == 1
    assert "pythonw.exe nao encontrado em" in proc.stderr
    assert "CALL:" not in proc.stdout


@pytest.mark.skipif(not shutil.which("powershell") and not shutil.which("pwsh"), reason="PowerShell indisponivel")
def test_install_availability_probe_wires_absolute_action_and_registers_with_force(tmp_path: Path) -> None:
    fake_pythonw = str(tmp_path / "fakepy" / "pythonw.exe")
    proc = _run_probe_harness(tmp_path, env={"FAKE_PYTHONW_PATH": fake_pythonw}, extra_args=["-TaskName", "Fake-Task"])
    assert proc.returncode == 0, proc.stderr
    action_line = next(line for line in proc.stdout.splitlines() if line.startswith("CALL:New-ScheduledTaskAction:"))
    assert f"Execute={fake_pythonw}" in action_line
    assert "Argument=-m core.infra.availability_probe" in action_line
    assert f"WorkingDirectory={tmp_path / 'repo'}" in action_line
    register_line = next(line for line in proc.stdout.splitlines() if line.startswith("CALL:Register-ScheduledTask:"))
    assert "TaskName=Fake-Task" in register_line
    assert "Force=True" in register_line


@pytest.mark.skipif(not shutil.which("powershell") and not shutil.which("pwsh"), reason="PowerShell indisponivel")
def test_install_availability_probe_registration_is_idempotent_across_repeated_runs(tmp_path: Path) -> None:
    env = {"FAKE_PYTHONW_PATH": str(tmp_path / "fakepy" / "pythonw.exe")}
    first = _run_probe_harness(tmp_path, env=env, extra_args=["-TaskName", "Fake-Task"])
    second = _run_probe_harness(tmp_path, env=env, extra_args=["-TaskName", "Fake-Task"])
    for proc in (first, second):
        assert proc.returncode == 0, proc.stderr
        register_line = next(line for line in proc.stdout.splitlines() if line.startswith("CALL:Register-ScheduledTask:"))
        assert "TaskName=Fake-Task" in register_line
        assert "Force=True" in register_line


@pytest.mark.skipif(not shutil.which("powershell") and not shutil.which("pwsh"), reason="PowerShell indisponivel")
def test_install_availability_probe_verify_reports_registered_task_last_result(tmp_path: Path) -> None:
    ok_env = {
        "FAKE_TASK_REGISTERED": "1",
        "FAKE_PYTHONW_PATH": str(tmp_path / "fakepy" / "pythonw.exe"),
        "FAKE_LAST_RESULT": "0",
    }
    ok_proc = _run_probe_harness(tmp_path, env=ok_env, extra_args=["-TaskName", "Fake-Task", "-Verify"])
    assert ok_proc.returncode == 0, ok_proc.stderr
    assert "Tarefa 'Fake-Task' registrada." in ok_proc.stdout
    assert "LastTaskResult: 0" in ok_proc.stdout

    failing_env = dict(ok_env, FAKE_LAST_RESULT="5")
    failing_proc = _run_probe_harness(tmp_path, env=failing_env, extra_args=["-TaskName", "Fake-Task", "-Verify"])
    assert failing_proc.returncode == 1
    assert "FALHOU" in failing_proc.stdout

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
