"""USR-321: external probes have a hard deadline, a structured error and stay fail-closed."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from core.line import routing
from core.usage import probe_deadline
from core.usage.probe_deadline import ProbeTimeout, probe_deadline_s, run_with_deadline


def test_deadline_env_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(probe_deadline.ENV_PROBE_DEADLINE, raising=False)
    assert probe_deadline_s() == 10.0
    monkeypatch.setenv(probe_deadline.ENV_PROBE_DEADLINE, "2.5")
    assert probe_deadline_s() == 2.5
    monkeypatch.setenv(probe_deadline.ENV_PROBE_DEADLINE, "garbage")
    assert probe_deadline_s() == 10.0


def test_slow_probe_raises_structured_timeout() -> None:
    started = time.monotonic()
    with pytest.raises(ProbeTimeout) as info:
        run_with_deadline("anthropic", lambda: time.sleep(5), deadline_s=0.2)
    assert time.monotonic() - started < 2.0
    payload = info.value.as_dict()
    assert payload["error"] == "probe_timeout"
    assert payload["provider"] == "anthropic"
    assert payload["deadline_s"] == 0.2
    # Windows timer/join granularity can report slightly under the deadline (0.187 s seen in CI).
    assert payload["duration_s"] >= 0.1


def test_fast_probe_returns_and_errors_propagate() -> None:
    assert run_with_deadline("x", lambda: 7, deadline_s=1.0) == 7
    with pytest.raises(ValueError):
        run_with_deadline("x", lambda: (_ for _ in ()).throw(ValueError("boom")), deadline_s=1.0)


def test_timeout_never_kills_children_of_other_threads() -> None:
    # A process started concurrently by unrelated work must survive a probe timeout.
    holder: dict[str, subprocess.Popen[bytes]] = {}
    started = threading.Event()

    def bystander() -> None:
        holder["p"] = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        started.set()

    def probe() -> None:
        threading.Thread(target=bystander, daemon=True).start()
        time.sleep(5)

    try:
        with pytest.raises(ProbeTimeout):
            run_with_deadline("codex", probe, deadline_s=1.0)
        assert started.wait(5)
        assert holder["p"].poll() is None
    finally:
        if "p" in holder:
            holder["p"].kill()
            holder["p"].wait(timeout=10)


def test_routing_fails_closed_with_probe_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from core.usage import adapters

    class SlowAdapter:
        class spec:  # noqa: N801
            provider_id = "anthropic"

        def inspect(self, force: bool = False):  # noqa: ANN201
            time.sleep(5)

    monkeypatch.setattr(adapters, "build_default_adapters", lambda _dir: [SlowAdapter()])
    monkeypatch.setenv(probe_deadline.ENV_PROBE_DEADLINE, "0.2")
    headroom, reason = routing._quota_headroom_diagnosis("anthropic")
    assert headroom is None
    assert reason == "probe_timeout"
    assert routing._default_quota_headroom("anthropic") is None
