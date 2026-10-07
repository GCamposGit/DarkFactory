"""JSONL process driver for the isolated HF-02 adapters."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

from pydantic import ValidationError

from spikes.runtime_choice.contracts import (
    DriverAction,
    DriverCommand,
    DriverEvent,
    DriverEventKind,
    LabConfig,
    RuntimeKind,
    RuntimeStatus,
)


class DriverConfigurationError(RuntimeError):
    """A sanitized configuration/optional-dependency failure."""

    def __init__(self, code: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


def describe_exception_chain(error: BaseException | None) -> str:
    """Return ``Type(code): message <- Type: message`` for an exception chain.

    Messages are sanitized (paths and secrets removed, truncated) so the text is
    safe for CI logs while still exposing the original exception type, which the
    sanitized ``STORE_UNAVAILABLE`` code alone hides.
    """

    from spikes.runtime_choice.native_adapter import sanitize_diagnostic

    parts: list[str] = []
    seen: set[int] = set()
    current = error
    while current is not None and id(current) not in seen and len(parts) < 6:
        seen.add(id(current))
        code = getattr(current, "code", None)
        label = type(current).__name__
        if isinstance(code, str):
            label = f"{label}({code})"
        parts.append(f"{label}: {sanitize_diagnostic(current)}")
        details = getattr(current, "details", None)
        if isinstance(details, dict) and details.get("error_type"):
            parts.append(
                f"{details['error_type']}: {sanitize_diagnostic(details.get('diagnostic', 'unknown'))}"
            )
        current = current.__cause__ or current.__context__
    text = " <- ".join(parts) if parts else "unknown"
    # ASCII-only: localized OS messages (e.g. cp1252 on Windows) must not break
    # the UTF-8 pipe readers that capture driver stderr.
    return text.encode("ascii", "backslashreplace").decode("ascii")


def load_config(path: Path | str) -> LabConfig:
    """Load a config file without reading a DSN into logs or output."""

    config_path = Path(path)
    try:
        return LabConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValidationError, ValueError) as error:
        raise DriverConfigurationError("CONFIG_INVALID") from error


def build_adapter(config: LabConfig):
    """Select an adapter lazily so common imports never load DBOS."""

    if config.runtime is RuntimeKind.NATIVE_SQLITE:
        from spikes.runtime_choice.native_adapter import NativeAdapter, NativeAdapterError

        try:
            return NativeAdapter(config)
        except (OSError, sqlite3.Error, NativeAdapterError) as error:
            raise DriverConfigurationError(
                "STORE_UNAVAILABLE",
                details={"cause": describe_exception_chain(error)},
            ) from error
    if config.runtime is RuntimeKind.DBOS_POSTGRES:
        try:
            from spikes.runtime_choice.dbos_adapter import DBOSAdapter  # type: ignore[import-not-found]
        except ImportError as error:
            raise DriverConfigurationError("DBOS_ADAPTER_UNAVAILABLE") from error
        try:
            return DBOSAdapter(config)
        except Exception as error:
            code = getattr(error, "code", "STORE_UNAVAILABLE")
            raise DriverConfigurationError(code) from error
    raise DriverConfigurationError("RUNTIME_UNSUPPORTED")


class DriverSession:
    """In-process protocol facade used by the CLI and deterministic tests."""

    def __init__(self, config: LabConfig) -> None:
        self.adapter = build_adapter(config)

    def dispatch(self, command: DriverCommand) -> list[DriverEvent]:
        return self.adapter.dispatch(command)


def _protocol_error(
    code: str,
    workflow_id: str = "driver-error",
    *,
    details: dict[str, Any] | None = None,
) -> DriverEvent:
    return DriverEvent(
        event_id="event-driver-error",
        workflow_id=workflow_id,
        kind=DriverEventKind.ERROR,
        runtime_status=RuntimeStatus.ERROR,
        code=code,
        details=details or {},
    )


def run_jsonl(config: LabConfig, input_stream: TextIO, output_stream: TextIO) -> int:
    """Run the JSONL loop while background workflows emit asynchronously."""

    session = DriverSession(config)
    write_lock = threading.Lock()
    stop_pump = threading.Event()

    def write(event: DriverEvent) -> None:
        with write_lock:
            output_stream.write(event.model_dump_json() + "\n")
            output_stream.flush()

    def pump() -> None:
        while not stop_pump.is_set():
            event = session.adapter.poll_event(timeout=0.05)
            if event is not None:
                write(event)
        # Drain any remaining events in the queue before terminating
        while True:
            event = session.adapter.poll_event(timeout=0.0)
            if event is None:
                break
            write(event)

    pump_thread = threading.Thread(target=pump, name="hf02-driver-events", daemon=True)
    pump_thread.start()
    exit_code = 0
    try:
        for line in input_stream:
            if not line.strip():
                continue
            try:
                command = DriverCommand.model_validate_json(line)
                events = session.dispatch(command)
            except (ValidationError, ValueError):
                exit_code = 3
                write(_protocol_error("CONTRACT_INVALID"))
                continue
            except DriverConfigurationError as error:
                exit_code = 2
                err_details = getattr(error, "details", {})
                write(_protocol_error(error.code, details=err_details))
                continue
            except Exception as error:
                code = getattr(error, "code", None)
                if code is not None and isinstance(code, str):
                    exit_code = 2
                    target_id = getattr(command, "workflow_id", None) or "driver-error"
                    err_details = getattr(error, "details", {})
                    write(_protocol_error(code, workflow_id=target_id, details=err_details))
                    continue
                raise
            for event in events:
                write(event)
            if command.action is DriverAction.SHUTDOWN:
                break
    finally:
        session.adapter.shutdown()
        stop_pump.set()
        pump_thread.join(timeout=2)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HF-02 runtime spike JSONL driver")
    parser.add_argument("--config", type=Path, default=os.environ.get("HF02_CONFIG"))
    args = parser.parse_args(argv)
    if args.config is None:
        print("CONFIG_REQUIRED", file=sys.stderr)
        return 2
    try:
        config = load_config(args.config)
        return run_jsonl(config, sys.stdin, sys.stdout)
    except DriverConfigurationError as error:
        print(error.code, file=sys.stderr)
        # The bare code stays the default stderr contract; the scenario
        # controller opts in to the original exception type and message.
        if os.environ.get("HF02_DRIVER_DIAGNOSTICS") == "1":
            cause = error.details.get("cause") or describe_exception_chain(error.__cause__)
            print(f"[diag] {cause}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DriverConfigurationError",
    "DriverSession",
    "build_adapter",
    "describe_exception_chain",
    "load_config",
    "main",
    "run_jsonl",
]
