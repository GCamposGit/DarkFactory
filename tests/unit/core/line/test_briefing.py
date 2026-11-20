"""USR-483 T4: one briefing command consults shared state and assumes on an owner decision."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.line import briefing
from core.line.briefing import SectionReading, collect_briefing, render_json, render_text
from core.line.coordination import assume, consult, open_state
from core.line.session_identity import build_session_identity, detect_session_harness
from core.line.shared_state import SharedState


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "tests" / "fixtures" / "harness_sessions"


def _identity(env: dict[str, str] | None = None, *, session_id: str, host: str, harness: str | None = None):
    current = dict(env or {})
    current["DARKFAC_SESSION_ID"] = session_id
    current["DARKFAC_SESSION_HOST"] = host
    return build_session_identity(
        current,
        harness_override=harness,
        host=host,
        started_at=NOW,
    )


def _state(tmp_path: Path) -> SharedState:
    return open_state(str(tmp_path / "shared.sqlite3"))


def _section(source: str, items: list[dict[str, object]] | None = None, *, freshness: str = "current") -> SectionReading:
    return SectionReading(
        source=source,
        observed_at="2026-11-11T12:00:00Z",
        source_time="2026-11-11T11:59:00Z",
        age_seconds=60,
        freshness=freshness,
        cause=None,
        items=list(items or []),
    )


def _loaders() -> dict[str, object]:
    return {
        "queue_loader": lambda _now: _section(
            "control_store",
            [{"id": "USR-1", "status": "planned", "title": "Fila compartilhada", "updated_at": "2026-11-11T11:50:00Z"}],
        ),
        "quota_loader": lambda _now: _section(
            "synced_usage_provider",
            [{
                "harness": "codex",
                "provider_id": "openai",
                "remaining_percent": 40.0,
                "checked_at": "2026-11-11T11:59:00Z",
                "age_seconds": 60,
                "freshness": "current",
                "confirmed": True,
                "cause": None,
            }],
        ),
        "runs_loader": lambda _now: _section(
            "control_store_line_live",
            [{
                "run_id": "run-1",
                "ticket_id": "USR-9",
                "title": "Ao vivo",
                "state": "running",
                "run_status": "active",
                "current_stage": "agent",
                "host_id": "desktop",
                "updated_at": "2026-11-11T11:59:30Z",
                "age_seconds": 30,
                "freshness": "current",
            }],
        ),
        "pull_request_loader": lambda _now: _section(
            "github",
            [{
                "number": 7,
                "title": "PR compartilhado",
                "url": "https://example.test/pr/7",
                "updated_at": "2026-11-11T11:40:00Z",
                "age_seconds": 1200,
                "freshness": "stale",
            }],
        ),
    }


def _lease_count(state: SharedState) -> int:
    with state.transaction() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM coordination_lease").fetchone()[0])


def _event_count(state: SharedState) -> int:
    with state.transaction() as connection:
        return int(connection.execute("SELECT COUNT(*) FROM coordination_events").fetchone()[0])


def test_confirmed_session_fixtures_beat_a_persisted_operating_harness() -> None:
    for name in ("codex.json", "antigravity.json"):
        payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        detection = detect_session_harness(payload["environment"])
        assert detection.harness == payload["expected_harness"]
        assert detection.signal == payload["expected_signal"]
        assert payload["environment"]["DARKFAC_OPERATING_HARNESS"] != payload["expected_harness"]


def test_no_reliable_signal_does_not_reuse_the_persisted_variable() -> None:
    detection = detect_session_harness(
        {"DARKFAC_OPERATING_HARNESS": "codex", "PATH": "C:\\Windows"}
    )
    assert detection.harness is None
    assert detection.signal is None


def test_explicit_override_is_only_the_current_call() -> None:
    env = {"DARKFAC_OPERATING_HARNESS": "antigravity", "CODEX_THREAD_ID": "thread-1"}
    overridden = detect_session_harness(env, override="claude")
    assert overridden.harness == "claude"
    assert overridden.explicit_override is True
    assert detect_session_harness(env).harness == "codex"


def test_consult_registers_the_session_without_creating_a_coordinator(tmp_path: Path) -> None:
    state = _state(tmp_path)
    identity = _identity(session_id="sess-a", host="notebook")
    view = consult(state, identity, now=NOW)
    assert view["coordinator"] is None
    assert view["absence_reason"] == "no_coordinator"
    assert view["caller"]["is_coordinator"] is False
    assert _lease_count(state) == 0
    assert [item["session_id"] for item in view["parallel_sessions"]] == ["sess-a"]


def test_owner_decision_swaps_the_lease_immediately(tmp_path: Path) -> None:
    state = _state(tmp_path)
    first = _identity(session_id="sess-a", host="notebook", harness="claude")
    second = _identity(session_id="sess-b", host="desktop", harness="grok")
    assume(state, first, owner_decision=True, now=NOW)
    swapped = assume(state, second, owner_decision=True, now=NOW + timedelta(seconds=5))
    assert swapped["coordinator"]["session_id"] == "sess-b"
    assert swapped["coordinator"]["harness"] == "grok"
    assert swapped["coordinator"]["host"] == "desktop"
    assert swapped["coordinator"]["origin"] == "owner_decision"
    assert swapped["assumed"] is True
    assert _lease_count(state) == 1
    assert _event_count(state) == 2
    with state.transaction() as connection:
        causes = [row[0] for row in connection.execute("SELECT cause FROM coordination_events ORDER BY event_id")]
    assert causes == ["owner_decision", "owner_decision"]


def test_persisted_variable_does_not_decide_the_assumed_harness(tmp_path: Path) -> None:
    payload = json.loads((FIXTURES / "codex.json").read_text(encoding="utf-8"))
    identity = build_session_identity(
        {**payload["environment"], "DARKFAC_SESSION_ID": "sess-codex"},
        host="notebook",
        started_at=NOW,
    )
    view = assume(_state(tmp_path), identity, owner_decision=True, now=NOW)
    assert identity.harness == "codex"
    assert view["coordinator"]["harness"] == "codex"
    assert view["coordinator"]["model"] == "codex-test-model"


def test_expired_coordinator_is_reported_and_not_renewed_by_consult(tmp_path: Path) -> None:
    state = _state(tmp_path)
    expired_at = NOW.timestamp() - 10
    with state.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO coordination_lease (
                singleton_id, lease_id, session_id, harness, model, host,
                decided_at, origin, last_heartbeat, expires_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "lease-old",
                "sess-old",
                "antigravity",
                "antigravity-test-model",
                "pc-old",
                expired_at - 900,
                "owner_decision",
                expired_at - 30,
                expired_at,
            ),
        )
    holder = _identity(session_id="sess-old", host="pc-old", harness="antigravity")
    view = consult(state, holder, now=NOW)
    assert view["coordinator"] is None
    assert view["absence_reason"] == "heartbeat_expired"
    assert view["expired_coordinator"]["session_id"] == "sess-old"
    assert view["expired_coordinator"]["freshness"] == "stale"
    assert view["caller"]["is_coordinator"] is False
    with state.transaction() as connection:
        expires = connection.execute("SELECT expires_at FROM coordination_lease").fetchone()[0]
    assert float(expires) == pytest.approx(expired_at)


def test_unknown_harness_can_consult_and_assumes_only_with_an_owner_decision(tmp_path: Path) -> None:
    state = _state(tmp_path)
    identity = _identity({"PATH": "C:\\Windows"}, session_id="sess-unknown", host="desktop")
    assert identity.harness is None
    consulted = consult(state, identity, now=NOW)
    assert consulted["coordinator"] is None
    assert consulted["parallel_sessions"][0]["harness"] == "unknown"
    with pytest.raises(briefing.AssumptionRefused):
        assume(state, identity, owner_decision=False, now=NOW)
    assert _lease_count(state) == 0
    assert _event_count(state) == 0
    taken = assume(state, identity, owner_decision=True, now=NOW)
    assert taken["coordinator"]["harness"] == "unknown"
    assert taken["coordinator"]["session_id"] == "sess-unknown"


def test_parallel_sessions_stay_visible_without_taking_coordination(tmp_path: Path) -> None:
    state = _state(tmp_path)
    owner = _identity(session_id="sess-owner", host="notebook", harness="claude")
    other = _identity(session_id="sess-other", host="desktop", harness="codex")
    assume(state, owner, owner_decision=True, now=NOW)
    viewed = consult(state, other, now=NOW + timedelta(seconds=20))
    assert viewed["coordinator"]["session_id"] == "sess-owner"
    assert viewed["caller"]["is_coordinator"] is False
    assert {item["session_id"] for item in viewed["parallel_sessions"]} == {"sess-other"}
    assert {item["host"] for item in viewed["sessions"]} == {"notebook", "desktop"}
    with pytest.raises(briefing.AssumptionRefused):
        assume(state, other, owner_decision=False, now=NOW + timedelta(seconds=30))
    assert consult(state, other, now=NOW + timedelta(seconds=40))["coordinator"]["session_id"] == "sess-owner"


def test_holder_consult_refreshes_a_live_heartbeat(tmp_path: Path) -> None:
    state = _state(tmp_path)
    identity = _identity(session_id="sess-owner", host="notebook", harness="claude")
    first = assume(state, identity, owner_decision=True, now=NOW)
    later = consult(state, identity, now=NOW + timedelta(seconds=100))
    assert later["coordinator"]["session_id"] == "sess-owner"
    assert later["coordinator"]["expires_at"] != first["coordinator"]["expires_at"]
    assert later["coordinator"]["freshness"] == "current"


def test_briefing_renders_shared_sections_with_freshness_and_not_handoff(tmp_path: Path) -> None:
    identity = _identity(session_id="sess-a", host="notebook", harness="codex")
    report = collect_briefing(
        identity=identity,
        now=NOW,
        state=_state(tmp_path),
        **_loaders(),
    )
    text = render_text(report)
    parsed = json.loads(render_json(report))
    for header in ("## Coordenador", "## Sessoes paralelas", "## Fila", "## Cotas", "## Runs vivos", "## PRs"):
        assert header in text
    assert "observado_em=2026-11-11T12:00:00Z" in text
    assert "frescor=vigente" in text
    assert "Fila compartilhada" in text
    assert "PR compartilhado" in text
    assert "run-1" in text
    assert "HANDOFF" not in text
    assert "FASE_ATUAL" not in text
    assert parsed["queue"]["source"] == "control_store"
    assert parsed["quotas"]["items"][0]["freshness"] == "current"
    assert parsed["pull_requests"]["items"][0]["freshness"] == "stale"
    assert parsed["observed_at"] == "2026-11-11T12:00:00Z"


def test_a_failed_provider_does_not_drop_the_other_sections(tmp_path: Path) -> None:
    def boom(_now: datetime) -> SectionReading:
        raise RuntimeError("quota down")

    loaders = _loaders()
    loaders["quota_loader"] = boom
    report = collect_briefing(
        identity=_identity(session_id="sess-a", host="notebook"),
        now=NOW,
        state=_state(tmp_path),
        **loaders,
    )
    assert report["quotas"]["freshness"] == "unavailable"
    assert report["quotas"]["cause"] == "RuntimeError"
    assert report["queue"]["items"][0]["id"] == "USR-1"
    assert report["live_runs"]["items"][0]["run_id"] == "run-1"


def test_synced_quota_keeps_a_stale_sample_unconfirmed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for provider in ("openai", "xai", "google", "anthropic"):
        monkeypatch.delenv(f"DARKFAC_{provider.upper()}_USAGE_JSON", raising=False)
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    provider_dir = tmp_path / "usage" / "providers"
    provider_dir.mkdir(parents=True)
    fresh = {
        "status": "connected",
        "checked_at": "2026-11-11T11:59:30Z",
        "windows": [{"quota_id": "week", "label": "semanal", "remaining_percent": 80}],
    }
    stale = {
        "status": "connected",
        "checked_at": "2026-11-05T01:00:00Z",
        "windows": [{"quota_id": "week", "label": "semanal", "remaining_percent": 12}],
    }
    (provider_dir / "anthropic.json").write_text(json.dumps(fresh), encoding="utf-8")
    (provider_dir / "openai.json").write_text(json.dumps(stale), encoding="utf-8")
    section = briefing.load_quota_section(NOW)
    by_harness = {item["harness"]: item for item in section.items}
    assert by_harness["claude"]["freshness"] == "current"
    assert by_harness["claude"]["confirmed"] is True
    assert by_harness["codex"]["freshness"] == "stale"
    assert by_harness["codex"]["confirmed"] is False
    assert by_harness["codex"]["remaining_percent"] == 12
    assert section.freshness == "stale"
    assert by_harness["grok"]["freshness"] == "unavailable"
    assert by_harness["grok"]["confirmed"] is False


def test_queue_and_runs_do_not_read_the_local_ledger_or_handoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("DARKFAC_HF02_DATABASE_URL", raising=False)
    monkeypatch.delenv("DARKHUB_CONTROL_DATABASE_URL", raising=False)
    monkeypatch.delenv("DARKHUB_LINE_DATABASE_URL", raising=False)
    opened: list[str] = []
    original = Path.read_text

    def spy(self: Path, *args: object, **kwargs: object) -> str:
        opened.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    queue = briefing.load_queue_section(NOW)
    runs = briefing.load_live_runs_section(NOW)
    assert queue.freshness == "unavailable"
    assert queue.cause == "control_store_absent"
    assert runs.freshness == "unavailable"
    assert runs.cause is not None and runs.cause.startswith("missing")
    joined = "\n".join(opened)
    assert "demands.json" not in joined
    assert "HANDOFF.md" not in joined
    assert "FASE_ATUAL.md" not in joined


def test_cli_refuses_assumption_without_the_owner_mark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _silence_providers(monkeypatch)
    path = str(tmp_path / "shared.sqlite3")
    code = briefing.main(["assumir", "--state", path, "--json"])
    captured = capsys.readouterr()
    assert code == 2
    body = json.loads(captured.out)
    assert body["ok"] is False
    assert body["cause"] == "owner_decision_required"
    assert body["observed_at"]
    assert _lease_count(SharedState(path)) == 0


def test_cli_consult_with_the_owner_flag_does_not_assume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _silence_providers(monkeypatch)
    code = briefing.main(["consultar", "--decisao-owner", "--state", str(tmp_path / "shared.sqlite3"), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["assumed"] is False
    assert report["coordinator"] is None
    assert report["absence_reason"] == "no_coordinator"


def test_cli_owner_assumption_prints_the_resulting_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _silence_providers(monkeypatch)
    monkeypatch.setenv("DARKFAC_SESSION_ID", "sess-cli")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-cli")
    monkeypatch.setenv("DARKFAC_OPERATING_HARNESS", "antigravity")
    path = tmp_path / "shared.sqlite3"
    code = briefing.main(["assumir", "--decisao-owner", "--state", str(path), "--json"])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["assumed"] is True
    assert report["coordinator"]["session_id"] == "sess-cli"
    assert report["coordinator"]["harness"] == "codex"
    assert report["coordinator"]["origin"] == "owner_decision"
    assert report["coordinator"]["freshness"] == "current"
    human = briefing.main(["consultar", "--state", str(path)])
    text = capsys.readouterr().out
    assert human == 0
    assert "sess-cli" in text
    assert "frescor=vigente" in text
    assert "## Cotas" in text


def _silence_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = _section("silenced")

    def loader(_now: datetime) -> SectionReading:
        return empty

    monkeypatch.setattr(briefing, "load_queue_section", loader)
    monkeypatch.setattr(briefing, "load_quota_section", loader)
    monkeypatch.setattr(briefing, "load_live_runs_section", loader)
    monkeypatch.setattr(briefing, "load_pull_request_section", loader)
