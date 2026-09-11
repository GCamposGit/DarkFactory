"""Loopback HTTP service for the independent HF-02 effect oracle."""

from __future__ import annotations

import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from pydantic import ValidationError

from spikes.runtime_choice.effect_store import (
    MAX_REQUEST_BYTES,
    ApprovalConflictError,
    ApprovalRequest,
    ApprovalSubjectConflictError,
    ApprovalSubjectNotBoundError,
    EffectConflictError,
    EffectRequest,
    NativeEffectStore,
    ObservationRequest,
)


LOGGER = logging.getLogger(__name__)


class EffectServer:
    """Manage a dynamic-port loopback server backed by ``NativeEffectStore``."""

    def __init__(self, lab_root: Path | str, *, host: str = "127.0.0.1", port: int = 0) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("effect server must bind to a loopback host")
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self.host = host
        self.requested_port = port
        self.store = NativeEffectStore(lab_root)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._httpd is None:
            return self.requested_port
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def is_running(self) -> bool:
        return self._httpd is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> str:
        if self.is_running:
            return self.base_url
        service = self

        class Handler(_EffectRequestHandler):
            effect_server = service

        self._httpd = ThreadingHTTPServer((self.host, self.requested_port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="hf02-effect-server",
            daemon=True,
        )
        self._thread.start()
        return self.base_url

    def stop(self) -> None:
        httpd, thread = self._httpd, self._thread
        self._httpd = None
        self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)

    def arm_disconnect_once(self, operation_key: str) -> None:
        self.store.arm_disconnect_once(operation_key)

    def __enter__(self) -> EffectServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


class _EffectRequestHandler(BaseHTTPRequestHandler):
    effect_server: EffectServer

    server_version = "HF02EffectServer/1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        LOGGER.debug("effect service: %s", format % args)

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _error(self, status: int, code: str) -> None:
        self._write_json(status, {"error_code": code})

    def _read_json(self) -> dict[str, Any] | None:
        length_header = self.headers.get("Content-Length")
        try:
            length = int(length_header or "-1")
        except ValueError:
            self._error(400, "INVALID_CONTENT_LENGTH")
            return None
        if length < 0:
            self._error(411, "CONTENT_LENGTH_REQUIRED")
            return None
        if length > MAX_REQUEST_BYTES:
            # Consume only enough bytes to complete a bounded rejection.  This
            # keeps local clients from seeing a reset on Windows while avoiding
            # an unbounded read of attacker-controlled Content-Length values.
            self.rfile.read(MAX_REQUEST_BYTES + 1)
            self.close_connection = True
            self._error(413, "REQUEST_TOO_LARGE")
            return None
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "INVALID_JSON")
            return None
        if not isinstance(value, dict):
            self._error(400, "JSON_OBJECT_REQUIRED")
            return None
        return value

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/effects/count":
            self._write_json(200, {"count": self.effect_server.store.effect_count()})
            return
        if path.startswith("/effects/"):
            key = unquote(path.removeprefix("/effects/"))
            receipt = self.effect_server.store.get_effect(key)
            if receipt is None:
                self._error(404, "EFFECT_NOT_FOUND")
                return
            self._write_json(200, receipt.model_dump(mode="json"))
            return
        if path in {"/observations", "/observations/count"}:
            query = parse_qs(parsed.query)
            workflow_id = (query.get("workflow_id") or [None])[0]
            if workflow_id is None:
                self._error(400, "WORKFLOW_ID_REQUIRED")
                return
            observations = self.effect_server.store.observations(workflow_id)
            if path.endswith("/count"):
                self._write_json(200, {"count": len(observations)})
            else:
                self._write_json(200, {"observations": [item.model_dump(mode="json") for item in observations]})
            return
        self._error(404, "NOT_FOUND")

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        payload = self._read_json()
        if payload is None:
            return
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/effects":
                commit = self.effect_server.store.apply_effect(payload)
                if commit.disconnect_after_commit:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                self._write_json(201 if commit.created else 200, commit.receipt.model_dump(mode="json"))
                return
            if parsed.path == "/observations":
                record = self.effect_server.store.record_observation(ObservationRequest.model_validate(payload))
                self._write_json(201, record.model_dump(mode="json"))
                return
            if parsed.path == "/approvals":
                record = self.effect_server.store.record_approval(ApprovalRequest.model_validate(payload))
                self._write_json(201, record.model_dump(mode="json"))
                return
        except (ValidationError, ValueError):
            self._error(400, "CONTRACT_INVALID")
            return
        except EffectConflictError:
            self._error(409, "EFFECT_CONFLICT")
            return
        except ApprovalSubjectNotBoundError:
            self._error(409, "APPROVAL_SUBJECT_NOT_BOUND")
            return
        except ApprovalSubjectConflictError:
            self._error(409, "APPROVAL_SUBJECT_CONFLICT")
            return
        except ApprovalConflictError:
            self._error(409, "APPROVAL_CONFLICT")
            return
        self._error(404, "NOT_FOUND")


__all__ = ["EffectServer"]
