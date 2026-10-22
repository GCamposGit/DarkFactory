"""USR-341: real OpenRouter qualification executor (fake HTTP, no network)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from core.models import openrouter_qualifier as oq
from core.models import qualification as q
from core.models import registry as reg

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
CAND = "acme/model-x"
BATTERY = q.load_battery()
ANSWERS = {t["prompt"]: t["expected"] for t in BATTERY["reference_tasks"]}


def _registry(tmp_path: Path, price: bool = False) -> Path:
    raw = json.loads(reg.DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    raw["models"].append({
        "id": CAND, "provider": "acme", "family": "acme", "tier": "unclassified", "aliases": [], "gateway_ids": [],
        "status": "candidate", "price_input_per_mtok": 1.0 if price else None,
        "price_output_per_mtok": 2.0 if price else None, "capabilities": ["text"], "forbidden_autonomous": False,
    })
    path = tmp_path / "model_registry.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


class FakeHttp:
    def __init__(self, cost: float = 0.01) -> None:
        self.calls = 0
        self.cost = cost

    def __call__(self, url: str, headers: dict, body: dict) -> dict:
        self.calls += 1
        assert headers["Authorization"] == "Bearer k"
        prompt = body["messages"][0]["content"]
        msg: dict = {"content": ANSWERS.get(prompt, '{"ok": true, "model": "x"}')}
        if "tools" in body:
            msg = {"content": "", "tool_calls": [{"function": {"name": "add", "arguments": '{"a": 2, "b": 3}'}}]}
        return {"choices": [{"message": msg}], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": self.cost}}


def _qualify(tmp_path: Path, http, *, env=None, policy=None, catalog=None):
    fn = oq.make_qualify_func(
        now=NOW, env={"OPENROUTER_API_KEY": "k"} if env is None else env, policy=policy, http_post=http,
        fetch_catalog=lambda: catalog or [], spend_dir=tmp_path / "spend", evidence_dir=tmp_path / "ev",
    )
    return fn


def _entry(path: Path) -> dict:
    return next(m for m in json.loads(path.read_text(encoding="utf-8"))["models"] if m["id"] == CAND)


def test_no_key_does_not_run(tmp_path):
    assert _qualify(tmp_path, FakeHttp(), env={}) is None


def test_unknown_price_goes_to_grill_without_calling(tmp_path):
    path, http = _registry(tmp_path), FakeHttp()
    _qualify(tmp_path, http)(path, [CAND])
    assert http.calls == 0
    assert _entry(path)["qualification"] is None if "qualification" in _entry(path) else True


def test_price_filled_from_catalog_then_qualified_and_cost_recorded(tmp_path):
    path, http = _registry(tmp_path), FakeHttp(cost=0.01)
    catalog = [{"id": CAND, "pricing": {"prompt": "0.000001", "completion": "0.000002"}}]
    _qualify(tmp_path, http, catalog=catalog)(path, [CAND])
    e = _entry(path)
    assert e["price_input_per_mtok"] == 1.0 and e["price_output_per_mtok"] == 2.0
    assert e["qualification"]["status"] == "approved"
    assert http.calls == 5
    assert abs(e["qualification"]["cost_usd"] - 0.05) < 1e-9  # real usage.cost, not the estimate
    ledger = json.loads((tmp_path / "spend" / "openrouter_spend.json").read_text(encoding="utf-8"))
    assert abs(ledger["total_usd"] - 0.05) < 1e-9 and ledger["date"] == "2026-10-22"


def test_daily_cap_refuses_without_calling(tmp_path):
    path, http = _registry(tmp_path, price=True), FakeHttp()
    (tmp_path / "spend").mkdir()
    (tmp_path / "spend" / "openrouter_spend.json").write_text(
        json.dumps({"date": "2026-10-22", "total_usd": 0.9999999, "by_model": {}}), encoding="utf-8")
    _qualify(tmp_path, http)(path, [CAND])
    assert http.calls == 0
    assert "qualification" not in _entry(path) or _entry(path)["qualification"] is None  # not a verdict


def test_candidate_cap_refuses_before_calling(tmp_path):
    path, http = _registry(tmp_path, price=True), FakeHttp(cost=0.4999)
    _qualify(tmp_path, http)(path, [CAND])
    assert http.calls == 1  # the next estimate would break the 0.50 per-candidate cap: refused without calling
    assert not (_entry(path).get("qualification"))  # a refusal is not a verdict


def test_candidate_spend_over_cap_fails_battery(tmp_path):
    path, http = _registry(tmp_path, price=True), FakeHttp(cost=0.2)
    _qualify(tmp_path, http)(path, [CAND])
    assert http.calls == 3
    assert _entry(path)["qualification"]["status"] == "failed"


def test_new_day_resets_ledger(tmp_path):
    ledger = oq.SpendLedger("2026-10-21", tmp_path)
    ledger.add("m", 0.9)
    assert oq.SpendLedger("2026-10-22", tmp_path).total == 0.0
    assert oq.SpendLedger("2026-10-21", tmp_path).total == 0.9


def test_policy_reflects_owner_decisions():
    p = oq.load_policy()
    assert (p.daily_cap_usd, p.per_candidate_cap_usd, p.api_key_env) == (1.0, 0.5, "OPENROUTER_API_KEY")
