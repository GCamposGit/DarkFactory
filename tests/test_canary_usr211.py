"""USR-211: canary never waits for a human, stale canaries are swept, mojibake titles are repaired."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from core.line import canary, stage_grill
from core.workflow.control_store import SQLiteControlStore


def _accept_canary(store: SQLiteControlStore, created: datetime) -> str:
    day = created.date()
    cmd = canary.build_intake_command(day, "normal")
    return store.accept(cmd, created).run_id


def test_canary_grill_is_always_auto() -> None:
    cmd = canary.build_intake_command(datetime.now(UTC).date(), "normal")
    assert cmd.payload["grill_policy"] == "auto"
    canary_project = SimpleNamespace(id=canary.CANARY_PROJECT_ID)
    assert stage_grill.is_auto_grill(canary_project, {})
    assert stage_grill.is_auto_grill(SimpleNamespace(id="darkfac-canary"), None)
    # Non-canary projects keep the normal (human) grill.
    assert not stage_grill.is_auto_grill(SimpleNamespace(id="darkfac"), {})


def test_repair_mojibake_fixes_double_decoded_title() -> None:
    good = "Canário diário 2026-09-30"
    garbled = good.encode("utf-8").decode("latin-1")
    assert garbled != good
    assert canary.repair_mojibake(garbled) == good
    # Correct text, plain ASCII and non-latin-1 text are untouched.
    assert canary.repair_mojibake(good) == good
    assert canary.repair_mojibake("Canario diario") == "Canario diario"
    assert canary.repair_mojibake("日本語") == "日本語"
    assert canary.repair_mojibake("") == ""


def test_sweep_closes_stale_canary_with_evidence(tmp_path: Path) -> None:
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    created = datetime.now(UTC) - timedelta(days=3)
    run_id = _accept_canary(store, created)
    evidence = tmp_path / "evidence"

    closed = canary.sweep_stale_canaries(store, now=created + timedelta(hours=25), evidence_dir=evidence)

    assert [r["run_id"] for r in closed] == [run_id]
    assert closed[0]["cause_code"] == canary.STALE_CANARY_CAUSE
    assert closed[0]["outcome"] == "failed"
    assert "Can" in closed[0]["title"]
    saved = json.loads((evidence / f"{run_id}.json").read_text(encoding="utf-8"))
    assert saved["run_id"] == run_id
    assert store.get_run_status(run_id)["status"] != "active"
    assert run_id not in store.list_active_run_ids()


def test_sweep_keeps_fresh_canary(tmp_path: Path) -> None:
    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    created = datetime.now(UTC)
    run_id = _accept_canary(store, created)

    assert canary.sweep_stale_canaries(store, now=created + timedelta(hours=23)) == []
    assert run_id in store.list_active_run_ids()
