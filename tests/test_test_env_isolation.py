"""Guard that remote worker settings from the host do not reach tests."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import AMBIENT_REMOTE_INFRA_ENV

ROOT = Path(__file__).resolve().parent.parent


def test_remote_worker_environment_list_covers_documented_settings() -> None:
    assert {
        "DARKFAC_TEST_WORKERS",
        "DARKFAC_WORKER_HEALTH_TIMEOUT_SEC",
        "DARKFAC_REMOTE_HARNESS",
        "DARKFAC_HARNESS_WORKER_JOB",
        "REMOTE_HARNESS_URL",
        "REMOTE_HARNESS_URLS",
    }.issubset(AMBIENT_REMOTE_INFRA_ENV)


def test_child_pytest_does_not_inherit_polluted_remote_worker_environment(
    tmp_path: Path,
) -> None:
    probe = tmp_path / "test_probe_env.py"
    probe.write_text(
        "import os\n"
        f"NAMES = {list(AMBIENT_REMOTE_INFRA_ENV)!r}\n"
        "def test_remote_settings_are_cleared():\n"
        "    assert [n for n in NAMES if n in os.environ] == ['DARKFAC_REMOTE_HARNESS']\n"
        "    assert os.environ['DARKFAC_REMOTE_HARNESS'] == 'off'\n"
        "def test_tests_can_set_remote_settings(monkeypatch):\n"
        "    monkeypatch.setenv('DARKFAC_TEST_WORKERS', 'http://127.0.0.1:1')\n"
        "    assert os.environ['DARKFAC_TEST_WORKERS'] == 'http://127.0.0.1:1'\n",
        encoding="utf-8",
    )
    polluted_env = {
        **os.environ,
        **{name: "http://100.64.0.1:8080" for name in AMBIENT_REMOTE_INFRA_ENV},
        "DARKFAC_SUITE_LOCK": "0",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n",
            "0",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.conftest",
            "--rootdir",
            str(tmp_path),
            str(probe),
        ],
        cwd=ROOT,
        env=polluted_env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_ordinary_test_runs_with_remote_harness_forced_off() -> None:
    assert os.environ.get("DARKFAC_REMOTE_HARNESS") == "off"
    assert "DARKFAC_TEST_WORKERS" not in os.environ


def test_maybe_dispatch_remote_without_mocks_never_touches_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import urllib.request

    from core.harness import remote_dispatch

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted from an unmocked test")

    monkeypatch.setattr(urllib.request, "urlopen", _forbidden)
    monkeypatch.setattr(socket, "create_connection", _forbidden)
    result = remote_dispatch.maybe_dispatch_remote(
        steps=[],
        config_hash="x",
        quick=True,
        include_holdout=False,
        config_path=tmp_path / "harness.config.json",
        project_root=tmp_path,
        cache_lookup_enabled=False,
        local_only=False,
        remote_required=False,
        emit_cache_hit=lambda *a, **k: False,
        notify_hub_on_pass=lambda: None,
    )
    assert result is None
