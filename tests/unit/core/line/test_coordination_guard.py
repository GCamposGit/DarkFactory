"""USR-483: exclusive coordination actions follow the lease; session ids survive the CLI process."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.line.coordination import (
    ABSENCE_HEARTBEAT_EXPIRED,
    ABSENCE_NO_COORDINATOR,
    LEASE_TTL_SECONDS,
    CoordinationRefused,
    assume,
    authorize,
    consult,
    open_state,
    publish_handoff,
    require_coordinator,
)
from core.line.session_identity import build_session_identity
from core.line.shared_state import SharedState


NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


def _identity(session_id: str, harness: str, host: str = "notebook"):
    return build_session_identity(
        {"DARKFAC_SESSION_ID": session_id},
        harness_override=harness,
        host=host,
        started_at=NOW,
    )


def _state(tmp_path: Path) -> SharedState:
    return open_state(str(tmp_path / "shared.sqlite3"))


def test_only_the_coordinator_passes_the_exclusive_guard(tmp_path: Path) -> None:
    state = _state(tmp_path)
    owner = _identity("sess-owner", "claude")
    parallel = _identity("sess-parallel", "codex", "desktop")
    assume(state, owner, owner_decision=True, now=NOW)

    assert authorize(state, owner, "operar_despachante", now=NOW + timedelta(seconds=30))["coordinator"]
    with pytest.raises(CoordinationRefused) as refused:
        authorize(state, parallel, "operar_despachante", now=NOW + timedelta(seconds=40))
    assert refused.value.coordinator["session_id"] == "sess-owner"
    assert "DARKFAC_OPERATING_HARNESS" not in str(refused.value)
    # the refused session still works as a normal parallel session
    assert consult(state, parallel, now=NOW + timedelta(seconds=50))["caller"]["is_coordinator"] is False


def test_without_coordinator_the_guard_refuses_with_the_cause(tmp_path: Path) -> None:
    state = _state(tmp_path)
    session = _identity("sess-a", "grok")
    with pytest.raises(CoordinationRefused) as refused:
        authorize(state, session, "politica_da_frota", now=NOW)
    assert refused.value.coordinator is None
    assert refused.value.absence_reason == ABSENCE_NO_COORDINATOR

    assume(state, session, owner_decision=True, now=NOW)
    later = NOW + timedelta(seconds=LEASE_TTL_SECONDS + 1)
    with pytest.raises(CoordinationRefused) as expired:
        authorize(state, session, "politica_da_frota", now=later)
    assert expired.value.absence_reason == ABSENCE_HEARTBEAT_EXPIRED


def test_expiry_is_persisted_once_with_an_audit_event(tmp_path: Path) -> None:
    state = _state(tmp_path)
    owner = _identity("sess-owner", "claude")
    other = _identity("sess-other", "codex")
    assume(state, owner, owner_decision=True, now=NOW)
    later = NOW + timedelta(seconds=LEASE_TTL_SECONDS + 5)
    consult(state, other, now=later)
    consult(state, other, now=later + timedelta(seconds=10))
    with state.connection() as connection:
        events = connection.execute(
            "SELECT cause FROM coordination_events WHERE event_type = 'lease_expired'"
        ).fetchall()
        status = connection.execute("SELECT absence_reason FROM coordination_status").fetchone()
    assert [row["cause"] for row in events] == [ABSENCE_HEARTBEAT_EXPIRED]
    assert status["absence_reason"] == ABSENCE_HEARTBEAT_EXPIRED


def test_replaced_session_cannot_revive_its_lease_by_consulting(tmp_path: Path) -> None:
    state = _state(tmp_path)
    first = _identity("sess-a", "claude")
    second = _identity("sess-b", "codex", "desktop")
    assume(state, first, owner_decision=True, now=NOW)
    taken = assume(state, second, owner_decision=True, now=NOW + timedelta(seconds=10))

    after = consult(state, first, now=NOW + timedelta(seconds=200))

    assert after["coordinator"]["session_id"] == "sess-b"
    assert after["coordinator"]["expires_at"] == taken["coordinator"]["expires_at"]
    assert after["caller"]["is_coordinator"] is False


def test_handoff_is_published_only_by_the_coordinator(tmp_path: Path) -> None:
    state = _state(tmp_path)
    owner = _identity("sess-owner", "claude")
    other = _identity("sess-other", "codex")
    assume(state, owner, owner_decision=True, now=NOW)
    target = tmp_path / "HANDOFF.md"

    with pytest.raises(CoordinationRefused):
        publish_handoff(target, "x", state=state, identity=other, now=NOW)
    assert not target.exists()
    publish_handoff(target, "ok", state=state, identity=owner, now=NOW)
    assert target.read_text(encoding="utf-8") == "ok"


def test_process_without_a_session_signal_is_not_subject_to_the_lease(tmp_path: Path) -> None:
    service = build_session_identity({}, host="notebook", started_at=NOW)
    assert service.harness is None
    require_coordinator("operar_despachante", state=_state(tmp_path), identity=service, now=NOW)


def test_claude_session_keeps_one_id_across_cli_processes(tmp_path: Path) -> None:
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}
    first = build_session_identity(env, host="notebook")
    second = build_session_identity(env, host="notebook")
    other_host = build_session_identity(env, host="desktop")

    assert first.session_id == second.session_id
    assert first.session_id != other_host.session_id
    assert first.started_at == second.started_at

    state = _state(tmp_path)
    assume(state, first, owner_decision=True, now=NOW)
    renewed = consult(state, second, now=NOW + timedelta(seconds=300))
    assert renewed["caller"]["is_coordinator"] is True
    assert renewed["coordinator"]["last_heartbeat"].startswith("2026-11-24T12:05:00")


def test_idle_fallback_session_rolls_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.paths import state_root

    env = {"CLAUDECODE": "1"}
    first = build_session_identity(env, host="notebook")
    registry = state_root() / "coordination" / "session_ids.json"
    records = json.loads(registry.read_text(encoding="utf-8"))
    for record in records.values():
        record["last_seen"] -= 10_000
    registry.write_text(json.dumps(records), encoding="utf-8")

    assert build_session_identity(env, host="notebook").session_id != first.session_id


def test_default_shared_state_uses_the_state_root_not_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.line.coordination import resolve_state_path

    monkeypatch.delenv("DARKFAC_SHARED_STATE_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assert SharedState().path == Path(resolve_state_path()).resolve()
