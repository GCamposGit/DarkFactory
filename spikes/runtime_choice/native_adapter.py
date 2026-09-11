"""Adapter for the existing SQLite orchestrator runtime.

This module intentionally imports only ``core.orchestrator`` and the local
effect service.  It does not add waiting, cancellation, deduplication, or
versioning wrappers that the baseline does not natively provide.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import ValidationError

from core.orchestrator.runtime import OrchestratorRuntime, RuntimeContext
from core.orchestrator.store import OrchestratorStore, RunStatus, StoreError
from spikes.runtime_choice.contracts import (
    AdapterCapabilities,
    DriverAction,
    DriverCommand,
    DriverEvent,
    DriverEventKind,
    LabConfig,
    RuntimeKind,
    RuntimeStatus,
    WorkflowVersion,
)


class NativeAdapterError(RuntimeError):
    """Sanitized adapter failure with a stable code for the driver."""

    def __init__(self, code: str, message: str = "native adapter failed") -> None:
        super().__init__(message)
        self.code = code


class EffectClient:
    """Small HTTP client for the independently persisted effect oracle."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                body = json.loads(error.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = {}
            raise NativeAdapterError(str(body.get("error_code", "EFFECT_HTTP_ERROR"))) from error
        except (URLError, TimeoutError, OSError) as error:
            raise NativeAdapterError("EFFECT_SERVICE_UNAVAILABLE") from error

    def observe(self, workflow_id: str, kind: str, step_id: str, details: dict[str, Any]) -> None:
        self._post(
            "/observations",
            {
                "workflow_id": workflow_id,
                "kind": kind,
                "step_id": step_id,
                "details": details,
            },
        )

    def publish(
        self,
        *,
        operation_key: str,
        workflow_id: str,
        release_digest: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self._post(
            "/effects",
            {
                "operation_key": operation_key,
                "workflow_id": workflow_id,
                "release_digest": release_digest,
                "payload_hash": hashlib.sha256(payload_bytes).hexdigest(),
            },
        )


class NativeAdapter:
    """Map the baseline runtime to the common driver protocol."""

    _STAGES = ("S0", "S1", "S3", "S4")

    def __init__(self, config: LabConfig) -> None:
        if config.runtime is not RuntimeKind.NATIVE_SQLITE:
            raise ValueError("NativeAdapter requires runtime=native_sqlite")
        self.config = config
        self.native_root = config.root_dir / "native"
        try:
            self.native_root.mkdir(parents=True, exist_ok=True)
            self.database_path = self.native_root / "orchestrator.sqlite3"
            self.store = OrchestratorStore(self.database_path, timeout_seconds=30.0)
            self.runtime = OrchestratorRuntime(
                self.store,
                owner=f"native-{os.getpid()}-{uuid4().hex[:8]}",
                lease_seconds=config.lease_seconds,
            )
        except (OSError, sqlite3.Error, StoreError) as error:
            raise NativeAdapterError("STORE_UNAVAILABLE") from error
        self.effects = EffectClient(config.effect_base_url)
        self._events: queue.Queue[DriverEvent] = queue.Queue()
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.RLock()
        self._closed = False

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Capabilities exposed by the baseline, without optimistic wrappers."""

        return AdapterCapabilities(
            durable_steps=True,
            resume_after_crash=True,
            durable_wait=False,
            deduplicated_intake=False,
            cancel_before_next_step=False,
            version_isolation=False,
            bounded_concurrency=False,
        )

    @property
    def adapter_capabilities(self) -> AdapterCapabilities:
        return self.capabilities

    @staticmethod
    def _event(
        workflow_id: str,
        kind: DriverEventKind,
        status: RuntimeStatus,
        *,
        step_id: str | None = None,
        code: str | None = None,
    ) -> DriverEvent:
        return DriverEvent(
            event_id=f"event-{uuid4().hex}",
            workflow_id=workflow_id,
            kind=kind,
            runtime_status=status,
            step_id=step_id,
            code=code,
        )

    def _emit(self, event: DriverEvent) -> None:
        self._events.put(event)

    def poll_event(self, timeout: float | None = None) -> DriverEvent | None:
        try:
            return self._events.get(timeout=timeout)
        except queue.Empty:
            return None

    def _unsupported(self, workflow_id: str, *, capability: str) -> DriverEvent:
        return self._event(
            workflow_id,
            DriverEventKind.UNSUPPORTED,
            RuntimeStatus.UNSUPPORTED,
            code=f"CAPABILITY_UNSUPPORTED:{capability}",
        )

    def _latest_is_duplicate(self, workflow_id: str) -> bool:
        latest = self.store.get_latest_run(workflow_id)
        if latest is None:
            return False
        if latest.status is RunStatus.SUCCEEDED:
            return True
        if latest.status is not RunStatus.RUNNING:
            return False
        lease = self.store.current_lease(workflow_id)
        return lease is not None and lease.is_valid()

    def start(self, command: DriverCommand) -> list[DriverEvent]:
        if command.action is not DriverAction.START:
            raise ValueError("start() accepts only a start command")
        assert command.workflow_id is not None
        assert command.scenario_id is not None
        assert command.workflow_version is not None
        if self._closed:
            return [self._event(command.workflow_id, DriverEventKind.ERROR, RuntimeStatus.ERROR, code="DRIVER_SHUTDOWN")]
        if command.workflow_version is not WorkflowVersion.V1:
            return [self._unsupported(command.workflow_id, capability="version_isolation")]
        if not bool(command.payload.get("approval_pre_resolved", False)):
            return [self._unsupported(command.workflow_id, capability="durable_wait")]
        with self._lock:
            current = self._threads.get(command.workflow_id)
            if current is not None and current.is_alive():
                return [self._unsupported(command.workflow_id, capability="deduplicated_intake")]
            try:
                if self._latest_is_duplicate(command.workflow_id):
                    return [self._unsupported(command.workflow_id, capability="deduplicated_intake")]
            except (sqlite3.Error, OSError, StoreError):
                return [
                    self._event(
                        command.workflow_id,
                        DriverEventKind.ERROR,
                        RuntimeStatus.ERROR,
                        code="STORE_UNAVAILABLE",
                    )
                ]
            started = self._event(
                command.workflow_id,
                DriverEventKind.STARTED,
                RuntimeStatus.RUNNING,
            )
            thread = threading.Thread(
                target=self._run_workflow,
                args=(command,),
                name=f"hf02-native-{command.workflow_id}",
                daemon=True,
            )
            self._threads[command.workflow_id] = thread
            thread.start()
            return [started]

    def _run_workflow(self, command: DriverCommand) -> None:
        assert command.workflow_id is not None
        workflow_id = command.workflow_id
        payload = deepcopy(command.payload)
        release_digest = str(payload.get("release_digest", "release-A"))
        input_value = payload.get("value", 0)

        def make_step(stage: str):
            def step(context: RuntimeContext) -> dict[str, Any]:
                self._emit(self._event(workflow_id, DriverEventKind.STEP_STARTED, RuntimeStatus.RUNNING, step_id=stage))
                self.effects.observe(workflow_id, "step_started", stage, {"step_index": context.step_index})
                if stage == "S0":
                    value = {"prepared": True, "value": input_value}
                elif stage == "S1":
                    value = {"candidate": input_value, "workflow_version": WorkflowVersion.V1.value}
                elif stage == "S3":
                    effect_payload = {"value": input_value, "stage": stage, "release_digest": release_digest}
                    receipt = self.effects.publish(
                        operation_key=f"{self.config.lab_id}:{workflow_id}:S3",
                        workflow_id=workflow_id,
                        release_digest=release_digest,
                        payload=effect_payload,
                    )
                    value = {"receipt_id": receipt.get("receipt_id"), "published": True}
                else:
                    value = {"finalized": True}
                self.effects.observe(workflow_id, "step_observed", stage, {"step_index": context.step_index})
                self._emit(self._event(workflow_id, DriverEventKind.STEP_OBSERVED, RuntimeStatus.RUNNING, step_id=stage))
                return value

            return step

        try:
            result = self.runtime.run(workflow_id, [make_step(stage) for stage in self._STAGES])
            status = RuntimeStatus.SUCCEEDED if result.status is RunStatus.SUCCEEDED else RuntimeStatus.ERROR
            self._emit(self._event(workflow_id, DriverEventKind.COMPLETED, status))
        except NativeAdapterError as error:
            self._emit(self._event(workflow_id, DriverEventKind.ERROR, RuntimeStatus.ERROR, code=error.code))
        except (sqlite3.Error, OSError, StoreError):
            self._emit(self._event(workflow_id, DriverEventKind.ERROR, RuntimeStatus.ERROR, code="STORE_UNAVAILABLE"))
        except Exception:
            self._emit(self._event(workflow_id, DriverEventKind.ERROR, RuntimeStatus.ERROR, code="NATIVE_RUNTIME_ERROR"))
        finally:
            with self._lock:
                self._threads.pop(workflow_id, None)

    def observe(self, command: DriverCommand) -> list[DriverEvent]:
        assert command.workflow_id is not None
        try:
            record = self.store.get_latest_run(command.workflow_id)
        except (sqlite3.Error, OSError, StoreError):
            return [
                self._event(
                    command.workflow_id,
                    DriverEventKind.ERROR,
                    RuntimeStatus.ERROR,
                    code="STORE_UNAVAILABLE",
                )
            ]
        if record is None:
            return [
                self._event(
                    command.workflow_id,
                    DriverEventKind.ERROR,
                    RuntimeStatus.ERROR,
                    code="RUN_NOT_FOUND",
                )
            ]
        status_map = {
            RunStatus.RUNNING: RuntimeStatus.RUNNING,
            RunStatus.SUCCEEDED: RuntimeStatus.SUCCEEDED,
            RunStatus.FAILED: RuntimeStatus.FAILED,
        }
        runtime_status = status_map.get(record.status)
        if runtime_status is None:
            return [
                self._event(
                    command.workflow_id,
                    DriverEventKind.ERROR,
                    RuntimeStatus.ERROR,
                    code="NATIVE_RUNTIME_ERROR",
                )
            ]
        return [
            self._event(
                command.workflow_id,
                DriverEventKind.STEP_OBSERVED,
                runtime_status,
                step_id=f"S{record.step_index}",
            )
        ]

    def approve(self, command: DriverCommand) -> list[DriverEvent]:
        assert command.workflow_id is not None
        return [self._unsupported(command.workflow_id, capability="durable_wait")]

    def cancel(self, command: DriverCommand) -> list[DriverEvent]:
        assert command.workflow_id is not None
        return [self._unsupported(command.workflow_id, capability="cancel_before_next_step")]

    def shutdown(self) -> None:
        self._closed = True
        with self._lock:
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=self.config.scenario_timeout_seconds)

    def dispatch(self, command: DriverCommand) -> list[DriverEvent]:
        if command.action is DriverAction.START:
            return self.start(command)
        if command.action is DriverAction.OBSERVE:
            return self.observe(command)
        if command.action is DriverAction.APPROVE:
            return self.approve(command)
        if command.action is DriverAction.CANCEL:
            return self.cancel(command)
        if command.action is DriverAction.SHUTDOWN:
            self.shutdown()
            return []
        raise NativeAdapterError("UNKNOWN_ACTION")


__all__ = ["EffectClient", "NativeAdapter", "NativeAdapterError"]
