"""USR-384: boot service definitions and Windows task XML."""

import hashlib
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath
import xml.etree.ElementTree as ET

import pytest

from core.line.fleet_dispatcher import LOG_BACKUP_COUNT, LOG_MAX_BYTES, configure_logging
from core.line import fleet_bootstrap
from core.line.fleet_task_xml import TASK_NAMESPACE as TASK_NS, build_fleet_task_xml


def _prepare_fleet_project(project_root: Path) -> Path:
    bootstrap = project_root / "core" / "line" / "fleet_bootstrap.py"
    dispatcher = project_root / "core" / "line" / "fleet_dispatcher.py"
    bootstrap.parent.mkdir(parents=True, exist_ok=True)
    bootstrap.write_text("# test bootstrap\n", encoding="utf-8")
    dispatcher.write_text("# test dispatcher\n", encoding="utf-8")
    return bootstrap


def test_windows_task_xml_runs_at_boot_as_service_user_and_restarts(tmp_path: Path) -> None:
    project_root = tmp_path.resolve()
    state_root = project_root / ".factory"
    state_root.mkdir()
    bootstrap = _prepare_fleet_project(project_root)
    xml = build_fleet_task_xml(
        project_root,
        state_root,
        Path("C:/Python312/python.exe"),
        bootstrap=bootstrap,
    )
    assert xml == build_fleet_task_xml(
        project_root,
        state_root,
        Path("C:/Python312/python.exe"),
        bootstrap=bootstrap,
    )
    root = ET.fromstring(xml)
    q = lambda name: f"{{{TASK_NS}}}{name}"

    assert root.find(f".//{q('BootTrigger')}") is not None
    timer = root.find(f".//{q('TimeTrigger')}")
    assert timer is not None
    assert timer.findtext(f"{q('Repetition')}/{q('Interval')}") == "PT1M"
    assert timer.findtext(f"{q('Repetition')}/{q('Duration')}") == "PT0S"
    assert root.findtext(f".//{q('Principal')}/{q('LogonType')}") == "InteractiveToken"
    assert root.findtext(f".//{q('RunLevel')}") == "LeastPrivilege"
    assert root.findtext(f".//{q('MultipleInstancesPolicy')}") == "IgnoreNew"
    assert root.findtext(f".//{q('ExecutionTimeLimit')}") == "PT0S"
    assert root.findtext(f".//{q('RestartOnFailure')}/{q('Interval')}") == "PT1M"
    assert root.findtext(f".//{q('RestartOnFailure')}/{q('Count')}") == "999"
    assert root.findtext(f".//{q('Command')}") == (
        r"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
    )
    arguments = root.findtext(f".//{q('Arguments')}") or ""
    assert "C:\\Python312\\python.exe" in arguments
    assert "PROJECT_ROOT = '" + str(project_root) + "'" in arguments
    assert "STATE_ROOT = '" + str(state_root) + "'" in arguments
    assert "$env:DARKFAC_OPERATING_HARNESS = ''" in arguments
    assert "PYTHONPATH = '" + str(project_root) + "'" in arguments
    assert "fleet_bootstrap.py' --project-root '" + str(project_root) in arguments
    assert "--state-root '" + str(state_root) + "' --loop" in arguments
    assert root.findtext(f".//{q('WorkingDirectory')}") == str(project_root)
    working_directory = root.findtext(f".//{q('WorkingDirectory')}")
    assert Path(working_directory).is_absolute() or PureWindowsPath(working_directory).is_absolute()


def test_windows_task_xml_rejects_relative_executable_or_root(tmp_path: Path) -> None:
    project_root = tmp_path.resolve()
    state_root = project_root / ".factory"
    state_root.mkdir()
    bootstrap = _prepare_fleet_project(project_root)
    with pytest.raises(ValueError):
        build_fleet_task_xml(project_root, state_root, Path("python.exe"), bootstrap=bootstrap)
    with pytest.raises(ValueError):
        build_fleet_task_xml(
            Path("relative"), state_root, Path("C:/Python312/python.exe"), bootstrap=bootstrap,
        )
    with pytest.raises(ValueError):
        build_fleet_task_xml(
            project_root, project_root / "missing-state", Path("C:/Python312/python.exe"),
            bootstrap=bootstrap,
        )


def test_systemd_unit_is_enabled_at_boot_and_restarts_on_failure() -> None:
    unit_path = Path(__file__).parents[1] / "scripts" / "fleet-dispatcher.service"
    unit = unit_path.read_text(encoding="utf-8")
    assert "Restart=always" in unit
    assert "RestartSec=10" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "After=network-online.target" in unit
    assert "User=darkfac" in unit
    assert "Group=darkfac" in unit
    assert "EnvironmentFile=-/etc/darkfac/fleet-dispatcher.env" in unit
    assert "Environment=DARKFAC_OPERATING_HARNESS=" in unit
    assert "Environment=PROJECT_ROOT=/opt/darkfac" in unit
    assert "Environment=STATE_ROOT=/opt/darkfac/.factory" in unit
    assert (
        "ExecStart=/opt/darkfac/.venv/bin/python -m core.line.fleet_bootstrap "
        "--project-root /opt/darkfac --state-root /opt/darkfac/.factory --loop"
    ) in unit
    assert "LogsDirectory=darkfac" in unit


def test_headless_bootstrap_sets_explicit_roots_and_removes_interactive_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    observed: dict[str, object] = {}

    def capture_run_module(name: str, *, run_name: str) -> dict[str, object]:
        observed["module"] = name
        observed["run_name"] = run_name
        observed["argv"] = list(__import__("sys").argv)
        observed["project_root"] = __import__("os").environ.get("DARKFAC_PROJECT_ROOT")
        observed["state_root"] = __import__("os").environ.get("DARKFAC_STATE_ROOT")
        observed["public_project_root"] = __import__("os").environ.get("PROJECT_ROOT")
        observed["public_state_root"] = __import__("os").environ.get("STATE_ROOT")
        observed["execution_context"] = __import__("os").environ.get("DARKFAC_EXECUTION_CONTEXT")
        observed["fleet_mode"] = __import__("os").environ.get("DARKFAC_FLEET_MODE")
        observed["operating_harness"] = __import__("os").environ.get("DARKFAC_OPERATING_HARNESS")
        observed["interactive"] = {
            key: __import__("os").environ.get(key)
            for key in fleet_bootstrap._INTERACTIVE_HARNESS_ENV
        }
        return {}

    monkeypatch.setattr(fleet_bootstrap.runpy, "run_module", capture_run_module)
    monkeypatch.setenv("DARKFAC_OPERATING_HARNESS", "antigravity")
    monkeypatch.setenv("CODEX_THREAD_ID", "interactive-session")
    monkeypatch.setenv("DARKFAC_EXECUTION_CONTEXT", "interactive")
    monkeypatch.setenv("DARKFAC_FLEET_MODE", "0")
    monkeypatch.setenv("PROJECT_ROOT", "inherited-project")
    monkeypatch.setenv("STATE_ROOT", "inherited-state")
    # `fleet_bootstrap.main` sets roots and the headless contract on the real `os.environ`.
    # Pre-register each key with `monkeypatch` so the process-level bootstrap does not leak into
    # later tests after this simulated dispatcher returns.
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / ".factory"))
    state = tmp_path / "persistent-state"
    state.mkdir()
    _prepare_fleet_project(tmp_path)
    assert fleet_bootstrap.main([
        "--project-root", str(tmp_path), "--state-root", str(state), "--loop"
    ]) == 0

    assert observed["module"] == "core.line.fleet_dispatcher"
    assert observed["argv"] == ["core.line.fleet_dispatcher", "--loop"]
    assert observed["project_root"] == str(tmp_path.resolve())
    assert observed["state_root"] == str(state.resolve())
    assert observed["public_project_root"] == str(tmp_path.resolve())
    assert observed["public_state_root"] == str(state.resolve())
    assert observed["execution_context"] == "headless"
    assert observed["fleet_mode"] == "1"
    assert observed["operating_harness"] == ""
    assert all(
        value is None
        for key, value in observed["interactive"].items()
        if key != "DARKFAC_OPERATING_HARNESS"
    )
    identity_record = json.loads(capsys.readouterr().out)
    assert identity_record == {
        "event": "dispatcher_process_identity",
        "harness": None,
        "origin": "headless",
        "mode": "write",
        "execution_context": "headless",
        "project_root": str(tmp_path.resolve()),
        "state_root": str(state.resolve()),
        "process_harness": None,
        "process_harness_origin": "declared_none",
        "model": None,
        "write_capable": True,
    }


def test_bootstrap_rejects_relative_persistent_roots() -> None:
    with pytest.raises(SystemExit) as exc:
        fleet_bootstrap.main(["--project-root", ".", "--state-root", ".factory", "--loop"])
    assert exc.value.code == 2


def test_bootstrap_requires_roots_and_rejects_missing_directories(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as missing:
        fleet_bootstrap.main(["--loop"])
    assert missing.value.code == 2

    state_root = tmp_path / "state"
    state_root.mkdir()
    with pytest.raises(SystemExit) as bad_project:
        fleet_bootstrap.main([
            "--project-root", str(tmp_path / "missing-project"),
            "--state-root", str(state_root), "--loop",
        ])
    assert bad_project.value.code == 2

    project_root = tmp_path / "project"
    _prepare_fleet_project(project_root)
    with pytest.raises(SystemExit) as bad_state:
        fleet_bootstrap.main([
            "--project-root", str(project_root),
            "--state-root", str(tmp_path / "missing-state"), "--loop",
        ])
    assert bad_state.value.code == 2


def test_controlled_exit_15_relaunch_preserves_queue_sha_and_single_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model Task Scheduler's next one-minute trigger after a crashed dispatcher process."""
    project = tmp_path / "project"
    state_root = tmp_path / "persistent-state"
    _prepare_fleet_project(project)
    monkeypatch.setenv("PROJECT_ROOT", str(project))
    monkeypatch.setenv("STATE_ROOT", str(state_root))
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(project))
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(state_root))
    monkeypatch.setenv("DARKFAC_EXECUTION_CONTEXT", "interactive")
    monkeypatch.setenv("DARKFAC_FLEET_MODE", "0")
    queue_path = state_root / "runs" / "pending_queue.json"
    queue_path.parent.mkdir(parents=True)
    queue = [{"id": "USR-471", "status": "planned", "candidate_digest": "a" * 40}]
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    state_path = state_root / "runs" / "fleet_dispatcher_state.json"
    state_path.write_text(json.dumps({"in_flight": {}, "queue_remaining": ["USR-471"]}), encoding="utf-8")
    launches = 0
    observed: list[dict[str, object]] = []
    pids = iter((13084, 14520))
    monkeypatch.setattr(fleet_bootstrap.os, "getpid", lambda: next(pids))

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = {"queue": digest(queue_path), "state": digest(state_path)}

    def controlled_dispatcher(_name: str, *, run_name: str) -> dict[str, object]:
        nonlocal launches
        launches += 1
        assert run_name == "__main__"
        saved_queue = json.loads(queue_path.read_text(encoding="utf-8"))
        saved_state = json.loads(state_path.read_text(encoding="utf-8"))
        observed.append({
            "pid": __import__("os").getpid(),
            "queue": saved_queue,
            "state": saved_state,
            "queue_digest": digest(queue_path),
            "state_digest": digest(state_path),
        })
        if launches == 1:
            raise SystemExit(15)
        return {}

    monkeypatch.setattr(fleet_bootstrap.runpy, "run_module", controlled_dispatcher)
    args = ["--project-root", str(project), "--state-root", str(state_root), "--loop"]
    with pytest.raises(SystemExit) as crash:
        fleet_bootstrap.main(args)
    assert crash.value.code == 15
    after_exit = {"queue": digest(queue_path), "state": digest(state_path)}
    assert fleet_bootstrap.main(args) == 0  # subsequent PT1M trigger starts a fresh process
    after_recovery = {"queue": digest(queue_path), "state": digest(state_path)}

    assert launches == 2
    assert [launch["pid"] for launch in observed] == [13084, 14520]
    assert observed[0]["queue_digest"] == before["queue"]
    assert observed[0]["state_digest"] == before["state"]
    assert after_exit == before
    assert observed[1]["queue_digest"] == after_exit["queue"]
    assert observed[1]["state_digest"] == after_exit["state"]
    assert after_recovery == before
    assert observed[1]["queue"] == queue
    assert observed[1]["state"]["queue_remaining"] == ["USR-471"]
    assert observed[1]["queue"][0]["candidate_digest"] == "a" * 40
    xml = build_fleet_task_xml(
        project,
        state_root,
        Path("C:/Python312/python.exe"),
        bootstrap=project / "core" / "line" / "fleet_bootstrap.py",
    )
    root = ET.fromstring(xml)
    q = lambda name: f"{{{TASK_NS}}}{name}"
    assert root.findtext(f".//{q('MultipleInstancesPolicy')}") == "IgnoreNew"
    assert root.findtext(f".//{q('TimeTrigger')}/{q('Repetition')}/{q('Interval')}") == "PT1M"


def _write_state(state_root: Path, *, in_flight: dict, ts: datetime | None) -> None:
    runs_dir = state_root / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    payload = {"in_flight": in_flight}
    if ts is not None:
        payload["ts"] = ts.isoformat()
    (runs_dir / "fleet_dispatcher_state.json").write_text(json.dumps(payload), encoding="utf-8")


def test_check_active_run_detects_fresh_in_flight_ticket(tmp_path: Path) -> None:
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    _write_state(tmp_path, in_flight={"notebook": "USR-471"}, ts=now - timedelta(seconds=30))

    active, reason = fleet_bootstrap.check_active_run(tmp_path, now=now)

    assert active is True
    assert "USR-471" in reason


def test_check_active_run_ignores_stale_cycle(tmp_path: Path) -> None:
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    _write_state(tmp_path, in_flight={"notebook": "USR-471"}, ts=now - timedelta(minutes=10))

    active, reason = fleet_bootstrap.check_active_run(tmp_path, now=now)

    assert active is False
    assert "obsoleto" in reason


def test_check_active_run_ignores_empty_in_flight(tmp_path: Path) -> None:
    now = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    _write_state(tmp_path, in_flight={}, ts=now)

    active, reason = fleet_bootstrap.check_active_run(tmp_path, now=now)

    assert active is False


def test_check_active_run_without_state_file_is_not_active(tmp_path: Path) -> None:
    active, reason = fleet_bootstrap.check_active_run(tmp_path)

    assert active is False
    assert "sem estado" in reason


def test_bootstrap_cli_check_active_run_returns_nonzero_without_touching_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(timezone.utc)
    _prepare_fleet_project(tmp_path)
    _write_state(tmp_path, in_flight={"desktop": "USR-999"}, ts=now)
    monkeypatch.delenv("DARKFAC_PROJECT_ROOT", raising=False)
    monkeypatch.delenv("DARKFAC_STATE_ROOT", raising=False)

    rc = fleet_bootstrap.main([
        "--project-root", str(tmp_path), "--state-root", str(tmp_path), "--check-active-run",
    ])

    assert rc == 1
    assert "USR-999" in capsys.readouterr().out
    # the check-only path never mutates process identity or environment (AGENTS.md: fail closed).
    assert "DARKFAC_PROJECT_ROOT" not in __import__("os").environ


def test_install_script_aborts_reinstall_when_a_run_is_active() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "install_fleet_dispatcher.ps1").read_text(encoding="utf-8")
    assert "--check-active-run" in script
    assert "fleet_bootstrap" in script
    check_section, _, rest = script.partition("--check-active-run")
    assert "exit 3" in rest.split("Register-ScheduledTask", 1)[0]
    assert "fila e o SHA" in rest.split("Register-ScheduledTask", 1)[0]
    assert 'Get-ScheduledTask -TaskName "DarkFac-Fleet-Dispatcher"' in script
    assert 'State -eq "Running"' in script
    assert "exit 4" in rest.split("Register-ScheduledTask -TaskName", 1)[0]
    assert "Restart-Computer" not in script
    assert "--project-root $repoRoot" in script
    assert "--state-root $stateRoot" in script
    assert "--bootstrap $bootstrapPath" in script
    assert "--repo" not in script
    assert 'Register-ScheduledTask -TaskName "DarkFac-Fleet-Dispatcher" -Xml ($xmlLines -join "`n")' in script
    assert "-Force | Out-Null" in script
    assert "Stop-ScheduledTask" not in script
    assert "Unregister-ScheduledTask" not in script


def test_dispatcher_logging_rotates_utf8_file_and_keeps_console_copy(tmp_path: Path) -> None:
    console = io.StringIO()
    handler = configure_logging(tmp_path, stream=console)
    try:
        assert handler.maxBytes == LOG_MAX_BYTES == 10 * 1024 * 1024
        assert handler.backupCount == LOG_BACKUP_COUNT == 5
        assert handler.encoding == "utf-8"
        logging.getLogger("darkfac.test").info("despacho café")
        handler.flush()
        log_file = tmp_path / "fleet-dispatcher.log"
        assert "despacho café" in log_file.read_text(encoding="utf-8")
        assert "despacho café" in console.getvalue()
    finally:
        logging.shutdown()
