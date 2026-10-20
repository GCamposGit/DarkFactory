"""USR-185 slice 6: deterministic qualification battery, budget, evidence and the active-requires-qualification rule."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.models import qualification as q
from core.models import registry as reg

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
CAND = "deepseek/deepseek-v4-pro"


def _good(cost: float = 0.0, tokens: int = 10) -> dict[str, q.ExecResult]:
    b = q.load_battery()
    out = {
        "contract": q.ExecResult('{"ok": true, "model": "x"}', input_tokens=tokens, cost_usd=cost),
        "tools": q.ExecResult(tool_calls=({"name": "add", "arguments": {"a": 2, "b": 3}},), input_tokens=tokens, cost_usd=cost),
    }
    for t in b["reference_tasks"]:
        out[f"reference:{t['id']}"] = q.ExecResult(t["expected"], input_tokens=tokens, cost_usd=cost)
    return out


def test_battery_approves_good_model_with_zero_cost() -> None:
    res = q.run_battery(CAND, q.RecordedExecutor(_good()), now=NOW)
    assert res.approved and res.status == "approved" and res.cost_usd == 0.0
    assert [s.step for s in res.steps][:2] == ["contract", "tools"]


@pytest.mark.parametrize("step", ["contract", "tools", "reference:sum_list"])
def test_battery_fails_on_wrong_answer(step: str) -> None:
    rec = _good()
    rec[step] = q.ExecResult("nope")
    res = q.run_battery(CAND, q.RecordedExecutor(rec), now=NOW)
    assert not res.approved and not res.budget_exceeded


def test_budget_usd_stops_and_fails() -> None:
    res = q.run_battery(CAND, q.RecordedExecutor(_good(cost=0.01)), now=NOW, budget=q.Budget(max_usd=0.015))
    assert res.budget_exceeded and not res.approved
    assert len(res.steps) == 2  # stopped at the second step, later steps never ran


def test_budget_tokens_stops_and_fails() -> None:
    res = q.run_battery(CAND, q.RecordedExecutor(_good(tokens=100)), now=NOW, budget=q.Budget(max_tokens=150))
    assert res.budget_exceeded and not res.approved


def test_executor_error_fails_step() -> None:
    res = q.run_battery(CAND, q.RecordedExecutor({}), now=NOW)
    assert not res.approved and "executor error" in res.steps[0].detail


def test_evidence_and_registry_record(tmp_path: Path) -> None:
    res = q.run_battery(CAND, q.RecordedExecutor(_good()), now=NOW)
    path = q.persist_evidence(res, tmp_path / "ev")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "deepseek__deepseek-v4-pro.json"
    assert data["status"] == "approved" and data["battery_version"] == res.battery_version and data["qualified_at"] == NOW.isoformat()
    assert len(data["steps"]) == len(res.steps)
    copy_path = tmp_path / "reg.json"
    copy_path.write_text(reg.DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    q.record_in_registry(res, path, copy_path)
    parsed = reg.parse_registry(json.loads(copy_path.read_text(encoding="utf-8")))
    entry = next(m for m in parsed.models if m.id == CAND)
    assert entry.status == "candidate"  # never promoted here
    assert entry.qualification is not None and entry.qualification.status == "approved"
    with pytest.raises(ValueError, match="not a candidate"):
        q.record_in_registry(
            q.QualificationResult("claude-haiku-5-5", "1", NOW.isoformat(), True, False, 0.0, 0), path, copy_path
        )


def _raw() -> dict:
    return copy.deepcopy(json.loads(reg.DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8")))


def test_candidate_without_qualification_cannot_become_active() -> None:
    raw = _raw()
    next(m for m in raw["models"] if m["id"] == CAND)["status"] = "active"
    with pytest.raises(ValidationError, match="without approved qualification"):
        reg.parse_registry(raw)


def test_failed_qualification_cannot_become_active_but_approved_can() -> None:
    raw = _raw()
    entry = next(m for m in raw["models"] if m["id"] == CAND)
    entry["status"] = "active"
    entry["qualification"] = {"status": "failed"}
    with pytest.raises(ValidationError, match="without approved qualification"):
        reg.parse_registry(raw)
    entry["qualification"] = {"status": "approved", "battery_version": "1.0.0"}
    reg.parse_registry(raw)
