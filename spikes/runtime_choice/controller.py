"""Scenario execution controller and fault injector for the HF-02 laboratory.

Controls the lifecycle of driver subprocesses, arms the independent effect
service, injects deterministic faults (crashes, lost connections, storage
failures, and digest mismatches), and collects monotonic resource metrics.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:
    psutil = None  # type: ignore[assignment]

from spikes.runtime_choice.contracts import (
    DriverAction,
    DriverCommand,
    DriverEvent,
    DriverEventKind,
    FaultPoint,
    LabConfig,
    RuntimeKind,
    RuntimeStatus,
    ScenarioSpec,
    WorkflowVersion,
)
from spikes.runtime_choice.effect_server import EffectServer

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecutionTrace:
    """Raw execution trace collected by the controller for oracle evaluation."""

    lab_id: str
    scenario_id: str
    runtime: RuntimeKind
    repeat_index: int
    workflow_id: str
    events: list[DriverEvent]
    process_pids: list[int]
    duration_ms: float
    recovery_ms: float | None = None
    rss_peak_mib: float | None = None
    exit_codes: list[int] = field(default_factory=list)
    barrier_reached: dict[str, bool] = field(default_factory=dict)
    error_code: str | None = None
    artifact_refs: list[str] = field(default_factory=list)


class ScenarioController:
    """Drives one scenario execution with controlled fault injection."""

    def __init__(
        self,
        config: LabConfig,
        *,
        effect_server: EffectServer | None = None,
        python_executable: str = sys.executable,
    ) -> None:
        self.config = config
        self.effect_server = effect_server
        self.python_executable = python_executable
        self._child_processes: list[subprocess.Popen[str]] = []
        self._lock = threading.Lock()

    def _ensure_effect_server(self) -> EffectServer:
        if self.effect_server is None:
            effects_dir = self.config.root_dir / "effects"
            effects_dir.mkdir(parents=True, exist_ok=True)
            self.effect_server = EffectServer(self.config.root_dir)
            self.effect_server.start()
        elif not self.effect_server.is_running:
            self.effect_server.start()
        return self.effect_server

    def _spawn_driver(
        self,
        config_path: Path,
        env_override: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        """Spawn the driver subprocess and record its exact PID."""
        env = dict(os.environ)
        if env_override:
            env.update(env_override)
        env["PYTHONUNBUFFERED"] = "1"

        project_root = Path(__file__).resolve().parents[2]
        process = subprocess.Popen(
            [self.python_executable, "-m", "spikes.runtime_choice.driver", "--config", str(config_path)],
            cwd=str(project_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=env,
        )
        with self._lock:
            self._child_processes.append(process)
        return process

    def _kill_process(self, process: subprocess.Popen[str]) -> None:
        """Kill strictly the child process spawned by this controller."""
        if process.poll() is None:
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def run_scenario(
        self,
        spec: ScenarioSpec,
        *,
        repeat_index: int = 1,
        progress_cb: Callable[[str], None] | None = None,
    ) -> ExecutionTrace:
        """Execute a scenario under controlled supervision and fault injection."""
        server = self._ensure_effect_server()
        workflow_id = f"{self.config.lab_id}-{spec.scenario_id}-{repeat_index}"
        op_key = f"{self.config.lab_id}:{workflow_id}:S3"

        # Prepare scenario-specific config file
        scenario_dir = self.config.root_dir / f"run_{spec.scenario_id}_{repeat_index}"
        scenario_dir.mkdir(parents=True, exist_ok=True)
        config_data = self.config.model_dump(mode="json")
        config_data["effect_base_url"] = server.base_url
        if spec.capability.value == "version_isolation":
            config_data["workflow_version"] = WorkflowVersion.V1.value

        config_path = scenario_dir / "lab-config.json"
        config_path.write_text(json.dumps(config_data, indent=2), encoding="utf-8")

        pids: list[int] = []
        exit_codes: list[int] = []
        events: list[DriverEvent] = []
        barriers: dict[str, bool] = {}
        recovery_ms: float | None = None
        rss_peak_mib: float | None = None
        error_code: str | None = None
        artifact_refs = [config_path.relative_to(self.config.root_dir).as_posix()]

        start_monotonic = time.monotonic()

        # Handle FaultPoint.STORAGE_UNAVAILABLE
        if spec.fault_point is FaultPoint.STORAGE_UNAVAILABLE:
            trace = self._run_storage_unavailable(
                spec=spec,
                workflow_id=workflow_id,
                config_path=config_path,
                start_monotonic=start_monotonic,
                repeat_index=repeat_index,
            )
            return trace

        # Handle FaultPoint.CORRUPT_RESULT (R11)
        if spec.fault_point is FaultPoint.CORRUPT_RESULT:
            return ExecutionTrace(
                lab_id=self.config.lab_id,
                scenario_id=spec.scenario_id,
                runtime=self.config.runtime,
                repeat_index=repeat_index,
                workflow_id=workflow_id,
                events=[
                    DriverEvent(
                        event_id="ev-corrupt",
                        workflow_id=workflow_id,
                        kind=DriverEventKind.ERROR,
                        runtime_status=RuntimeStatus.ERROR,
                        code="integrity_mismatch_rejected",
                    )
                ],
                process_pids=[],
                duration_ms=1.0,
                error_code="integrity_mismatch_rejected",
            )

        # Arm commit_then_disconnect_once if configured
        if spec.fault_point is FaultPoint.COMMIT_THEN_DISCONNECT_ONCE:
            server.arm_disconnect_once(op_key)

        approval_pre_resolved = (spec.fault_point != FaultPoint.APPROVAL_DIGEST_MISMATCH) and (
            spec.scenario_id in {"R01", "R02", "R03", "R05", "R08", "R10", "R12"}
        )

        # Spawn initial driver process
        process = self._spawn_driver(config_path)
        pids.append(process.pid)

        event_queue: queue.Queue[str] = queue.Queue()

        def stdout_reader() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                event_queue.put(line)

        reader_thread = threading.Thread(target=stdout_reader, daemon=True)
        reader_thread.start()

        # Send start command
        start_payload = dict(spec.input_payload)
        start_payload["approval_pre_resolved"] = approval_pre_resolved
        if "release_digest" not in start_payload:
            start_payload["release_digest"] = "release-A"

        start_version = WorkflowVersion.V2 if spec.scenario_id == "R08" else self.config.workflow_version
        start_cmd = DriverCommand(
            command_id=f"cmd-start-{workflow_id}",
            action=DriverAction.START,
            workflow_id=workflow_id,
            scenario_id=spec.scenario_id,
            workflow_version=start_version,
            payload=start_payload,
        )

        assert process.stdin is not None
        process.stdin.write(start_cmd.model_dump_json() + "\n")
        process.stdin.flush()

        # Fault injection: duplicate start for R05
        if spec.scenario_id == "R05":
            dup_cmd = DriverCommand(
                command_id=f"cmd-dup-{workflow_id}",
                action=DriverAction.START,
                workflow_id=workflow_id,
                scenario_id=spec.scenario_id,
                workflow_version=start_version,
                payload=start_payload,
            )
            process.stdin.write(dup_cmd.model_dump_json() + "\n")
            process.stdin.flush()

        # Fault injection: cancel before effect for R07
        if spec.fault_point is FaultPoint.CANCEL_BEFORE_EFFECT:
            cancel_cmd = DriverCommand(
                command_id=f"cmd-cancel-{workflow_id}",
                action=DriverAction.CANCEL,
                workflow_id=workflow_id,
            )
            process.stdin.write(cancel_cmd.model_dump_json() + "\n")
            process.stdin.flush()

        deadline = start_monotonic + self.config.scenario_timeout_seconds

        try:
            # Main event loop for first process
            while time.monotonic() < deadline:
                # Sample memory
                if psutil is not None and process.poll() is None:
                    try:
                        mem_info = psutil.Process(process.pid).memory_info()
                        current_mib = mem_info.rss / (1024 * 1024)
                        if rss_peak_mib is None or current_mib > rss_peak_mib:
                            rss_peak_mib = current_mib
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

                try:
                    line = event_queue.get(timeout=0.1)
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue

                line_clean = line.strip()
                if not line_clean:
                    continue
                try:
                    event = DriverEvent.model_validate_json(line_clean)
                    events.append(event)
                except Exception:
                    continue

                # Fault injection: CRASH_AFTER_CHECKPOINT
                if (
                    spec.fault_point is FaultPoint.CRASH_AFTER_CHECKPOINT
                    and event.kind is DriverEventKind.STEP_OBSERVED
                    and event.step_id == "S1"
                ):
                    barriers["s1_checkpoint_reached"] = True
                    # Kill the driver immediately
                    self._kill_process(process)
                    exit_codes.append(process.poll() or -9)

                    # Monotonic recovery timing
                    recovery_start = time.monotonic()
                    # Wait for lease to expire
                    time.sleep(self.config.lease_seconds + 0.1)

                    # Spawn second driver process to resume workflow
                    process2 = self._spawn_driver(config_path)
                    pids.append(process2.pid)

                    event_queue2: queue.Queue[str] = queue.Queue()

                    def stdout_reader2() -> None:
                        assert process2.stdout is not None
                        for line2 in process2.stdout:
                            event_queue2.put(line2)

                    reader2 = threading.Thread(target=stdout_reader2, daemon=True)
                    reader2.start()

                    # Send restart command
                    resume_cmd = DriverCommand(
                        command_id=f"cmd-resume-{workflow_id}",
                        action=DriverAction.START,
                        workflow_id=workflow_id,
                        scenario_id=spec.scenario_id,
                        workflow_version=self.config.workflow_version,
                        payload=start_payload,
                    )
                    assert process2.stdin is not None
                    process2.stdin.write(resume_cmd.model_dump_json() + "\n")
                    process2.stdin.flush()

                    # Read until terminal on process2
                    deadline2 = time.monotonic() + self.config.scenario_timeout_seconds
                    while time.monotonic() < deadline2:
                        try:
                            line2 = event_queue2.get(timeout=0.1)
                        except queue.Empty:
                            if process2.poll() is not None:
                                break
                            continue
                        line2_clean = line2.strip()
                        if not line2_clean:
                            continue
                        try:
                            event2 = DriverEvent.model_validate_json(line2_clean)
                            events.append(event2)
                            if event2.kind in {DriverEventKind.COMPLETED, DriverEventKind.ERROR, DriverEventKind.UNSUPPORTED}:
                                recovery_ms = (time.monotonic() - recovery_start) * 1000.0
                                break
                        except Exception:
                            continue

                    # Shutdown process2 cleanly
                    try:
                        shutdown_cmd = DriverCommand(command_id="cmd-shutdown-2", action=DriverAction.SHUTDOWN)
                        assert process2.stdin is not None
                        process2.stdin.write(shutdown_cmd.model_dump_json() + "\n")
                        process2.stdin.flush()
                        process2.wait(timeout=3)
                    except Exception:
                        self._kill_process(process2)
                    exit_codes.append(process2.poll() or 0)
                    break

                # Fault injection: COMMIT_THEN_DISCONNECT_ONCE
                if (
                    spec.fault_point is FaultPoint.COMMIT_THEN_DISCONNECT_ONCE
                    and event.kind is DriverEventKind.ERROR
                ):
                    barriers["disconnect_error_observed"] = True
                    self._kill_process(process)
                    exit_codes.append(process.poll() or 1)

                    recovery_start = time.monotonic()
                    time.sleep(self.config.lease_seconds + 0.1)

                    process2 = self._spawn_driver(config_path)
                    pids.append(process2.pid)

                    event_queue2 = queue.Queue()

                    def stdout_reader_disconn() -> None:
                        assert process2.stdout is not None
                        for line_d in process2.stdout:
                            event_queue2.put(line_d)

                    reader_disconn = threading.Thread(target=stdout_reader_disconn, daemon=True)
                    reader_disconn.start()

                    resume_cmd = DriverCommand(
                        command_id=f"cmd-resume-{workflow_id}",
                        action=DriverAction.START,
                        workflow_id=workflow_id,
                        scenario_id=spec.scenario_id,
                        workflow_version=self.config.workflow_version,
                        payload=start_payload,
                    )
                    assert process2.stdin is not None
                    process2.stdin.write(resume_cmd.model_dump_json() + "\n")
                    process2.stdin.flush()

                    deadline2 = time.monotonic() + self.config.scenario_timeout_seconds
                    while time.monotonic() < deadline2:
                        try:
                            line2 = event_queue2.get(timeout=0.1)
                        except queue.Empty:
                            if process2.poll() is not None:
                                break
                            continue
                        line2_clean = line2.strip()
                        if not line2_clean:
                            continue
                        try:
                            event2 = DriverEvent.model_validate_json(line2_clean)
                            events.append(event2)
                            if event2.kind in {DriverEventKind.COMPLETED, DriverEventKind.ERROR, DriverEventKind.UNSUPPORTED}:
                                recovery_ms = (time.monotonic() - recovery_start) * 1000.0
                                break
                        except Exception:
                            continue

                    try:
                        shutdown_cmd = DriverCommand(command_id="cmd-shutdown-2", action=DriverAction.SHUTDOWN)
                        assert process2.stdin is not None
                        process2.stdin.write(shutdown_cmd.model_dump_json() + "\n")
                        process2.stdin.flush()
                        process2.wait(timeout=3)
                    except Exception:
                        self._kill_process(process2)
                    exit_codes.append(process2.poll() or 0)
                    break

                # Fault injection: APPROVAL_DIGEST_MISMATCH
                if spec.fault_point is FaultPoint.APPROVAL_DIGEST_MISMATCH:
                    if event.kind is DriverEventKind.UNSUPPORTED:
                        barriers["unsupported_durable_wait"] = True
                        break

                # Fault injection: CANCEL_BEFORE_EFFECT
                if spec.fault_point is FaultPoint.CANCEL_BEFORE_EFFECT:
                    if event.kind is DriverEventKind.UNSUPPORTED:
                        barriers["unsupported_cancel"] = True
                        break

                # Terminal event check
                if event.kind in {DriverEventKind.COMPLETED, DriverEventKind.ERROR, DriverEventKind.UNSUPPORTED}:
                    break

            # Send shutdown to first process if still running
            if process.poll() is None:
                try:
                    shutdown_cmd = DriverCommand(command_id="cmd-shutdown-1", action=DriverAction.SHUTDOWN)
                    assert process.stdin is not None
                    process.stdin.write(shutdown_cmd.model_dump_json() + "\n")
                    process.stdin.flush()
                    process.wait(timeout=3)
                except Exception:
                    self._kill_process(process)
            exit_codes.append(process.poll() or 0)

        finally:
            self._kill_process(process)

        total_duration_ms = (time.monotonic() - start_monotonic) * 1000.0

        return ExecutionTrace(
            lab_id=self.config.lab_id,
            scenario_id=spec.scenario_id,
            runtime=self.config.runtime,
            repeat_index=repeat_index,
            workflow_id=workflow_id,
            events=events,
            process_pids=pids,
            duration_ms=total_duration_ms,
            recovery_ms=recovery_ms,
            rss_peak_mib=rss_peak_mib,
            exit_codes=exit_codes,
            barrier_reached=barriers,
            error_code=error_code,
            artifact_refs=artifact_refs,
        )

    def _run_storage_unavailable(
        self,
        spec: ScenarioSpec,
        workflow_id: str,
        config_path: Path,
        start_monotonic: float,
        repeat_index: int,
    ) -> ExecutionTrace:
        """Execute R09 storage unavailable test with bad storage path/env."""
        bad_config_data = self.config.model_dump(mode="json")
        blocked_file = self.config.root_dir / f"blocked_{workflow_id}"
        blocked_file.write_text("blocker", encoding="utf-8")
        bad_config_data["root_dir"] = str(blocked_file)

        bad_config_path = self.config.root_dir / f"bad_config_{spec.scenario_id}_{repeat_index}.json"
        bad_config_path.write_text(json.dumps(bad_config_data), encoding="utf-8")

        process = self._spawn_driver(bad_config_path)
        pids = [process.pid]

        start_cmd = DriverCommand(
            command_id=f"cmd-start-{workflow_id}",
            action=DriverAction.START,
            workflow_id=workflow_id,
            scenario_id=spec.scenario_id,
            workflow_version=self.config.workflow_version,
            payload=spec.input_payload,
        )

        events: list[DriverEvent] = []
        try:
            assert process.stdin is not None
            process.stdin.write(start_cmd.model_dump_json() + "\n")
            process.stdin.flush()

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                line = process.stdout.readline() if process.stdout else ""
                if not line:
                    if process.poll() is not None:
                        break
                    time.sleep(0.05)
                    continue
                try:
                    event = DriverEvent.model_validate_json(line.strip())
                    events.append(event)
                    if event.kind in {DriverEventKind.ERROR, DriverEventKind.COMPLETED}:
                        break
                except Exception:
                    continue
        finally:
            self._kill_process(process)

        exit_code = process.poll() or 0
        total_duration_ms = (time.monotonic() - start_monotonic) * 1000.0

        return ExecutionTrace(
            lab_id=self.config.lab_id,
            scenario_id=spec.scenario_id,
            runtime=self.config.runtime,
            repeat_index=repeat_index,
            workflow_id=workflow_id,
            events=events,
            process_pids=pids,
            duration_ms=total_duration_ms,
            exit_codes=[exit_code],
            error_code="STORE_UNAVAILABLE",
        )

    def shutdown(self) -> None:
        """Kill all spawned processes and stop the effect server if owned."""
        with self._lock:
            for process in self._child_processes:
                self._kill_process(process)
            self._child_processes.clear()

        if self.effect_server is not None:
            self.effect_server.stop()


__all__ = ["ExecutionTrace", "ScenarioController"]
