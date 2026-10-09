"""Tests for VPS disk space monitoring, safe Docker cache cleanup, and alerting (USR-122)."""

from __future__ import annotations

import inspect
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from core.infra import vps_cleanup as vc
from core.infra.vps_cleanup import (
    DEFAULT_DISK_ALERT_THRESHOLD,
    check_vps_disk_and_alert,
    clean_vps_docker_cache,
    clean_vps_if_configured,
    get_local_disk_usage,
    is_disk_space_failure,
)
from scripts.dokploy_redeploy import (
    Deployment,
    detect_disk_space_failure,
    clean_dokploy_build_cache_and_images,
)


def test_is_disk_space_failure_matches_known_patterns() -> None:
    apt_err = "E: You don't have enough free space in /var/cache/apt/archives/."
    assert is_disk_space_failure(apt_err) is not None

    enospc_err = "npm ERR! syscall write ENOSPC"
    assert is_disk_space_failure(enospc_err) is not None

    nospace_err = "failed to copy: no space left on device"
    assert is_disk_space_failure(nospace_err) is not None

    assert is_disk_space_failure("Build succeeded") is None
    assert is_disk_space_failure("") is None
    assert is_disk_space_failure(None) is None


def test_clean_vps_docker_cache_calls_safe_endpoints() -> None:
    calls: List[tuple[str, str, Optional[Dict[str, Any]]]] = []

    def mock_transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        calls.append((method, path, payload))
        return {"ok": True}

    res = clean_vps_docker_cache("https://dokploy.example.com", "fake-key", transport=mock_transport)

    assert res["cleanDockerBuilder"] is True
    assert res["cleanUnusedImages"] is True
    assert res["errors"] == []
    assert res["hygiene_status"] == "ok"
    assert res["attempts"] == {"cleanDockerBuilder": 1, "cleanUnusedImages": 1}
    assert len(calls) == 2
    assert calls[0] == ("POST", "/api/settings.cleanDockerBuilder", {})
    assert calls[1] == ("POST", "/api/settings.cleanUnusedImages", {})


def test_clean_vps_docker_cache_handles_partial_failure() -> None:
    def failing_transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if "cleanDockerBuilder" in path:
            raise RuntimeError("API timeout")
        return {"ok": True}

    res = clean_vps_docker_cache("https://dokploy.example.com", "fake-key", transport=failing_transport)

    assert res["cleanDockerBuilder"] is False
    assert res["cleanUnusedImages"] is True
    assert res["outcomes"]["cleanDockerBuilder"] == "transient"
    assert res["outcomes"]["cleanUnusedImages"] == "ok"
    assert res["hygiene_status"] == "transient"
    assert res["attempts"]["cleanDockerBuilder"] == 1
    assert len(res["errors"]) == 1
    assert "cleanDockerBuilder failed" in res["errors"][0]


def test_check_vps_disk_and_alert_thresholds() -> None:
    notified_messages: List[str] = []

    def mock_notifier(msg: str) -> bool:
        notified_messages.append(msg)
        return True

    # Below threshold (85%) -> no alert
    assert check_vps_disk_and_alert(80.0, threshold=85.0, notifier=mock_notifier) is False
    assert check_vps_disk_and_alert(85.0, threshold=85.0, notifier=mock_notifier) is False
    assert len(notified_messages) == 0

    # Above threshold (85%) -> triggers alert
    assert check_vps_disk_and_alert(85.1, threshold=85.0, notifier=mock_notifier) is True
    assert len(notified_messages) == 1
    assert "85.1% em uso" in notified_messages[0]
    assert "[ALERTA DE DISCO VPS]" in notified_messages[0]


def test_get_local_disk_usage_returns_valid_structure() -> None:
    usage = get_local_disk_usage("/")
    assert "total_gb" in usage
    assert "used_gb" in usage
    assert "free_gb" in usage
    assert "disk_percent" in usage
    assert isinstance(usage["total_gb"], (int, float))
    assert usage["total_gb"] >= 0


def test_clean_vps_if_configured_without_creds_returns_none() -> None:
    with patch("core.infra.vps_cleanup.read_dokploy_credentials", return_value=(None, None)):
        res = clean_vps_if_configured()
        assert res is None


def test_clean_vps_if_configured_with_creds() -> None:
    with patch("core.infra.vps_cleanup.read_dokploy_credentials", return_value=("http://mock", "key")):
        with patch("core.infra.vps_cleanup.clean_vps_docker_cache", return_value={"cleanDockerBuilder": True}):
            res = clean_vps_if_configured()
            assert res == {"cleanDockerBuilder": True}


def test_detect_disk_space_failure_in_deployment() -> None:
    dep_ok = Deployment(deployment_id="d1", status="done", title="Deployed", created_at=None)
    assert detect_disk_space_failure(dep_ok) is None

    dep_fail = Deployment(
        deployment_id="d2",
        status="error",
        title="Failed",
        created_at=None,
        error_message="E: You don't have enough free space in /var/cache/apt/archives/",
    )
    assert detect_disk_space_failure(dep_fail) is not None

    dep_desc_fail = Deployment(
        deployment_id="d3",
        status="error",
        title="Failed",
        created_at=None,
        description="write ENOSPC in /tmp/build",
    )
    assert detect_disk_space_failure(dep_desc_fail) is not None


def test_clean_dokploy_build_cache_and_images_delegates() -> None:
    calls = []

    def mock_transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        calls.append((method, path))
        return {}

    clean_dokploy_build_cache_and_images(mock_transport)
    assert len(calls) == 2
    assert ("/api/settings.cleanDockerBuilder" in calls[0][1])
    assert ("/api/settings.cleanUnusedImages" in calls[1][1])


def test_dokploy_redeploy_arg_parser_has_clean_flag() -> None:
    from scripts.dokploy_redeploy import build_arg_parser

    parser = build_arg_parser()
    args = parser.parse_args(["--clean"])
    assert args.clean is True


def test_deploy_socket_budget_is_shorter_than_the_dokploy_prune_wait() -> None:
    """USR-180 diagnosis: the redeploy client polls with a 30s socket timeout.

    Dokploy holds settings.cleanUnusedImages for up to 300s (dockerSafeExec) while
    `docker image prune --all --force` runs. A read past 30s is the closeout line
    `settings.cleanUnusedImages failed: The read operation timed out`.
    """
    from scripts.dokploy_redeploy import make_urllib_transport

    default = inspect.signature(make_urllib_transport).parameters["timeout"].default
    assert default == 30.0
    assert default < vc.DOKPLOY_DOCKER_SAFE_EXEC_WAIT_SECONDS <= vc.PRUNE_TIMEOUT_SECONDS
    assert vc.timeout_for_dokploy_path("/api/project.all", default) == default
    assert vc.timeout_for_dokploy_path("/api/settings.cleanUnusedImages", default) == vc.PRUNE_TIMEOUT_SECONDS
    assert vc.timeout_for_dokploy_path("/api/settings.cleanDockerBuilder?x=1", 10) == vc.PRUNE_TIMEOUT_SECONDS


def test_socket_read_past_the_deadline_matches_the_closeout_timeout() -> None:
    """A peer that accepts the POST and sends nothing raises TimeoutError.

    On Windows the message is `The read operation timed out`, the same text the
    USR-134 closeout wrote after `settings.cleanUnusedImages failed:`.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)
    port = listener.getsockname()[1]

    def _hold() -> None:
        try:
            conn, _addr = listener.accept()
        except OSError:
            return
        conn.settimeout(2)
        try:
            conn.recv(65536)
            time.sleep(1.0)
        except OSError:
            pass
        finally:
            conn.close()

    worker = threading.Thread(target=_hold, daemon=True)
    worker.start()
    client = socket.create_connection(("127.0.0.1", port), timeout=1)
    try:
        client.sendall(
            b"POST /api/settings.cleanUnusedImages HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
        )
        client.settimeout(0.2)
        with pytest.raises(TimeoutError) as caught:
            while client.recv(4096):
                pass
        message = str(caught.value)
        assert "timed out" in message.lower()
        assert vc.prune_failure_kind(caught.value) == "read_timeout"
        recorded = f"settings.cleanUnusedImages failed: {caught.value}"
        assert recorded.startswith("settings.cleanUnusedImages failed:")
        assert "timed out" in recorded.lower()
    finally:
        client.close()
        listener.close()
        worker.join(timeout=2)


def test_read_timeout_is_recorded_once_and_stays_on_the_safe_endpoint() -> None:
    calls: List[str] = []

    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        calls.append(path)
        raise TimeoutError("The read operation timed out")

    res = clean_vps_docker_cache(
        "https://dokploy.example.com", "fake-key", transport=transport, builder=False, images=True
    )

    assert calls == ["/api/settings.cleanUnusedImages"]
    assert set(calls) <= vc.SAFE_PRUNE_PATHS
    assert res["cleanUnusedImages"] is False
    assert res["outcomes"]["cleanUnusedImages"] == "transient"
    assert res["failure_kind"]["cleanUnusedImages"] == "read_timeout"
    assert res["attempts"]["cleanUnusedImages"] == 1
    assert res["hygiene_status"] == "transient"
    assert res["errors"] == ["settings.cleanUnusedImages failed: The read operation timed out"]
    line = vc.format_hygiene_record("post-deploy", res)
    assert "hygiene_status=transient" in line
    assert "The read operation timed out" in line


def test_connect_failure_retries_the_same_endpoint_once() -> None:
    calls: List[str] = []

    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        calls.append(path)
        if len(calls) == 1:
            raise urllib.error.URLError("connection refused")
        return {"ok": True}

    res = clean_vps_docker_cache(
        "https://dokploy.example.com", "fake-key", transport=transport, builder=False, images=True
    )

    assert calls == ["/api/settings.cleanUnusedImages", "/api/settings.cleanUnusedImages"]
    assert res["cleanUnusedImages"] is True
    assert res["hygiene_status"] == "ok"
    assert res["attempts"]["cleanUnusedImages"] == 2
    assert res["errors"] == []
    assert len(res["notes"]) == 1
    assert "connection refused" in res["notes"][0]


def test_exhausted_connect_retry_stays_transient_and_does_not_escalate() -> None:
    calls: List[str] = []

    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        calls.append(path)
        raise RuntimeError("Dokploy API unreachable for POST /api/settings.cleanUnusedImages: connection refused")

    res = clean_vps_docker_cache(
        "https://dokploy.example.com", "fake-key", transport=transport, builder=False, images=True
    )

    assert calls == ["/api/settings.cleanUnusedImages", "/api/settings.cleanUnusedImages"]
    assert res["hygiene_status"] == "transient"
    assert res["failure_kind"]["cleanUnusedImages"] == "connect"
    assert res["attempts"]["cleanUnusedImages"] == 2
    assert set(calls) <= vc.SAFE_PRUNE_PATHS


def test_hard_prune_error_is_not_retried() -> None:
    calls: List[str] = []

    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        calls.append(path)
        raise RuntimeError("HTTP 401 unauthorized")

    res = clean_vps_docker_cache(
        "https://dokploy.example.com", "fake-key", transport=transport, builder=True, images=True
    )

    assert calls == ["/api/settings.cleanDockerBuilder", "/api/settings.cleanUnusedImages"]
    assert res["outcomes"]["cleanDockerBuilder"] == "failed"
    assert res["outcomes"]["cleanUnusedImages"] == "failed"
    assert res["hygiene_status"] == "failed"
    assert res["attempts"] == {"cleanDockerBuilder": 1, "cleanUnusedImages": 1}


def test_direct_prune_call_uses_the_long_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: List[float] = []

    class _Body:
        def read(self) -> bytes:
            return b"{}"

        def __enter__(self) -> "_Body":
            return self

        def __exit__(self, *args: object) -> bool:
            return False

    def fake_urlopen(request: urllib.request.Request, timeout: float = 0) -> _Body:
        seen.append(timeout)
        return _Body()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    res = clean_vps_docker_cache("https://dokploy.example", "k", timeout=30, builder=False, images=True)
    assert seen == [vc.PRUNE_TIMEOUT_SECONDS]
    assert res["hygiene_status"] == "ok"
