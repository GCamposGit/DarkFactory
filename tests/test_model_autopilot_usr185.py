"""USR-185 slice 9b: telemetry rollback, real Grill pending, model PR delivery and the idempotent daily run."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.models import autopilot, lifecycle as lc, pending, pr, telemetry
from core.models import registry as reg

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
TS = "2026-10-21T10:00:00+00:00"


def _model(mid: str, family: str, status: str = "active", qual: str | None = "grandfathered", **extra: Any) -> dict[str, Any]:
    m: dict[str, Any] = {"id": mid, "provider": "p", "family": family, "tier": "mid", "status": status, **extra}
    if qual:
        q: dict[str, Any] = {"status": qual}
        if status == "candidate":
            q["evidence_path"] = "q.json"
        m["qualification"] = q
    return m


def _raw(candidate_price: float = 1.0, promoted: bool = False) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "schema_version": 1,
        "quota_floor_percent": 15.0,
        "forbidden_autonomous_markers": ["fable"],
        "models": [
            _model("dev-old", "claude"),
            _model("dev-new", "claude", "active" if promoted else "candidate", "approved",
                   price_input_per_mtok=candidate_price, price_output_per_mtok=candidate_price * 2),
            _model("rev-a", "openai"),
        ],
        "roles": {"development.x": "dev-new" if promoted else "dev-old", "review.x": "rev-a"},
        "review_pairs": [{"implementer": "development.x", "reviewer": "review.x"}],
        "rollback_thresholds": {"min_samples": 5, "max_failure_rate": 0.4, "max_escalation_rate": 0.5,
                                "max_cost_per_call_usd": 0.5, "window_days": 7},
    }
    if promoted:
        raw["role_previous"] = {"development.x": "dev-old"}
    return raw


def _events(model: str, ok: int, bad: int, cost: float = 0.01) -> list[dict[str, Any]]:
    out = [{"model": model, "success": True, "cost_usd": cost, "timestamp": TS, "ticket_id": f"T{i}",
            "invocation_id": f"o{model}{i}"} for i in range(ok)]
    out += [{"model": model, "success": False, "cost_usd": cost, "timestamp": TS, "ticket_id": f"F{i}",
             "invocation_id": f"b{model}{i}"} for i in range(bad)]
    return out


@pytest.fixture()
def promoted(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(_raw(promoted=True)), encoding="utf-8")
    return {"registry_path": path, "ledger_path": tmp_path / "ledger.jsonl", "now": NOW}


def _evaluate(env: dict[str, Any], events: list[dict[str, Any]]):
    registry = reg.load_registry(env["registry_path"])
    stats = telemetry.collect_telemetry(registry, now=NOW, events=events)
    return telemetry.evaluate_post_promotion(registry, stats, **env), stats


def test_rollback_above_threshold(promoted: dict[str, Any]) -> None:
    decisions, _ = _evaluate(promoted, _events("dev-new", ok=3, bad=4))
    assert [(d.model_id, d.reason, d.applied) for d in decisions] == [("dev-new", "gate_failure", True)]
    assert reg.load_registry(promoted["registry_path"]).roles["development.x"] == "dev-old"
    assert lc.read_ledger(promoted["ledger_path"])[0]["action"] == "rollback"


def test_no_rollback_below_threshold_or_samples(promoted: dict[str, Any]) -> None:
    decisions, stats = _evaluate(promoted, _events("dev-new", ok=8, bad=2))
    assert decisions == [] and stats["dev-new"].failure_rate == pytest.approx(0.2)
    decisions, _ = _evaluate(promoted, _events("dev-new", ok=0, bad=1))  # one failure never reverts
    assert decisions == []
    assert reg.load_registry(promoted["registry_path"]).roles["development.x"] == "dev-new"


def test_rollback_on_cost_and_escalation_counting(promoted: dict[str, Any]) -> None:
    decisions, _ = _evaluate(promoted, _events("dev-new", ok=6, bad=0, cost=0.9))
    assert decisions[0].reason == "cost"
    ev = [{"model": "m1", "success": False, "timestamp": TS, "ticket_id": "A"},
          {"model": "m2", "success": True, "timestamp": "2026-10-21T11:00:00+00:00", "ticket_id": "A"}]
    stats = telemetry.aggregate_events(ev)
    assert stats["m1"].escalations == 1 and stats["m2"].escalations == 0


def test_grill_generates_pending_and_notifies(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(_raw(candidate_price=50.0)), encoding="utf-8")
    sent: list[str] = []
    mirrored: list[Any] = []

    def handler(model_id: str, role: str | None, reasons: list[str]) -> None:
        pending.request_grill(model_id, role, reasons, now=NOW, send=lambda t: sent.append(t) or True,
                              mirror=mirrored.append, pending_dir=tmp_path / "pending")

    for _ in range(2):  # second attempt: same pending, no second notification
        with pytest.raises(lc.LifecycleError, match="WAITING_HUMAN"):
            lc.promote("dev-new", now=NOW, role="development.x", registry_path=path,
                       ledger_path=tmp_path / "l.jsonl", grill_handler=handler)
    assert len(sent) == 1 and "Recomendacao pronta" in sent[0] and "Passo a passo" in sent[0] and "dev-new" in sent[0]
    assert len(mirrored) == 1 and mirrored[0].kind == "grill"
    assert reg.load_registry(path).models[1].status == "candidate"  # still blocked


def _fake_ws_factory(root: Path, raw_fn):
    ws_dir = root / "ws"
    calls: list[Any] = []

    def factory(ticket_id: str, **kw: Any) -> Any:
        calls.append((ticket_id, kw))
        target = ws_dir / ".factory" / "config" / "model_registry.json"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(raw_fn()), encoding="utf-8")
        return SimpleNamespace(path=ws_dir, main_root=root)

    return factory, calls


def test_pr_receives_expected_arguments(tmp_path: Path) -> None:
    factory, _ = _fake_ws_factory(tmp_path, _raw)
    got: list[Any] = []

    def executor(t: str, title: str, cwd: Path, kind: str) -> Any:
        got.append((t, title, cwd, kind))
        return SimpleNamespace(ok=True, action="merged", pr_url="u", message="")

    res = pr.deliver_model_change("promover dev-new", lambda p: "ok", repo_root=tmp_path,
                                  workspace_factory=factory, cleanup_fn=lambda *a, **k: None, executor=executor)
    assert got == [("USR-185", "chore(models): promover dev-new", tmp_path / "ws", "registry")]
    assert res.opened and res.pr_url == "u"


def test_run_daily_is_idempotent(tmp_path: Path) -> None:
    factory, ws_calls = _fake_ws_factory(tmp_path, _raw)
    sent: list[str] = []
    delivered: list[Any] = []

    def executor(t: str, title: str, cwd: Path, kind: str) -> Any:
        delivered.append((t, title, kind))
        return SimpleNamespace(ok=True, action="merged", pr_url="u", message="")

    kw: dict[str, Any] = dict(
        now=NOW, repo_root=tmp_path, events=[], workspace_factory=factory, cleanup_fn=lambda *a, **k: None,
        executor=executor, send=lambda t: sent.append(t) or True, mirror=lambda r: None,
        pending_dir=tmp_path / "pending",
    )
    first = autopilot.run_daily(**kw)
    assert first.promoted == ["dev-new"] and first.pr and first.pr.opened
    assert delivered == [("USR-185", "chore(models): promover dev-new", "registry")]
    second = autopilot.run_daily(**kw)
    assert not second.changed and second.pr and not second.pr.opened and second.pr.action == "no_changes"
    assert len(delivered) == 1 and len(ws_calls) == 2


def test_run_daily_applies_discovered_candidates_in_worktree(tmp_path: Path) -> None:
    factory, _ = _fake_ws_factory(tmp_path, _raw)
    seen: list[Path] = []

    def applier(path: Path, state_dir: Path | None = None) -> list[str]:
        seen.append(path)
        return ["cand-x"] if len(seen) == 1 else []

    res = autopilot.run_daily(
        now=NOW, repo_root=tmp_path, events=[], workspace_factory=factory, cleanup_fn=lambda *a, **k: None,
        executor=lambda *a: SimpleNamespace(ok=True, action="merged", pr_url="u", message=""),
        send=lambda t: True, mirror=lambda r: None, pending_dir=tmp_path / "pending",
        apply_candidates_func=applier,
    )
    assert seen == [tmp_path / "ws" / ".factory" / "config" / "model_registry.json"]
    assert res.discovered == ["cand-x"] and res.pr and res.pr.opened
