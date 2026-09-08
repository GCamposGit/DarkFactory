"""Shared deterministic fixtures and safety rails for the official suite."""

from __future__ import annotations

import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pytest
import soundfile as sf


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
