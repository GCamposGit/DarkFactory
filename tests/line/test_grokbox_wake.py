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


def test_preflight_check_down_suppresses_email_without_explicit_authorization(caplog) -> None:
    caplog.set_level("INFO", logger="darkfac.line.grokbox_wake")
    calls: list = []
    down = grokbox_wake.preflight_check(probe=lambda: False, send_email=_email_stub(calls))

    assert down is True
    assert calls == []
    assert (
        "provider=grokbox_wake_email outcome=suppressed "
        "cause=external_communication_not_authorized"
    ) in caplog.text
    state = grokbox_wake._load_state()
    assert state["down"] is True
    assert "last_email_sent_at" not in state


def test_preflight_check_down_sends_wake_email_when_explicitly_authorized() -> None:
    calls: list = []
    down = grokbox_wake.preflight_check(
        probe=lambda: False,
        send_email=_email_stub(calls),
        allow_external_communication=True,
    )

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

    grokbox_wake.preflight_check(
        probe=lambda: False, send_email=_email_stub(calls), now=t0, allow_external_communication=True
    )
    assert len(calls) == 1

    # 10 minutes later: still throttled, no second email.
    grokbox_wake.preflight_check(
        probe=lambda: False, send_email=_email_stub(calls), now=t0 + timedelta(minutes=10),
        allow_external_communication=True,
    )
    assert len(calls) == 1

    # 31 minutes after the first email: throttle window elapsed, a second email goes out.
    grokbox_wake.preflight_check(
        probe=lambda: False, send_email=_email_stub(calls), now=t0 + timedelta(minutes=31),
        allow_external_communication=True,
    )
    assert len(calls) == 2


def test_preflight_check_probe_exception_is_treated_as_down_and_logged_only() -> None:
    def _raising_probe() -> bool:
        raise RuntimeError("DERP relay timeout")

    calls: list = []
    down = grokbox_wake.preflight_check(
        probe=_raising_probe, send_email=_email_stub(calls), allow_external_communication=True
    )

    assert down is True
    assert len(calls) == 1


def test_preflight_check_email_failure_never_raises() -> None:
    def _raising_email(subject: str, text: str) -> dict:
        raise RuntimeError("Cloudflare Worker unreachable")

    down = grokbox_wake.preflight_check(
        probe=lambda: False, send_email=_raising_email, allow_external_communication=True
    )

    assert down is True  # the probe result stands even though the email failed


def test_preflight_check_email_ok_false_never_raises() -> None:
    calls: list = []
    down = grokbox_wake.preflight_check(
        probe=lambda: False, send_email=_email_stub(calls, ok=False), allow_external_communication=True
    )

    assert down is True
    assert len(calls) == 1
    # Reserve before sending because a timeout/negative response can follow delivery.
    state = grokbox_wake._load_state()
    assert "last_email_sent_at" in state


def test_state_root_defaults_to_common_checkout_for_linked_worktrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DARKFAC_STATE_ROOT", raising=False)
    common_dir = tmp_path / "main" / ".git"
    monkeypatch.setattr(
        grokbox_wake.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"stdout": str(common_dir)})(),
    )

    assert grokbox_wake._shared_state_root() == tmp_path / "main" / ".factory"


def test_explicit_state_root_remains_respected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    explicit = tmp_path / "custom-state"
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(explicit))

    assert grokbox_wake._shared_state_root() == explicit.resolve()


def test_unreadable_state_suppresses_authorized_email(caplog, tmp_path: Path) -> None:
    caplog.set_level("INFO", logger="darkfac.line.grokbox_wake")
    path = grokbox_wake._state_file()
    path.write_text("{broken", encoding="utf-8")
    calls: list = []

    assert grokbox_wake.preflight_check(
        probe=lambda: False,
        send_email=_email_stub(calls),
        allow_external_communication=True,
    ) is True

    assert calls == []
    assert "provider=grokbox_wake_email outcome=suppressed cause=state_unreadable" in caplog.text


def test_state_write_failure_suppresses_authorized_email(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    caplog.set_level("INFO", logger="darkfac.line.grokbox_wake")
    calls: list = []
    monkeypatch.setattr(
        grokbox_wake, "_save_state",
        lambda _state: (_ for _ in ()).throw(grokbox_wake._ThrottleStateError("state_write_failed")),
    )

    assert grokbox_wake.preflight_check(
        probe=lambda: False,
        send_email=_email_stub(calls),
        allow_external_communication=True,
    ) is True

    assert calls == []
    assert "provider=grokbox_wake_email outcome=suppressed cause=state_write_failed" in caplog.text


def test_unavailable_lock_suppresses_authorized_email(caplog) -> None:
    caplog.set_level("INFO", logger="darkfac.line.grokbox_wake")
    path = grokbox_wake._state_file()
    path.with_suffix(".lock").mkdir()
    calls: list = []

    assert grokbox_wake.preflight_check(
        probe=lambda: False,
        send_email=_email_stub(calls),
        allow_external_communication=True,
    ) is True

    assert calls == []
    assert "provider=grokbox_wake_email outcome=suppressed cause=lock_unavailable" in caplog.text


def test_concurrent_processes_share_throttle_and_send_at_most_once(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    state_root = tmp_path / "shared-state"
    sent_file = tmp_path / "sent.txt"
    script = f'''\
import time
from pathlib import Path
from core.line import grokbox_wake as wake
def send(_subject, _text):
    with Path({str(sent_file)!r}).open("a", encoding="utf-8") as stream:
        stream.write("sent\\n")
    time.sleep(0.3)
    return {{"ok": True}}
wake.preflight_check(probe=lambda: False, send_email=send, allow_external_communication=True)
'''
    env = os.environ.copy()
    env["DARKFAC_STATE_ROOT"] = str(state_root)
    repo = Path(__file__).resolve().parents[2]
    processes = [
        subprocess.Popen([sys.executable, "-c", script], cwd=repo, env=env)
        for _ in range(2)
    ]
    assert [process.wait(timeout=10) for process in processes] == [0, 0]

    assert sent_file.read_text(encoding="utf-8").splitlines() == ["sent"]


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
    grokbox_wake.preflight_check(
        probe=lambda: False, send_email=lambda *_: {"ok": True}, allow_external_communication=True
    )

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
    grokbox_wake.preflight_check(
        probe=lambda: False, send_email=lambda *_: {"ok": True}, allow_external_communication=True
    )

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
    grokbox_wake.preflight_check(
        probe=lambda: False, send_email=lambda *_: {"ok": True}, allow_external_communication=True
    )

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
