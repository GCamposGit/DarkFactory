"""Tests for the grokbox pre-flight wake-up probe (USR-310)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.line import grokbox_wake


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))


def _email_stub(calls: list, *, ok: bool = True):
    def _send(subject: str, text: str) -> dict:
        calls.append((subject, text))
        return {"ok": ok} if ok else {"ok": False, "error": "boom"}

    return _send


# --- preflight_check ---------------------------------------------------------


def test_preflight_check_healthy_sends_no_email() -> None:
    calls: list = []
    down = grokbox_wake.preflight_check(probe=lambda: True, send_email=_email_stub(calls))

    assert down is False
    assert calls == []
    state = grokbox_wake._load_state()
    assert state["down"] is False


def test_preflight_check_down_sends_wake_email() -> None:
    calls: list = []
    down = grokbox_wake.preflight_check(probe=lambda: False, send_email=_email_stub(calls))

    assert down is True
    assert len(calls) == 1
    subject, text = calls[0]
    assert "grokbox" in subject.lower()
    assert "100.77.130.5" in text
    state = grokbox_wake._load_state()
    assert state["down"] is True
    assert "last_email_sent_at" in state


def test_preflight_check_throttles_repeated_email_within_30_min() -> None:
    calls: list = []
    t0 = datetime(2026, 10, 18, 12, 0, 0, tzinfo=UTC)

    grokbox_wake.preflight_check(probe=lambda: False, send_email=_email_stub(calls), now=t0)
    assert len(calls) == 1

    # 10 minutes later: still throttled, no second email.
    grokbox_wake.preflight_check(
        probe=lambda: False, send_email=_email_stub(calls), now=t0 + timedelta(minutes=10)
    )
    assert len(calls) == 1

    # 31 minutes after the first email: throttle window elapsed, a second email goes out.
    grokbox_wake.preflight_check(
        probe=lambda: False, send_email=_email_stub(calls), now=t0 + timedelta(minutes=31)
    )
    assert len(calls) == 2


def test_preflight_check_probe_exception_is_treated_as_down_and_logged_only() -> None:
    def _raising_probe() -> bool:
        raise RuntimeError("DERP relay timeout")

    calls: list = []
    down = grokbox_wake.preflight_check(probe=_raising_probe, send_email=_email_stub(calls))

    assert down is True
    assert len(calls) == 1


def test_preflight_check_email_failure_never_raises() -> None:
    def _raising_email(subject: str, text: str) -> dict:
        raise RuntimeError("Cloudflare Worker unreachable")

    down = grokbox_wake.preflight_check(probe=lambda: False, send_email=_raising_email)

    assert down is True  # the probe result stands even though the email failed


def test_preflight_check_email_ok_false_never_raises() -> None:
    calls: list = []
    down = grokbox_wake.preflight_check(probe=lambda: False, send_email=_email_stub(calls, ok=False))

    assert down is True
    assert len(calls) == 1
    # A failed send must not be recorded as successfully throttling future attempts.
    state = grokbox_wake._load_state()
    assert "last_email_sent_at" not in state


# --- wait_if_down -------------------------------------------------------------


def test_wait_if_down_noop_when_grokbox_was_up() -> None:
    grokbox_wake.preflight_check(probe=lambda: True, send_email=lambda *_: {"ok": True})

    probe_calls: list = []
    sleep_calls: list = []

    def _probe() -> bool:
        probe_calls.append(1)
        return True

    woke = grokbox_wake.wait_if_down(probe=_probe, sleep=lambda s: sleep_calls.append(s))

    assert woke is False
    assert probe_calls == []
    assert sleep_calls == []


def test_wait_if_down_polls_until_healthy() -> None:
    grokbox_wake.preflight_check(probe=lambda: False, send_email=lambda *_: {"ok": True})

    results = iter([False, False, True])
    sleep_calls: list = []

    woke = grokbox_wake.wait_if_down(
        probe=lambda: next(results),
        sleep=lambda s: sleep_calls.append(s),
        limit_s=60.0,
        poll_s=10.0,
    )

    assert woke is True
    assert sleep_calls == [10.0, 10.0]
    state = grokbox_wake._load_state()
    assert state["down"] is False


def test_wait_if_down_gives_up_after_limit() -> None:
    grokbox_wake.preflight_check(probe=lambda: False, send_email=lambda *_: {"ok": True})

    sleep_calls: list = []
    woke = grokbox_wake.wait_if_down(
        probe=lambda: False,
        sleep=lambda s: sleep_calls.append(s),
        limit_s=25.0,
        poll_s=10.0,
    )

    assert woke is False
    assert sum(sleep_calls) <= 25.0
    state = grokbox_wake._load_state()
    assert state["down"] is False  # the window was consumed either way


def test_wait_if_down_probe_exception_does_not_crash() -> None:
    grokbox_wake.preflight_check(probe=lambda: False, send_email=lambda *_: {"ok": True})

    def _raising_probe() -> bool:
        raise RuntimeError("DERP relay timeout")

    woke = grokbox_wake.wait_if_down(
        probe=_raising_probe,
        sleep=lambda _s: None,
        limit_s=15.0,
        poll_s=10.0,
    )

    assert woke is False


def test_state_dir_is_ignored_by_git_so_the_shared_checkout_stays_clean() -> None:
    """The wake state lives in the shared checkout's .factory; if git saw it, the next
    run_ticket would refuse with "checkout compartilhado sujo"."""
    import subprocess

    repo = Path(__file__).resolve().parents[2]
    rel = f".factory/{grokbox_wake.STATE_SUBDIR}/{grokbox_wake.STATE_FILE_NAME}"
    result = subprocess.run(
        ["git", "check-ignore", "-q", rel], cwd=repo, capture_output=True, check=False
    )
    assert result.returncode == 0, f"{rel} must be ignored by .gitignore"
