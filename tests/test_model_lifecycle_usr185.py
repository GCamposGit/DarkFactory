"""USR-185 slice 7: promotion, rollback and date-based retirement, audited in an append-only ledger."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from core.models import lifecycle as lc
from core.models import registry as reg

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 20)


def _model(mid: str, family: str, status: str = "active", qual: str | None = "grandfathered", **extra: Any) -> dict[str, Any]:
    m: dict[str, Any] = {"id": mid, "provider": "p", "family": family, "tier": "mid", "status": status, **extra}
    if qual:
        qualification = {"status": qual}
        if status == "candidate" and qual == "approved":
            qualification["evidence_path"] = "qualification.json"
        m["qualification"] = qualification
    return m


def _raw() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "quota_floor_percent": 15.0,
        "forbidden_autonomous_markers": ["fable"],
        "models": [
            _model("dev-old", "claude"),
            _model("dev-new", "claude", "candidate", "approved", price_input_per_mtok=0.5, price_output_per_mtok=2.0),
            _model("dev-unqualified", "claude", "candidate", None),
            _model("dev-failed", "claude", "candidate", "failed"),
            _model("claude-fable-9", "claude", "candidate", "approved", forbidden_autonomous=True),
            _model("rev-a", "openai"),
            _model("old-dep", "claude", "deprecated", retirement_date="2026-10-01"),
            _model("future-dep", "claude", "deprecated", retirement_date="2026-12-01"),
            _model("nodate-dep", "claude", "deprecated"),
        ],
        "roles": {"development.x": "dev-old", "review.x": "rev-a"},
        "review_pairs": [{"implementer": "development.x", "reviewer": "review.x"}],
    }


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Path]:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(_raw()), encoding="utf-8")
    return {"registry_path": path, "ledger_path": tmp_path / "ledger.jsonl"}


def _status(env: dict[str, Path], mid: str) -> str:
    return reg.load_registry(env["registry_path"]).models[[m.id for m in reg.load_registry(env["registry_path"]).models].index(mid)].status


def test_promote_approved_without_role(env: dict[str, Path]) -> None:
    rec = lc.promote("dev-new", now=NOW, **env)
    assert _status(env, "dev-new") == "active"
    assert rec["from_status"] == "candidate" and rec["to_status"] == "active" and rec["ts"] == NOW.isoformat()
    assert lc.read_ledger(env["ledger_path"]) == [rec]


def test_promote_refused_without_or_failed_qualification(env: dict[str, Path]) -> None:
    before = env["registry_path"].read_text(encoding="utf-8")
    for mid in ("dev-unqualified", "dev-failed"):
        with pytest.raises(lc.LifecycleError, match="qualification"):
            lc.promote(mid, now=NOW, **env)
    assert env["registry_path"].read_text(encoding="utf-8") == before
    assert lc.read_ledger(env["ledger_path"]) == []


def test_promote_binds_role_and_keeps_previous(env: dict[str, Path]) -> None:
    rec = lc.promote("dev-new", now=NOW, role="development.x", actor="tester", **env)
    r = reg.load_registry(env["registry_path"])
    assert r.roles["development.x"] == "dev-new"
    assert json.loads(env["registry_path"].read_text(encoding="utf-8"))["role_previous"] == {"development.x": "dev-old"}
    assert rec["from_model"] == "dev-old" and rec["to_model"] == "dev-new" and rec["actor"] == "tester"


def test_promote_precheck_can_veto(env: dict[str, Path]) -> None:
    with pytest.raises(lc.LifecycleError, match="vetoed"):
        lc.promote("dev-new", now=NOW, precheck=lambda raw, entry, role: "needs grill", **env)
    assert _status(env, "dev-new") == "candidate"


def test_promote_blocks_when_grill_is_required(env: dict[str, Path]) -> None:
    raw = json.loads(env["registry_path"].read_text(encoding="utf-8"))
    next(model for model in raw["models"] if model["id"] == "dev-new")["tier"] = "frontier"
    env["registry_path"].write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(lc.LifecycleError, match="WAITING_HUMAN.*frontier"):
        lc.promote("dev-new", now=NOW, **env)

    assert _status(env, "dev-new") == "candidate"
    assert lc.read_ledger(env["ledger_path"]) == []


def test_rollback_restores_previous_and_audits(env: dict[str, Path]) -> None:
    lc.promote("dev-new", now=NOW, role="development.x", **env)
    audits = lc.rollback("development.x", "gate_failure", now=NOW, **env)
    r = reg.load_registry(env["registry_path"])
    assert r.roles["development.x"] == "dev-old"
    assert _status(env, "dev-new") == "deprecated"
    assert "role_previous" not in json.loads(env["registry_path"].read_text(encoding="utf-8"))
    assert [a["action"] for a in audits] == ["rollback", "deprecate"]
    assert audits[0]["reason"] == "gate_failure" and audits[0]["to_model"] == "dev-old"
    assert [a["action"] for a in lc.read_ledger(env["ledger_path"])] == ["promote", "rollback", "deprecate"]


@pytest.mark.parametrize("reason", ["cost", "quota"])
def test_rollback_by_model_id_with_each_signal(env: dict[str, Path], reason: str) -> None:
    lc.promote("dev-new", now=NOW, role="development.x", **env)
    lc.rollback("dev-new", reason, now=NOW, **env)
    assert reg.load_registry(env["registry_path"]).roles["development.x"] == "dev-old"


def test_rollback_refusals(env: dict[str, Path]) -> None:
    with pytest.raises(lc.LifecycleError, match="reason"):
        lc.rollback("development.x", "vibes", now=NOW, **env)
    with pytest.raises(lc.LifecycleError, match="no previous"):
        lc.rollback("development.x", "cost", now=NOW, **env)
    with pytest.raises(lc.LifecycleError, match="neither a role"):
        lc.rollback("nope", "cost", now=NOW, **env)
    assert lc.read_ledger(env["ledger_path"]) == []


def test_forbidden_never_becomes_role_or_active(env: dict[str, Path]) -> None:
    before = env["registry_path"].read_text(encoding="utf-8")
    with pytest.raises(lc.LifecycleError, match="forbidden"):
        lc.promote("claude-fable-9", now=NOW, role="development.x", **env)
    with pytest.raises(lc.LifecycleError, match="forbidden"):
        lc.promote("claude-fable-9", now=NOW, **env)
    assert env["registry_path"].read_text(encoding="utf-8") == before


def test_retire_by_date_and_idempotent(env: dict[str, Path]) -> None:
    out = lc.retire_due(TODAY, now=NOW, **env)
    assert [a["model_id"] for a in out] == ["old-dep"]
    assert _status(env, "old-dep") == "retired"
    assert _status(env, "future-dep") == "deprecated" and _status(env, "nodate-dep") == "deprecated"
    assert lc.retire_due(TODAY, now=NOW, **env) == []
    assert len(lc.read_ledger(env["ledger_path"])) == 1
    assert lc.retire_due(date(2026, 12, 1), now=NOW, **env)[0]["model_id"] == "future-dep"


def test_invalid_resulting_registry_writes_nothing(env: dict[str, Path]) -> None:
    raw = json.loads(env["registry_path"].read_text(encoding="utf-8"))
    raw["models"].append(
        _model(
            "rev-claude",
            "claude",
            "candidate",
            "approved",
            price_input_per_mtok=0.5,
            price_output_per_mtok=2.0,
        )
    )
    env["registry_path"].write_text(json.dumps(raw), encoding="utf-8")
    before = env["registry_path"].read_text(encoding="utf-8")
    # binding a claude model to the review role would put reviewer in the implementer's family
    with pytest.raises(lc.LifecycleError, match="invalid"):
        lc.promote("rev-claude", now=NOW, role="review.x", **env)
    assert env["registry_path"].read_text(encoding="utf-8") == before
    assert lc.read_ledger(env["ledger_path"]) == []
    assert not env["registry_path"].with_suffix(".json.tmp").exists()
