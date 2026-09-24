"""Shared deterministic fixtures and safety rails for the official suite."""

from __future__ import annotations

import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pytest
import soundfile as sf

IMPORT_ROOT = Path(__file__).resolve().parent.parent
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

from core.harness import suite_lock as _suite_lock

_DEFAULT_SUITE_LOCK_MIN_ITEMS = 100

# Set by pytest_sessionstart (xdist controller) or pytest_collection_finish
# (plain, non-distributed run with enough items to count as "the full
# suite"). Released in pytest_sessionfinish. A raw `pytest` invocation by ANY
# harness/agent on this machine goes through this lock too -- not only the
# official `core/harness/runner.py` -- so two full suites never fight over
# CPU and sqlite state at once.
_session_suite_lock: _suite_lock.SuiteLock | None = None


def _is_xdist_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput")


def _lock_should_be_skipped(config: pytest.Config) -> bool:
    return (
        _suite_lock.is_disabled()
        or _suite_lock.is_held_by_ancestor()
        or _is_xdist_worker(config)
    )


def _requested_numprocesses(config: pytest.Config) -> int | None:
    try:
        value = config.getoption("numprocesses")
    except (ValueError, AttributeError):
        return None
    if not value or value in ("0",):
        return None
    return value


def _acquire_session_lock() -> None:
    global _session_suite_lock
    if _session_suite_lock is not None:
        return
    lock = _suite_lock.SuiteLock()
    lock.acquire()
    os.environ["DARKFAC_SUITE_LOCK_HELD"] = "1"
    _session_suite_lock = lock


def pytest_sessionstart(session: pytest.Session) -> None:
    """xdist controller acquires the machine-wide lock before workers spawn."""

    config = session.config
    if _lock_should_be_skipped(config):
        return
    if _requested_numprocesses(config) is not None:
        _acquire_session_lock()


def pytest_collection_finish(session: pytest.Session) -> None:
    """Non-distributed run: only queue once collection proves this is a big run."""

    config = session.config
    if _lock_should_be_skipped(config):
        return
    if _session_suite_lock is not None:
        return  # already acquired at sessionstart (xdist controller path)
    if _requested_numprocesses(config) is not None:
        return  # xdist requested but sessionstart already handled it
    min_items = int(os.environ.get("DARKFAC_SUITE_LOCK_MIN_ITEMS", str(_DEFAULT_SUITE_LOCK_MIN_ITEMS)))
    if len(session.items) >= min_items:
        _acquire_session_lock()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _session_suite_lock
    if _session_suite_lock is not None:
        _session_suite_lock.release()
        _session_suite_lock = None


_SECRET_ENVIRONMENT_KEYS = (
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "XAI_API_KEY",
    "XAI_MANAGEMENT_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "SILICONFLOW_API_KEY",
    "DASHSCOPE_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPU_API_KEY",
    "MINIMAX_API_KEY",
    "ARTIFICIAL_ANALYSIS_API_KEY",
)


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("darkfac-audio")
    group.addoption(
        "--run-live-audio",
        action="store_true",
        help="Enable tests marked live (manual audio/provider experiment).",
    )
    group.addoption(
        "--run-gpu-audio",
        action="store_true",
        help="Enable tests marked gpu (manual CUDA experiment).",
    )
    group.addoption(
        "--allow-network",
        action="store_true",
        help="Allow network access for explicitly opted-in live experiments.",
    )


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    run_live = config.getoption("--run-live-audio") or _env_flag(
        "DARKFAC_RUN_LIVE_AUDIO"
    )
    run_gpu = config.getoption("--run-gpu-audio") or _env_flag(
        "DARKFAC_RUN_GPU_AUDIO"
    )
    skip_live = pytest.mark.skip(
        reason="ensaio live desabilitado; use --run-live-audio explicitamente"
    )
    skip_gpu = pytest.mark.skip(
        reason="ensaio GPU desabilitado; use --run-gpu-audio explicitamente"
    )

    for item in items:
        if "live" in item.keywords and not run_live:
            item.add_marker(skip_live)
        if "gpu" in item.keywords and not run_gpu:
            item.add_marker(skip_gpu)


@pytest.fixture(scope="session", autouse=True)
def offline_test_environment(request: pytest.FixtureRequest) -> Iterator[None]:
    """Remove credentials and block network for every default test run."""

    previous_values = {key: os.environ.get(key) for key in _SECRET_ENVIRONMENT_KEYS}
    offline_values = {
        "DARKFAC_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
    previous_offline_values = {
        key: os.environ.get(key) for key in offline_values
    }

    for key in _SECRET_ENVIRONMENT_KEYS:
        os.environ.pop(key, None)
    os.environ.update(offline_values)

    allow_network = request.config.getoption("--allow-network") or _env_flag(
        "DARKFAC_ALLOW_NETWORK"
    )
    original_urlopen = urllib.request.urlopen
    original_connect = socket.socket.connect

    def block_urlopen(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.URLError("DarkFac official suite is offline")

    def block_connect(sock: socket.socket, address: Any) -> Any:
        """Block external TCP while preserving local test transports."""

        unix_family = getattr(socket, "AF_UNIX", None)
        if unix_family is not None and sock.family == unix_family:
            return original_connect(sock, address)
        if isinstance(address, tuple) and address:
            host = str(address[0]).strip("[]").lower()
            if host in {"localhost", "127.0.0.1", "::1"}:
                return original_connect(sock, address)
        raise OSError("DarkFac official suite is offline")

    if not allow_network:
        urllib.request.urlopen = block_urlopen  # type: ignore[assignment]
        socket.socket.connect = block_connect  # type: ignore[method-assign]

    try:
        yield
    finally:
        urllib.request.urlopen = original_urlopen  # type: ignore[assignment]
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        for key, value in previous_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for key, value in previous_offline_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def stereo_wav(tmp_path: Path) -> Path:
    """Generate a tiny deterministic stereo fixture without shipping binary data."""

    sample_rate = 8_000
    duration_sec = 1.0
    sample_count = int(sample_rate * duration_sec)
    timeline = np.arange(sample_count, dtype=np.float32) / sample_rate
    data = np.column_stack(
        (
            0.20 * np.sin(2 * np.pi * 220 * timeline),
            0.05 * np.sin(2 * np.pi * 440 * timeline),
        )
    ).astype(np.float32)
    path = tmp_path / "meeting_stereo.wav"
    sf.write(path, data, sample_rate, subtype="PCM_16")
    return path
