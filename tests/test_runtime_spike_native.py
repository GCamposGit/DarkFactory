"""Integration tests for the HF-02-04 baseline adapter and JSONL facade."""

from __future__ import annotations

import io
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from spikes.runtime_choice.contracts import (
    DriverAction,
    DriverCommand,
    DriverEventKind,
    LabConfig,
    RuntimeKind,
    RuntimeStatus,
    WorkflowVersion,
)
from spikes.runtime_choice.effect_server import EffectServer
from spikes.runtime_choice.native_adapter import NativeAdapter, NativeAdapterError
from spikes.runtime_choice.driver import run_jsonl

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TERMINAL_EVENT_DEADLINE_SECONDS = 60.0


def make_config(tmp_path: Path, effect_base_url: str) -> LabConfig:
    return LabConfig(
        lab_id="native-test",
        root_dir=tmp_path,
        runtime=RuntimeKind.NATIVE_SQLITE,
        runtime_version="native-core",
        workflow_version=WorkflowVersion.V1,
        database_alias="darkfac_hf02_native",
        effect_base_url=effect_base_url,
        lease_seconds=15.0,
    )


def start_command(workflow_id: str, *, approval_pre_resolved: bool = True) -> DriverCommand:
    return DriverCommand(
        command_id=f"start-{workflow_id}",
        action=DriverAction.START,
        workflow_id=workflow_id,
        scenario_id="R01",
        workflow_version=WorkflowVersion.V1,
        payload={"approval_pre_resolved": approval_pre_resolved, "value": 7, "release_digest": "release-A"},
    )


def wait_for_terminal(adapter: NativeAdapter, workflow_id: str) -> list:
    events = []
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        event = adapter.poll_event(timeout=0.2)
        if event is None:
            continue
        events.append(event)
        if event.workflow_id == workflow_id and event.kind in {DriverEventKind.COMPLETED, DriverEventKind.ERROR}:
            return events
    raise AssertionError(f"workflow {workflow_id!r} did not reach a terminal event")


def test_native_adapter_executes_core_path_and_keeps_capability_gaps(tmp_path: Path) -> None:
    with EffectServer(tmp_path) as server:
        config = make_config(tmp_path, server.base_url)
        adapter = NativeAdapter(config)
        started = adapter.start(start_command("workflow-1"))
        events = wait_for_terminal(adapter, "workflow-1")

        assert started[0].kind is DriverEventKind.STARTED
        assert events[-1].kind is DriverEventKind.COMPLETED
        assert events[-1].runtime_status is RuntimeStatus.SUCCEEDED
        assert server.store.effect_count(workflow_id="workflow-1") == 1
        assert [event.step_id for event in events if event.kind is DriverEventKind.STEP_OBSERVED] == ["S0", "S1", "S3", "S4"]
        assert adapter.database_path == tmp_path / "native" / "orchestrator.sqlite3"
        assert adapter.capabilities.durable_steps is True
        assert adapter.capabilities.durable_wait is False
        assert adapter.capabilities.cancel_before_next_step is False

        duplicate = adapter.start(start_command("workflow-1"))
        assert duplicate[0].kind is DriverEventKind.UNSUPPORTED
        assert duplicate[0].code == "CAPABILITY_UNSUPPORTED:deduplicated_intake"
        adapter.shutdown()


def test_native_adapter_labels_wait_cancel_and_version_as_unsupported(tmp_path: Path) -> None:
    with EffectServer(tmp_path) as server:
        adapter = NativeAdapter(make_config(tmp_path, server.base_url))
        waiting = adapter.start(start_command("workflow-wait", approval_pre_resolved=False))
        assert waiting[0].code == "CAPABILITY_UNSUPPORTED:durable_wait"

        approve = adapter.approve(
            DriverCommand(command_id="approve-1", action="approve", workflow_id="workflow-wait")
        )
        cancel = adapter.cancel(
            DriverCommand(command_id="cancel-1", action="cancel", workflow_id="workflow-wait")
        )
        version = adapter.start(
            DriverCommand(
                command_id="start-v2",
                action="start",
                workflow_id="workflow-v2",
                scenario_id="R08",
                workflow_version="v2",
                payload={"approval_pre_resolved": True},
            )
        )
        assert approve[0].code == "CAPABILITY_UNSUPPORTED:durable_wait"
        assert cancel[0].code == "CAPABILITY_UNSUPPORTED:cancel_before_next_step"
        assert version[0].code == "CAPABILITY_UNSUPPORTED:version_isolation"
        adapter.shutdown()


def test_native_adapter_reclaims_an_unleased_run_from_its_exclusive_store(tmp_path: Path) -> None:
    with EffectServer(tmp_path) as server:
        config = make_config(tmp_path, server.base_url)
        adapter = NativeAdapter(config)
        adapter.store.create_run(
            "workflow-recover",
            run_id="run-recover",
            checkpoint={"outputs": {"0": {"prepared": True, "value": 7}}},
        )
        started = adapter.start(start_command("workflow-recover"))
        events = wait_for_terminal(adapter, "workflow-recover")

        assert started[0].kind is DriverEventKind.STARTED
        assert events[-1].runtime_status is RuntimeStatus.SUCCEEDED
        assert adapter.store.get_run("run-recover").status.value == "SUCCEEDED"
        assert server.store.effect_count(workflow_id="workflow-recover") == 1
        adapter.shutdown()


def test_native_adapter_distinguishes_unknown_run_from_store_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = NativeAdapter(make_config(tmp_path, "http://127.0.0.1:18402"))
    unknown = adapter.observe(
        DriverCommand(command_id="observe-unknown", action=DriverAction.OBSERVE, workflow_id="missing-run")
    )
    assert unknown[0].kind is DriverEventKind.ERROR
    assert unknown[0].code == "RUN_NOT_FOUND"

    def fail_to_read(_workflow_id: str) -> None:
        raise sqlite3.OperationalError("database is unavailable")

    monkeypatch.setattr(adapter.store, "get_latest_run", fail_to_read)
    unavailable = adapter.observe(
        DriverCommand(command_id="observe-unavailable", action=DriverAction.OBSERVE, workflow_id="existing-run")
    )
    assert unavailable[0].kind is DriverEventKind.ERROR
    assert unavailable[0].code == "STORE_UNAVAILABLE"
    adapter.shutdown()


def test_jsonl_driver_keeps_workflow_threaded_until_shutdown(tmp_path: Path) -> None:
    with EffectServer(tmp_path) as server:
        config = make_config(tmp_path, server.base_url)
        start = start_command("workflow-jsonl")
        shutdown = DriverCommand(command_id="shutdown-1", action="shutdown")
        output = io.StringIO()
        code = run_jsonl(
            config,
            io.StringIO(start.model_dump_json() + "\n" + shutdown.model_dump_json() + "\n"),
            output,
        )

        events = [json.loads(line) for line in output.getvalue().splitlines()]
        assert code == 0
        assert any(event["kind"] == "started" for event in events)
        assert any(event["kind"] == "completed" and event["runtime_status"] == "succeeded" for event in events)
        assert server.store.effect_count(workflow_id="workflow-jsonl") == 1


def test_json_config_subprocess_runs_protocol_until_terminal_then_shutdown(tmp_path: Path) -> None:
    with EffectServer(tmp_path) as server:
        config = make_config(tmp_path, server.base_url)
        config_path = tmp_path / "lab-config.json"
        config_path.write_text(config.model_dump_json(), encoding="utf-8")
        start = start_command("workflow-subprocess")
        shutdown = DriverCommand(command_id="shutdown-subprocess", action=DriverAction.SHUTDOWN)
        process = subprocess.Popen(
            [sys.executable, "-m", "spikes.runtime_choice.driver", "--config", str(config_path)],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env={**os.environ, "HF02_DRIVER_DIAGNOSTICS": "1"},
        )
        lines: queue.Queue[str] = queue.Queue()
        stderr_lines: list[str] = []

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)

        def read_errors() -> None:
            # Drain stderr continuously so the driver never blocks on a full pipe
            # and the diagnostics survive a timeout (USR-150).
            assert process.stderr is not None
            for line in process.stderr:
                stderr_lines.append(line.rstrip())

        reader = threading.Thread(target=read_output, name="driver-test-reader", daemon=True)
        reader.start()
        err_reader = threading.Thread(target=read_errors, name="driver-test-stderr", daemon=True)
        err_reader.start()
        events: list[dict] = []
        started_at = time.monotonic()
        try:
            assert process.stdin is not None
            process.stdin.write(start.model_dump_json() + "\n")
            process.stdin.flush()
            # Generous deadline: a loaded Windows runner (xdist, repo creation, sqlite)
            # needs far more than the ~1 s a quiet machine takes; the loop exits as
            # soon as a terminal event or the driver exit is observed.
            deadline = started_at + TERMINAL_EVENT_DEADLINE_SECONDS
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=min(0.2, max(0.0, deadline - time.monotonic())))
                except queue.Empty:
                    if process.poll() is not None and lines.empty():
                        break
                    continue
                events.append(json.loads(line))
                if events[-1]["kind"] in {"completed", "error"}:
                    break

            assert any(
                event["kind"] == "started"
                and event["workflow_id"] == "workflow-subprocess"
                and event["runtime_status"] == "running"
                for event in events
            )
            terminal_events = [
                event for event in events if event["kind"] in {"completed", "error"}
            ]
            if not terminal_events:
                err_reader.join(timeout=1)
                pytest.fail(
                    "no terminal event from driver subprocess: "
                    f"elapsed={time.monotonic() - started_at:.2f}s "
                    f"deadline={TERMINAL_EVENT_DEADLINE_SECONDS}s "
                    f"returncode={process.poll()} "
                    f"event_kinds={[event['kind'] for event in events]} "
                    f"stderr={' | '.join(stderr_lines) or '<empty>'}"
                )
            assert terminal_events[-1]["kind"] == "completed", (
                f"terminal event was not completed: {terminal_events[-1]} "
                f"stderr={' | '.join(stderr_lines) or '<empty>'}"
            )
            assert terminal_events[-1]["runtime_status"] == "succeeded"
            assert [
                event["step_id"] for event in events if event["kind"] == "step_observed"
            ] == ["S0", "S1", "S3", "S4"]
            assert server.store.effect_count(workflow_id="workflow-subprocess") == 1
            assert server.store.get_effect("native-test:workflow-subprocess:S3") is not None

            process.stdin.write(shutdown.model_dump_json() + "\n")
            process.stdin.flush()
            process.stdin.close()
            shutdown_code = process.wait(timeout=30)
            err_reader.join(timeout=5)
            assert shutdown_code == 0, " | ".join(stderr_lines)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
            err_reader.join(timeout=5)
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


@pytest.mark.parametrize(
    ("label", "invalid_root"),
    [("bool", True), ("object", {"path": "invalid"}), ("null", None)],
)
def test_cli_rejects_invalid_json_root_with_sanitized_config_error(
    tmp_path: Path, label: str, invalid_root: object
) -> None:
    config = make_config(tmp_path, "http://127.0.0.1:18402")
    payload = json.loads(config.model_dump_json())
    payload["root_dir"] = invalid_root
    config_path = tmp_path / f"invalid-root-{label}.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "spikes.runtime_choice.driver", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip() == "CONFIG_INVALID"
    assert "Traceback" not in result.stderr


def test_cli_reports_inaccessible_native_store_without_fallback_or_traceback(tmp_path: Path) -> None:
    native_path = tmp_path / "native"
    native_path.write_text("occupied", encoding="utf-8")
    config = make_config(tmp_path, "http://127.0.0.1:18402")
    config_path = tmp_path / "lab-config.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "spikes.runtime_choice.driver", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip() == "STORE_UNAVAILABLE"
    assert "Traceback" not in result.stderr
    assert native_path.is_file()


def test_cli_reports_unknown_run_over_real_jsonl_boundary(tmp_path: Path) -> None:
    config = make_config(tmp_path, "http://127.0.0.1:18402")
    config_path = tmp_path / "lab-config.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    observe = DriverCommand(
        command_id="observe-missing",
        action=DriverAction.OBSERVE,
        workflow_id="missing-over-process",
    )
    shutdown = DriverCommand(command_id="shutdown-missing", action=DriverAction.SHUTDOWN)

    result = subprocess.run(
        [sys.executable, "-m", "spikes.runtime_choice.driver", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        input=observe.model_dump_json() + "\n" + shutdown.model_dump_json() + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert result.returncode == 0
    assert result.stderr == ""
    assert any(
        event["kind"] == "error"
        and event["workflow_id"] == "missing-over-process"
        and event["code"] == "RUN_NOT_FOUND"
        for event in events
    )


def test_native_adapter_direct_construction_fails_cleanly_on_inaccessible_store(tmp_path: Path) -> None:
    native_file = tmp_path / "native"
    native_file.write_text("occupied", encoding="utf-8")
    config_file = make_config(tmp_path, "http://127.0.0.1:18402")
    with pytest.raises(NativeAdapterError) as exc_info_file:
        NativeAdapter(config_file)
    assert exc_info_file.value.code == "STORE_UNAVAILABLE"
    assert native_file.is_file()

    tmp_dir_case = tmp_path / "case_dir"
    tmp_dir_case.mkdir()
    db_dir = tmp_dir_case / "native" / "orchestrator.sqlite3"
    db_dir.mkdir(parents=True)
    config_dir = make_config(tmp_dir_case, "http://127.0.0.1:18402")
    with pytest.raises(NativeAdapterError) as exc_info_dir:
        NativeAdapter(config_dir)
    assert exc_info_dir.value.code == "STORE_UNAVAILABLE"
    assert db_dir.is_dir()


def test_cli_reports_inaccessible_sqlite_directory_without_fallback_or_traceback(tmp_path: Path) -> None:
    db_dir = tmp_path / "native" / "orchestrator.sqlite3"
    db_dir.mkdir(parents=True)
    config = make_config(tmp_path, "http://127.0.0.1:18402")
    config_path = tmp_path / "lab-config.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "spikes.runtime_choice.driver", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip() == "STORE_UNAVAILABLE"
    assert "Traceback" not in result.stderr
    assert db_dir.is_dir()
    fallback_files = list(tmp_path.glob("*.sqlite*"))
    assert fallback_files == []


def test_jsonl_driver_emits_store_unavailable_when_observe_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(tmp_path, "http://127.0.0.1:18402")
    observe = DriverCommand(command_id="obs-1", action=DriverAction.OBSERVE, workflow_id="wf-store-fail")
    shutdown = DriverCommand(command_id="shut-1", action=DriverAction.SHUTDOWN)

    output = io.StringIO()
    input_stream = io.StringIO(observe.model_dump_json() + "\n" + shutdown.model_dump_json() + "\n")

    def fail_latest_run(_workflow_id: str) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    from spikes.runtime_choice.driver import DriverSession

    original_init = DriverSession.__init__

    def patched_init(self, cfg: LabConfig) -> None:
        original_init(self, cfg)
        monkeypatch.setattr(self.adapter.store, "get_latest_run", fail_latest_run)

    monkeypatch.setattr(DriverSession, "__init__", patched_init)

    code = run_jsonl(config, input_stream, output)
    events = [json.loads(line) for line in output.getvalue().splitlines()]

    assert code == 0
    assert any(
        event["kind"] == "error"
        and event["workflow_id"] == "wf-store-fail"
        and event["code"] == "STORE_UNAVAILABLE"
        for event in events
    )


def test_native_adapter_distinguishes_storage_error_from_effect_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Part A: Storage error simulation
    lab_storage = tmp_path / "lab_storage"
    lab_storage.mkdir(parents=True, exist_ok=True)
    with EffectServer(tmp_path / "srv_storage") as server:
        config = make_config(lab_storage, server.base_url)
        adapter = NativeAdapter(config)

        def fail_save_checkpoint(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked: busy")

        monkeypatch.setattr(adapter.store, "save_checkpoint", fail_save_checkpoint)

        started = adapter.start(start_command("wf-storage-fail"))
        events = wait_for_terminal(adapter, "wf-storage-fail")

        assert started[0].kind is DriverEventKind.STARTED
        assert events[-1].kind is DriverEventKind.ERROR
        assert events[-1].code == "STORE_UNAVAILABLE"
        assert events[-1].details.get("source") == "storage"
        assert events[-1].details.get("error_type") == "OperationalError"
        assert "database is locked" in events[-1].details.get("diagnostic", "")
        assert str(tmp_path) not in json.dumps(events[-1].details)
        adapter.shutdown()

    # Part B: Effect service failure simulation
    lab_effect = tmp_path / "lab_effect"
    lab_effect.mkdir(parents=True, exist_ok=True)
    with EffectServer(tmp_path / "srv_effect") as server:
        config = make_config(lab_effect, server.base_url)
        adapter = NativeAdapter(config)

        def fail_observe(*args, **kwargs):
            raise NativeAdapterError(
                "EFFECT_SERVICE_UNAVAILABLE",
                details={"source": "effect_service", "error_type": "URLError", "diagnostic": "connection refused"},
            )

        monkeypatch.setattr(adapter.effects, "observe", fail_observe)

        started = adapter.start(start_command("wf-effect-fail"))
        events = wait_for_terminal(adapter, "wf-effect-fail")

        assert started[0].kind is DriverEventKind.STARTED
        assert events[-1].kind is DriverEventKind.ERROR
        assert events[-1].code == "EFFECT_SERVICE_UNAVAILABLE"
        assert events[-1].details.get("source") == "effect_service"
        assert events[-1].details.get("error_type") == "URLError"
        assert "connection refused" in events[-1].details.get("diagnostic", "")
        adapter.shutdown()


def test_native_adapter_observe_includes_sanitized_storage_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = NativeAdapter(make_config(tmp_path, "http://127.0.0.1:18402"))

    def fail_latest_run(_workflow_id: str) -> None:
        raise sqlite3.OperationalError(r"disk I/O error on C:\dev\DarkFac\secret\db.sqlite3")

    monkeypatch.setattr(adapter.store, "get_latest_run", fail_latest_run)

    events = adapter.observe(
        DriverCommand(command_id="obs-diag", action=DriverAction.OBSERVE, workflow_id="wf-obs-diag")
    )
    assert events[0].kind is DriverEventKind.ERROR
    assert events[0].code == "STORE_UNAVAILABLE"
    assert events[0].details.get("source") == "storage"
    assert events[0].details.get("error_type") == "OperationalError"
    assert "disk I/O error" in events[0].details.get("diagnostic", "")
    assert r"C:\dev" not in events[0].details.get("diagnostic", "")
    adapter.shutdown()


def test_native_adapter_executes_repeatedly_without_transient_store_error(tmp_path: Path) -> None:
    with EffectServer(tmp_path / "server_repeat") as server:
        for i in range(3):
            lab_dir = tmp_path / f"repeat_{i}"
            lab_dir.mkdir(parents=True, exist_ok=True)
            config = make_config(lab_dir, server.base_url)
            adapter = NativeAdapter(config)
            wf_id = f"workflow-repeat-{i}"
            started = adapter.start(start_command(wf_id))
            events = wait_for_terminal(adapter, wf_id)
            assert started[0].kind is DriverEventKind.STARTED
            assert events[-1].kind is DriverEventKind.COMPLETED
            assert events[-1].runtime_status is RuntimeStatus.SUCCEEDED
            adapter.shutdown()


def test_driver_diagnostics_expose_original_store_exception_when_opted_in(tmp_path: Path) -> None:
    """USR-111: STORE_UNAVAILABLE must carry the original exception type and message."""
    native_path = tmp_path / "native"
    native_path.write_text("occupied", encoding="utf-8")
    config = make_config(tmp_path, "http://127.0.0.1:18402")
    config_path = tmp_path / "lab-config.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "spikes.runtime_choice.driver", "--config", str(config_path)],
        cwd=PROJECT_ROOT,
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
        env={**os.environ, "HF02_DRIVER_DIAGNOSTICS": "1"},
    )

    assert result.returncode == 2
    lines = result.stderr.strip().splitlines()
    assert lines[0] == "STORE_UNAVAILABLE"
    assert lines[1].startswith("[diag] NativeAdapterError(STORE_UNAVAILABLE)")
    # The original OS exception type is visible, not only the stable code.
    assert any(name in lines[1] for name in ("FileExistsError", "NotADirectoryError", "OSError"))
    assert "Traceback" not in result.stderr
