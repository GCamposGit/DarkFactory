"""Tests for VPS disk space monitoring, safe Docker cache cleanup, and alerting (USR-122)."""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

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
