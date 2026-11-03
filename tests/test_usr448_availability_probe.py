"""USR-448: restore core.infra.availability_probe with a no-alert --check mode."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, List

import pytest

from core.infra import availability_probe as mod

ROOT = Path(__file__).resolve().parents[1]


def _sender(calls: List[str]) -> Callable[[str], bool]:
    def send(message: str) -> bool:
        calls.append(message)
        return True

    return send


# --------------------------------------------------------------------------- tick


def test_tick_alerts_once_after_120s_continuous_failure_then_sends_recovery() -> None:
    calls: List[str] = []
    state = mod.ProbeState()
    clock = {"t": 0.0}

    def now() -> float:
        return clock["t"]

    # First failure observed at t=0: not yet alerted (0s down).
    state = mod.tick(state, probe=lambda: False, send=_sender(calls), now=now)
    assert calls == []
    assert state.alerted is False

    # Still failing at t=119s: still under the 120s threshold.
    clock["t"] = 119.0
    state = mod.tick(state, probe=lambda: False, send=_sender(calls), now=now)
    assert calls == []

    # At t=121s the alert fires exactly once.
    clock["t"] = 121.0
    state = mod.tick(state, probe=lambda: False, send=_sender(calls), now=now)
    assert len(calls) == 1
    assert state.alerted is True

    # Repeated failure observations never re-alert.
    clock["t"] = 200.0
    state = mod.tick(state, probe=lambda: False, send=_sender(calls), now=now)
    assert len(calls) == 1

    # Recovery sends exactly one message and clears the state.
    clock["t"] = 210.0
    state = mod.tick(state, probe=lambda: True, send=_sender(calls), now=now)
    assert len(calls) == 2
    assert state.alerted is False
    assert state.first_failure_ts is None


def test_tick_recovery_without_prior_alert_sends_nothing() -> None:
    calls: List[str] = []
    state = mod.ProbeState()
    state = mod.tick(state, probe=lambda: True, send=_sender(calls), now=lambda: 0.0)
    assert calls == []
    assert state.alerted is False


def test_tick_sender_failure_never_raises_and_does_not_mark_alerted() -> None:
    def boom(_message: str) -> bool:
        raise RuntimeError("telegram down")

    state = mod.ProbeState(first_failure_ts=0.0)
    state = mod.tick(state, probe=lambda: False, send=boom, now=lambda: 300.0)
    assert state.alerted is False


# --------------------------------------------------------------------------- http_probe


def test_http_probe_never_raises_on_network_failure() -> None:
    assert mod.http_probe("http://127.0.0.1:1/unreachable", timeout=0.2) is False


# --------------------------------------------------------------------------- state io


def test_load_state_missing_file_returns_default(tmp_path: Path) -> None:
    state = mod.load_state(tmp_path / "missing.json")
    assert state == mod.ProbeState()


def test_save_then_load_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "hub_availability.json"
    mod.save_state(path, mod.ProbeState(first_failure_ts=42.0, alerted=True))
    loaded = mod.load_state(path)
    assert loaded == mod.ProbeState(first_failure_ts=42.0, alerted=True)


# --------------------------------------------------------------------------- main / --check


def test_main_check_mode_prints_status_skips_sender_and_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(mod, "http_probe", lambda url=mod.DEFAULT_URL, timeout=5.0: True)

    def fail_sender(_message: str) -> bool:
        raise AssertionError("default_sender must not be called in --check mode")

    monkeypatch.setattr(mod, "default_sender", fail_sender)

    state_path = tmp_path / "hub_availability.json"
    rc = mod.main(argv=["--check"], path=state_path)

    assert rc == 0
    assert not state_path.exists()
    out = capsys.readouterr().out
    assert "OK" in out
    assert mod.DEFAULT_URL in out


def test_main_check_mode_reports_down_without_raising(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr(mod, "http_probe", lambda url=mod.DEFAULT_URL, timeout=5.0: False)

    rc = mod.main(argv=["--check"])

    assert rc == 0
    assert "DOWN" in capsys.readouterr().out


def test_main_default_mode_persists_clean_state_when_health_is_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "http_probe", lambda url=mod.DEFAULT_URL, timeout=5.0: True)

    def fail_sender(_message: str) -> bool:
        raise AssertionError("default_sender must not be called when nothing was ever alerted")

    monkeypatch.setattr(mod, "default_sender", fail_sender)

    state_path = tmp_path / "hub_availability.json"
    rc = mod.main(argv=[], path=state_path)

    assert rc == 0
    assert state_path.exists()
    assert mod.load_state(state_path) == mod.ProbeState()


def test_main_uses_state_root_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(mod, "http_probe", lambda url=mod.DEFAULT_URL, timeout=5.0: True)

    rc = mod.main(argv=[])

    assert rc == 0
    assert (tmp_path / mod.STATE_FILENAME).exists()


def test_module_is_importable_as_a_runnable_script() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "core.infra.availability_probe", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "/health" in result.stdout
