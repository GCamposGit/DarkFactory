"""Owner-action reconciliation proposes outcomes and never loses history (USR-307)."""

from __future__ import annotations

import json
from pathlib import Path

from core.owner_actions import reconcile as rc
from core.owner_actions.cli import run
from core.owner_actions.models import ActionStatus, OwnerAction, ActionStep
from core.owner_actions.store import OwnerActionStore


def _oa(oa_id: str, blocks: list[str], why: str = "porque") -> OwnerAction:
    return OwnerAction(id=oa_id, title="Titulo da acao", why=why, blocks=blocks,
                       steps=[ActionStep(text="Passo unico")], verify="v")


def _store(tmp_path: Path, actions: list[OwnerAction]) -> OwnerActionStore:
    path = tmp_path / "oa.json"
    path.write_text(json.dumps({"schema_version": 1, "actions": [a.to_record() for a in actions]}), encoding="utf-8")
    return OwnerActionStore(path)


def test_classification_covers_the_three_classes() -> None:
    tickets = {"USR-1": "completed", "USR-2": "planned", "USR-3": "cancelled"}
    qr = _oa("OA-QR-9", ["USR-9"], why="Motivo. PR: https://github.com/x/y/pull/9")
    props = {
        p.action_id: p
        for p in rc.reconcile(
            [_oa("OA-001", ["USR-1", "USR-3"]), _oa("OA-002", ["USR-1", "USR-2"]), _oa("OA-003", ["USR-2"]), qr],
            tickets, lambda n: ("CLOSED", False),
        )
    }
    assert props["OA-001"].classification is rc.Classification.OBSOLETE_BLOCKER and props["OA-001"].outcome == "update_blocks"
    assert props["OA-001"].new_blocks == ()
    assert props["OA-002"].classification is rc.Classification.STILL_NEEDED
    assert props["OA-002"].new_blocks == ("USR-2",) and props["OA-002"].dropped_blocks == ("USR-1",)
    assert props["OA-003"].outcome == "keep" and not props["OA-003"].safe
    assert props["OA-QR-9"].classification is rc.Classification.ORPHAN_PR and props["OA-QR-9"].outcome == "close"


def test_open_pr_is_kept_and_obsolete_blockers_are_never_closed() -> None:
    qr = _oa("OA-QR-9", ["USR-1"])
    [prop] = rc.reconcile([qr], {"USR-1": "completed"}, lambda n: ("OPEN", False))
    assert prop.outcome == "keep"
    [prop] = rc.reconcile([_oa("OA-004", ["USR-1"])], {"USR-1": "completed"})
    assert prop.outcome != "close"


def test_apply_preserves_fields_and_appends_history(tmp_path: Path) -> None:
    store = _store(tmp_path, [_oa("OA-001", ["USR-1"]), _oa("OA-002", ["USR-1"]),
                              _oa("OA-QR-9", ["USR-9"], why="PR: https://github.com/x/y/pull/9")])
    props = rc.reconcile(store.list_actions(include_done=False), {"USR-1": "completed"}, lambda n: ("CLOSED", False))
    applied = rc.apply_proposals(store, props, only=["OA-001", "OA-QR-9"])
    assert {p.action_id for p in applied} == {"OA-001", "OA-QR-9"}
    one, two, qr = store.get("OA-001"), store.get("OA-002"), store.get("OA-QR-9")
    assert one and one.blocks == [] and one.status is ActionStatus.OPEN and one.steps[0].text == "Passo unico"
    assert one.history and one.history[0]["outcome"] == "obsolete_blocker:update_blocks" and one.history[0]["note"]
    assert two and two.blocks == ["USR-1"] and not two.history  # untouched: not in --id filter
    assert qr and qr.status is ActionStatus.DONE and qr.resolved_at and qr.blocks == ["USR-9"]
    assert qr.history[0]["outcome"] == "orphan_pr:close"


def test_cli_reconcile_dry_run_then_apply(tmp_path: Path) -> None:
    store = _store(tmp_path, [_oa("OA-001", ["USR-1"])])
    demands = tmp_path / "demands.json"
    demands.write_text(json.dumps([{"id": "USR-1", "status": "completed"}]), encoding="utf-8")

    class Out:
        text = ""

        def write(self, value: str) -> None:
            self.text += value

    out = Out()
    assert run(["reconcile", "--no-gh", "--demands", str(demands)], store=store, out=out) == 0  # type: ignore[arg-type]
    assert "[seguro]" in out.text and store.get("OA-001").blocks == ["USR-1"]
    assert run(["reconcile", "--no-gh", "--apply", "--demands", str(demands)], store=store, out=Out()) == 0  # type: ignore[arg-type]
    assert store.get("OA-001").blocks == []
