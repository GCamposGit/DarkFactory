"""USR-207: Hub availability during merges (classifier, single deploy, watchdog, frontend retry)."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from core.infra import availability_probe as probe_mod
from core.infra import container_memory
from core.infra import deploy_classifier as dc
from scripts import dokploy_redeploy as mod
from scripts import hub_load_probe
from scripts import hub_downtime_probe


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
        ([".factory/demands/demands.json", "docs/x.md", "tests/test_a.py", ".github/workflows/ci.yml", "README.md"], False),
        (["docs/x.md", "core/infra/node_sync.py"], True),
        (["hub/frontend/app.js"], True),
        (["deploy/dokploy/docker-compose.hub.yml"], True),
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
    assert dc.classify(A, B, [".factory\\demands\\demands.json"])[0] is False


def _dockerfile_copy_sources() -> set[str]:
    dockerfile = (ROOT / "deploy" / "dokploy" / "Dockerfile.hub").read_text(encoding="utf-8")
    return {
        match.group(1).rstrip("/")
        for line in dockerfile.splitlines()
        if (match := re.match(r"^\s*COPY\s+([^\s]+)\s+", line, flags=re.IGNORECASE))
    }


def _hub_image_workflow_paths() -> set[str]:
    workflow = (ROOT / ".github" / "workflows" / "hub-image.yml").read_text(encoding="utf-8")
    paths_start = re.search(r"^\s+paths:\s*$", workflow, flags=re.MULTILINE)
    assert paths_start, "hub-image workflow has no push.paths trigger"
    paths: set[str] = set()
    for line in workflow[paths_start.end():].splitlines():
        if line.strip() and not line.startswith("      "):
            break
        match = re.match(r'^\s+-\s+["\']?([^"\']+?)["\']?\s*$', line)
        if match:
            paths.add(match.group(1))
    return paths


def test_classifier_rebuild_paths_are_embedded_in_the_hub_image() -> None:
    copied_sources = _dockerfile_copy_sources()
    expected_paths = {f"{path}/**" for path in copied_sources if path not in {"docs", ".factory", "requirements.txt", "harness.config.json"}}
    # The explicit hub/data COPY is covered by the broader hub/** source.
    expected_paths.discard("hub/data/**")
    expected_paths.update({"requirements.txt", "harness.config.json"})
    assert set(dc.runtime_image_paths()) == expected_paths


def test_classifier_rebuild_paths_are_covered_by_hub_image_workflow_trigger() -> None:
    workflow_paths = _hub_image_workflow_paths()
    missing = sorted(set(dc.runtime_image_paths()) - workflow_paths)

    assert not missing, f"workflow trigger misses runtime image paths: {missing}"


def test_proposed_hub_image_trigger_exactly_matches_runtime_paths_and_serializes_builds() -> None:
    patch = (ROOT / "docs" / "proposals" / "USR-207-hub-image-trigger.patch").read_text(encoding="utf-8")
    lines = patch.splitlines()
    paths_index = next(
        (index for index, line in enumerate(lines) if line.startswith(("+", " ")) and re.match(r"^[+ ]\s+paths:\s*$", line)),
        None,
    )
    assert paths_index is not None, "proposed workflow patch must restrict push.paths"
    proposed_paths: set[str] = set()
    for line in lines[paths_index + 1 :]:
        if line.startswith("-"):
            continue
        if not line.startswith(("+", " ")):
            break
        content = line[1:]
        match = re.match(r"^\s+-\s+(['\"]?)(.*?)\1\s*$", content)
        if match:
            proposed_paths.add(match.group(2))
        elif content.strip():
            break
    assert proposed_paths == set(dc.runtime_image_paths())
    assert "+concurrency:" in patch and "+  group: hub-image-${{ github.ref }}" in patch
    assert "+  cancel-in-progress: false" in patch


def test_hub_image_workflow_checker_reports_synthetic_trigger_divergence(tmp_path: Path) -> None:
    synthetic = tmp_path / "hub-image-trigger-out-of-sync.yml"
    synthetic.write_text('on:\n  push:\n    paths:\n      - "core/**"\n', encoding="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "hub_image_workflow_check.py"), "--workflow", str(synthetic)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    finally:
        synthetic.unlink(missing_ok=True)
    assert result.returncode != 0
    assert "faltam" in result.stdout.lower()
    assert "sobram" in result.stdout.lower()


def test_run_hub_only_merge_is_data_only() -> None:
    assert dc.classify(A, B, ["run_hub.py"]) == (False, dc.REASON_DATA_ONLY)


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


def test_downtime_probe_samples_once_per_second_and_records_network_failures(tmp_path: Path) -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.elapsed = 0.0

        def monotonic(self) -> float:
            return self.elapsed

        def sleep(self, seconds: float) -> None:
            self.elapsed += seconds

        def now(self) -> datetime:
            return NOW + timedelta(seconds=self.elapsed)

    clock = FakeClock()
    responses = [
        {"status": 200, "body": {"git_sha": A}, "latency_ms": 2},
        TimeoutError("timed out"),
        urllib.error.URLError("connection refused"),
    ]

    def get(_url: str, _timeout: float) -> Any:
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    output = tmp_path / "samples.jsonl"
    count = hub_downtime_probe.sample_probe(
        "https://darkhub.ggcampos.com/health",
        output,
        duration=2.0,
        interval=1.0,
        timeout=0.5,
        http_get=get,
        now=clock.now,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert count == len(rows) == 3
    assert [row["status"] for row in rows] == [200, None, None]
    assert [row["error"] for row in rows] == [None, "timeout", "connection_error"]
    assert [row["sha"] for row in rows] == [A, None, None]
    assert [datetime.fromisoformat(rows[i + 1]["timestamp"]) - datetime.fromisoformat(rows[i]["timestamp"])
            for i in range(2)] == [timedelta(seconds=1), timedelta(seconds=1)]


@pytest.mark.parametrize("rows", [
    [{"status": 200, "sha": A}, {"status": 503, "sha": B}],
    [{"status": 200, "sha": A}, {"status": 200, "sha": A}],
    [{"status": 200, "sha": A, "error": "timeout"}, {"status": 200, "sha": B}],
])
def test_downtime_probe_verdict_rejects_non200_or_single_sha(tmp_path: Path, rows: List[Dict[str, Any]]) -> None:
    report = tmp_path / "samples.jsonl"
    report.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert hub_downtime_probe.verify_report(report)["passed"] is False


def test_downtime_probe_verdict_accepts_zero_failures_and_two_shas(tmp_path: Path) -> None:
    report = tmp_path / "samples.jsonl"
    report.write_text(
        "\n".join(json.dumps({"status": 200, "sha": sha}) for sha in (A, B)) + "\n",
        encoding="utf-8",
    )
    verdict = hub_downtime_probe.verify_report(report)
    assert verdict["passed"] is True
    assert verdict["non200"] == 0
    assert verdict["shas"] == [A, B]


def test_downtime_probe_cli_saves_verdict_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = tmp_path / "samples.jsonl"
    report.write_text(
        "\n".join(json.dumps({"status": 200, "sha": sha}) for sha in (A, B)) + "\n",
        encoding="utf-8",
    )
    verdict_file = tmp_path / "evidence" / "verdict.json"
    assert hub_downtime_probe.main(["--verify", str(report), "--verdict-output", str(verdict_file)]) == 0
    assert json.loads(verdict_file.read_text(encoding="utf-8"))["passed"] is True
    assert json.loads(capsys.readouterr().out)["passed"] is True


@pytest.mark.parametrize("payload", [{"status": "ok"}, {"git_sha": None}, {}, "not-json"])
def test_downtime_probe_sha_extraction_tolerates_health_payload_without_sha(payload: Any) -> None:
    assert hub_downtime_probe.extract_sha(payload) is None

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
    assert plan(A, [".factory/demands/demands.json"]) == mod.DEPLOY_SKIP
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
    code, text = _run(t, monkeypatch, A, [".factory/demands/demands.json", "docs/a.md"])
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


def test_watchdog_alerts_once_after_two_minutes_and_reports_recovery() -> None:
    clock = [1000.0]
    healthy = [False]
    sent: List[str] = []
    state = probe_mod.ProbeState()

    def step(dt: float) -> None:
        clock[0] += dt
        probe_mod.tick(state, probe=lambda: healthy[0], send=lambda m: sent.append(m) or True, now=lambda: clock[0])

    step(0)
    step(60)
    step(59)
    assert sent == []  # 119 s down
    step(2)
    assert len(sent) == 1 and "indisponivel" in sent[0]
    step(60)
    assert len(sent) == 1  # no spam
    healthy[0] = True
    step(30)
    assert len(sent) == 2 and "voltou" in sent[1]
    assert state.first_failure_ts is None and not state.alerted


def test_watchdog_retries_when_sender_fails_and_short_blip_is_silent() -> None:
    sent: List[str] = []
    calls = {"n": 0}

    def send(m: str) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("telegram down")
        sent.append(m)
        return True

    state = probe_mod.ProbeState()
    for t in (0.0, 130.0, 140.0):
        probe_mod.tick(state, probe=lambda: False, send=send, now=lambda t=t: t)
    assert len(sent) == 1

    quiet: List[str] = []
    s2 = probe_mod.ProbeState()
    for t, up in ((0.0, False), (60.0, False), (90.0, True)):
        probe_mod.tick(s2, probe=lambda up=up: up, send=lambda m: quiet.append(m) or True, now=lambda t=t: t)
    assert quiet == []


def test_probe_state_roundtrip_and_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "s.json"
    probe_mod.save_state(path, probe_mod.ProbeState(first_failure_ts=5.0, alerted=True))
    assert probe_mod.load_state(path) == probe_mod.ProbeState(5.0, True)
    path.write_text("{garbage", encoding="utf-8")
    assert probe_mod.load_state(path) == probe_mod.ProbeState()


def test_container_memory_reads_cgroup_v2_and_unbounded_limit(tmp_path: Path) -> None:
    (tmp_path / "memory.current").write_text("12345\n", encoding="utf-8")
    (tmp_path / "memory.max").write_text("max\n", encoding="utf-8")
    (tmp_path / "memory.events").write_text("low 0\nhigh 1\nmax 0\noom 0\noom_kill 2\n", encoding="utf-8")

    result = container_memory.read_container_memory(cgroup_v2=tmp_path, cgroup_v1=tmp_path / "missing")

    assert result == {
        "available": True, "version": 2, "current_bytes": 12345,
        "limit_bytes": None, "oom_kill_count": 2,
    }
    missing = tmp_path / "partial"
    missing.mkdir()
    (missing / "memory.current").write_text("42", encoding="utf-8")
    assert container_memory.read_container_memory(cgroup_v2=missing, cgroup_v1=tmp_path / "absent")["available"] is True
    assert container_memory.read_container_memory(cgroup_v2=missing, cgroup_v1=tmp_path / "absent")["oom_kill_count"] is None


def test_container_memory_falls_back_to_v1_and_never_raises(tmp_path: Path) -> None:
    v1 = tmp_path / "v1"
    v1.mkdir()
    (v1 / "memory.usage_in_bytes").write_text("500\n", encoding="utf-8")
    (v1 / "memory.limit_in_bytes").write_text("1000\n", encoding="utf-8")
    (v1 / "memory.oom_control").write_text("oom_kill 3\nunder_oom 0\n", encoding="utf-8")

    assert container_memory.read_container_memory(cgroup_v2=tmp_path / "absent", cgroup_v1=v1) == {
        "available": True, "version": 1, "current_bytes": 500,
        "limit_bytes": 1000, "oom_kill_count": 3,
    }
    assert container_memory.read_container_memory(cgroup_v2=tmp_path / "absent", cgroup_v1=tmp_path / "absent2")["available"] is False


def test_health_memory_query_exposes_cgroup_data_without_changing_default_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient
    from hub.backend.main import app

    monkeypatch.setattr(container_memory, "read_container_memory", lambda: {"available": True, "current_bytes": 12})
    client = TestClient(app)
    standard = client.get("/health")
    measured = client.get("/health?memory=true")
    assert "memory" not in standard.json() and "instance_id" not in standard.json()
    assert measured.json()["memory"] == {"available": True, "current_bytes": 12}
    assert measured.json()["instance_id"]


def test_memory_alerts_once_for_oom_and_recovers_and_requires_two_high_ticks() -> None:
    state = probe_mod.ProbeState()
    sent: List[str] = []
    now = [100.0]
    values = {"status": "ok", "git_sha": "a", "memory": {"available": True, "current_bytes": 90, "limit_bytes": 100, "oom_kill_count": 0}}

    def step() -> None:
        probe_mod.tick(state, probe=lambda: values, send=lambda message: sent.append(message) or True, now=lambda: now[0])
        now[0] += 60

    values["memory"]["current_bytes"] = 90
    step()
    assert not sent  # one high tick is quiet
    step()
    assert len(sent) == 1 and "85%" in sent[0]
    step()
    assert len(sent) == 1
    values["memory"]["current_bytes"] = 20
    step()
    assert len(sent) == 2 and "recuper" in sent[-1].lower()
    values["memory"]["oom_kill_count"] = 1
    step()
    step()
    assert len(sent) == 4 and "oom_kill" in sent[-2].lower() and "recuper" in sent[-1].lower()


def test_probe_status_fails_when_last_tick_is_stale_and_passes_when_recent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "probe.json"
    probe_mod.save_state(path, probe_mod.ProbeState(last_tick_ts=100.0))
    assert probe_mod.main(path=path, status=True, now=1000.0, stale_after_seconds=300) != 0
    assert "idade" in capsys.readouterr().out.lower()
    probe_mod.save_state(path, probe_mod.ProbeState(last_tick_ts=950.0))
    assert probe_mod.main(path=path, status=True, now=1000.0, stale_after_seconds=300) == 0


def test_load_probe_report_calculates_peak_and_p95_from_samples() -> None:
    report = hub_load_probe.summarize_samples([
        {"latency_ms": 10, "memory_current_bytes": 100, "git_sha": "a", "oom_kill_count": 0},
        {"latency_ms": 20, "memory_current_bytes": 250, "git_sha": "a", "oom_kill_count": 0},
        {"latency_ms": 30, "memory_current_bytes": 150, "git_sha": "a", "oom_kill_count": 0},
    ])
    assert report["peak_memory_bytes"] == 250
    assert report["latency_p95_ms"] == 30


def test_load_probe_records_transport_failure_as_non_200_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(hub_load_probe.urllib.request, "urlopen", fail_urlopen)

    status, payload, latency = hub_load_probe._get("https://hub.example/api/cloud/status", 1)

    assert status == 0
    assert payload is None
    assert latency >= 0


def test_load_probe_aborts_and_writes_report_when_health_drops_mid_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"health": 0}

    def fake_get(url: str, timeout: float) -> tuple[int, dict[str, Any] | None, float]:
        if "/health" in url:
            calls["health"] += 1
            if calls["health"] == 1:
                return 200, {
                    "status": "ok", "git_sha": "a", "instance_id": "instance-a",
                    "memory": {"current_bytes": 100, "oom_kill_count": 0},
                }, 1.0
            return 0, None, 2.0
        return 200, None, 3.0

    monkeypatch.setattr(hub_load_probe, "_get", fake_get)
    output = tmp_path / "report.json"
    report = hub_load_probe.run_probe(
        "https://hub.example", duration_seconds=0.01, concurrency=1, timeout=1, output=output,
    )

    assert report["aborted"] is True
    assert "health" in report["abort_reason"].lower()
    assert output.exists()


# --------------------------------------------------------------------------- frontend (static)

def test_no_raw_fetch_outside_the_helper() -> None:
    raw_fetch_files = []
    for js in sorted(FRONTEND.glob("*.js")):
        source = js.read_text(encoding="utf-8")
        if re.search(r"(?<![\w.$])fetch\(", source):
            raw_fetch_files.append(js.name)
    assert raw_fetch_files == ["hub_net.js"]


def test_helper_is_loaded_before_every_module_and_shows_banner_text() -> None:
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="/static/([\w.]+)\?v=', html)
    assert scripts[0] == "hub_net.js"
    assert "Hub atualizando, reconectando..." in (FRONTEND / "hub_net.js").read_text(encoding="utf-8")


def test_live_page_loads_helper_before_live_module() -> None:
    html = (FRONTEND / "live.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="/static/([\w.]+)\?v=', html)
    assert scripts.index("hub_net.js") < scripts.index("live.js")


def test_hub_net_node_behaviour() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed on this runner")
    result = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "js" / "hub_net.test.cjs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=180, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
