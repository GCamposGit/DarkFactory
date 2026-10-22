"""USR-185 slice 12: end-to-end proof with fakes only (no network, no real gh, no human touch).

Chain: fake provider -> daily discovery (state only) -> worktree apply + fake qualification -> promotion -> fake PR
(`registry` kind) -> post-promotion telemetry above the limit -> rollback -> second fake PR. Plus the Grill case: the
pending is raised, a recorded approval releases the promotion in the next cycle without asking again, and a refusal
closes the pending for good.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.models import autopilot, discovery, lifecycle as lc, pending
from core.models import qualification as q
from core.models import registry as reg
from core.models.discovery_sources import apply_candidates, run_daily_discovery
from tests.test_model_autopilot_usr185 import NOW, _events, _fake_ws_factory, _model, _raw
from tests.test_model_qualification_usr185 import _good

NEW_ID = "zz-claude-fiction-9"
DAY = "2026-10-22"
CFG = discovery.DiscoveryConfig(sources={"openrouter": discovery.SourceConfig(enabled=False)})


def _base_raw() -> dict[str, Any]:
    raw = _raw()
    raw["models"] = [m for m in raw["models"] if m["id"] != "dev-new"]
    raw["models"][0]["tier"] = "mid"
    return raw


class Recorder:
    """Fake PR executor + fake ``gh pr list`` (argv recorded) shared by the scenarios."""

    def __init__(self) -> None:
        self.delivered: list[tuple[str, str, str]] = []
        self.listed: list[list[str]] = []

    def executor(self, ticket: str, title: str, cwd: Path, kind: str) -> Any:
        self.delivered.append((ticket, title, kind))
        return SimpleNamespace(ok=True, action="merged", pr_url=f"https://example.test/pull/{len(self.delivered)}", message="")

    def lister(self, args: Any, cwd: Path) -> Any:
        self.listed.append(list(args))
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


def _kw(tmp_path: Path, rec: Recorder, raw_fn: Any, **extra: Any) -> dict[str, Any]:
    factory, _ = _fake_ws_factory(tmp_path, raw_fn)
    base: dict[str, Any] = dict(
        now=NOW, repo_root=tmp_path, workspace_factory=factory, cleanup_fn=lambda *a, **k: None,
        executor=rec.executor, pr_lister=rec.lister, send=lambda t: True, mirror=lambda r: None,
        pending_dir=tmp_path / "pending", role_for=lambda e, r: "development.x",
    )
    base.update(extra)
    return base


def test_full_chain_discovery_to_promotion_to_rollback_without_human_touch(tmp_path: Path) -> None:
    base = tmp_path / "base_registry.json"
    base.write_text(json.dumps(_base_raw()), encoding="utf-8")
    state = tmp_path / "state"
    http_calls: list[str] = []

    def http(url: str, headers: dict[str, str]) -> Any:
        http_calls.append(url)
        return {"data": [{"id": NEW_ID, "created_at": "2026-10-20T00:00:00Z"}]}

    # 1) daily discovery writes candidates to the state only, never the registry
    before = base.read_bytes()
    found = run_daily_discovery(base, CFG, http, {"ANTHROPIC_API_KEY": "k"}, state, DAY)
    assert found and found["new"] == [NEW_ID] and base.read_bytes() == before

    def qualify(path: Path, ids: list[str]) -> None:
        for mid in ids:
            result = q.run_battery(mid, q.RecordedExecutor(_good()), now=NOW)  # fake executor approves
            assert result.approved
            evidence = q.persist_evidence(result, tmp_path / "evidence")
            q.record_in_registry(result, evidence, path)
            raw = json.loads(path.read_text(encoding="utf-8"))  # the provider listing has no price: measured here
            for entry in raw["models"]:
                if entry["id"] == mid:
                    entry.update(price_input_per_mtok=1.0, price_output_per_mtok=2.0)
            path.write_text(json.dumps(raw), encoding="utf-8")

    rec = Recorder()
    kw = _kw(tmp_path, rec, _base_raw, events=[], qualify_func=qualify,
             apply_candidates_func=lambda path, sd=None: apply_candidates(path, state))
    first = autopilot.run_daily(**kw)
    assert first.discovered == [NEW_ID] and first.promoted == [NEW_ID] and not first.blocked
    assert first.pr and first.pr.opened and first.pr.kind == "registry"
    assert rec.delivered == [("USR-185", "chore(models): descobrir zz-claude-fiction-9; promover zz-claude-fiction-9", "registry")]
    assert rec.listed and rec.listed[0][:3] == ["pr", "list", "--state"] and "headRefName" in rec.listed[0][-1]
    ws_registry = reg.load_registry(tmp_path / "ws" / ".factory" / "config" / "model_registry.json")
    assert ws_registry.roles["development.x"] == NEW_ID
    ledger = lc.read_ledger(tmp_path / "ws" / ".factory" / "models" / "lifecycle_ledger.jsonl")
    assert [e["action"] for e in ledger] == ["promote"]

    # 2) idempotent: nothing left to do, no second PR
    again = autopilot.run_daily(**kw)
    assert not again.changed and len(rec.delivered) == 1

    # 3) telemetry above the limit rolls back and opens another PR
    bad = _events(NEW_ID, ok=3, bad=4)
    kw["events"] = bad
    third = autopilot.run_daily(**kw)
    assert third.rolled_back == [NEW_ID] and third.pr and third.pr.opened
    assert rec.delivered[-1] == ("USR-185", "chore(models): rollback zz-claude-fiction-9", "registry")
    assert reg.load_registry(tmp_path / "ws" / ".factory" / "config" / "model_registry.json").roles["development.x"] == "dev-old"
    assert len(rec.delivered) == 2 and all(kind == "registry" for _, _, kind in rec.delivered)
    assert len(http_calls) == 1  # only the one fake provider call; nothing else touched the network


def _grill_raw() -> dict[str, Any]:
    return _raw(candidate_price=50.0)


def test_grill_pending_then_approval_promotes_in_next_cycle(tmp_path: Path) -> None:
    rec, sent = Recorder(), []
    answers: dict[str, str | None] = {}
    kw = _kw(tmp_path, rec, _grill_raw, events=[], send=lambda t: sent.append(t) or True,
             decision_reader=lambda action_id: answers.get(action_id))
    first = autopilot.run_daily(**kw)
    assert first.blocked == ["dev-new"] and not first.promoted and not first.pr.opened
    assert len(sent) == 1 and rec.delivered == []
    action_id = pending.decision_action_id("dev-new")
    assert action_id.startswith("OA-HR-model-promote-dev-new")
    # unanswered: still blocked, the owner is not asked again
    autopilot.run_daily(**kw)
    assert len(sent) == 1 and rec.delivered == []
    # the owner approves through the owner-action answer channel
    answers[action_id] = pending.APPROVE
    nxt = autopilot.run_daily(**kw)
    assert nxt.promoted == ["dev-new"] and nxt.pr and nxt.pr.opened and len(sent) == 1
    assert rec.delivered == [("USR-185", "chore(models): promover dev-new", "registry")]
    ledger = lc.read_ledger(tmp_path / "ws" / ".factory" / "models" / "lifecycle_ledger.jsonl")
    assert ledger[-1]["action"] == "promote" and "grill_approved" in ledger[-1]["reason"]
    stored = json.loads((tmp_path / "pending" / "dev-new.json").read_text(encoding="utf-8"))
    assert stored["decision"] == "approved"


def test_grill_refusal_closes_pending(tmp_path: Path) -> None:
    rec, sent = Recorder(), []
    answers: dict[str, str | None] = {}
    kw = _kw(tmp_path, rec, _grill_raw, events=[], send=lambda t: sent.append(t) or True,
             decision_reader=lambda action_id: answers.get(action_id))
    autopilot.run_daily(**kw)
    answers[pending.decision_action_id("dev-new")] = pending.REJECT
    refused = autopilot.run_daily(**kw)
    assert not refused.promoted and refused.skipped["dev-new"].startswith("grill_rejected")
    answers.clear()  # even if the answer disappears, the verdict is terminal and nobody is asked again
    later = autopilot.run_daily(**kw)
    assert not later.promoted and "dev-new" in later.skipped and len(sent) == 1 and rec.delivered == []
    assert reg.load_registry(tmp_path / "ws" / ".factory" / "config" / "model_registry.json").roles["development.x"] == "dev-old"


def test_approval_without_a_raised_pending_is_ignored(tmp_path: Path) -> None:
    assert pending.consume_decision("ghost", now=NOW, reader=lambda a: pending.APPROVE, pending_dir=tmp_path) is None


def test_approval_never_lifts_other_checks(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    raw = _raw()
    raw["models"][1]["qualification"] = {"status": "pending", "evidence_path": "q.json"}
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(lc.LifecycleError, match="approved qualification"):
        lc.promote("dev-new", now=NOW, role="development.x", registry_path=path, ledger_path=tmp_path / "l.jsonl",
                   grill_approved=True)


def test_duplicate_open_model_pr_is_not_reopened(tmp_path: Path) -> None:
    rec = Recorder()
    rows = [{"number": 7, "title": "chore(models): promover x", "headRefName": "ticket/usr-185", "url": "https://example.test/pull/7"}]
    kw = _kw(tmp_path, rec, _grill_raw, events=[], apply_candidates_func=lambda p, s=None: ["cand"])
    kw["pr_lister"] = lambda args, cwd: SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")
    res = autopilot.run_daily(**kw)
    assert res.pr and not res.pr.opened and res.pr.action == "duplicate_pr" and "#7" in res.pr.message
    assert rec.delivered == []
    by_branch = [{"number": 8, "title": "other", "headRefName": "models/auto-1", "url": "u"}]
    from core.models import pr as model_pr

    assert model_pr.find_open_model_pr(lambda a, c: SimpleNamespace(returncode=0, stdout=json.dumps(by_branch)), tmp_path)
    assert model_pr.find_open_model_pr(lambda a, c: SimpleNamespace(returncode=0, stdout="[]"), tmp_path) is None
    assert model_pr.find_open_model_pr(lambda a, c: SimpleNamespace(returncode=1, stdout="", stderr="x"), tmp_path) is None


def test_daily_hook_runs_once_per_day_and_never_raises(tmp_path: Path) -> None:
    calls: list[datetime] = []

    def runner(*, now: datetime, **kw: Any) -> Any:
        calls.append(now)
        return autopilot.DailyReport()

    day = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)
    assert autopilot.daily_hook(now=day, state_dir=tmp_path, runner=runner) is not None
    assert autopilot.daily_hook(now=day + timedelta(hours=3), state_dir=tmp_path, runner=runner) is None
    assert autopilot.daily_hook(now=day + timedelta(days=1), state_dir=tmp_path, runner=runner) is not None
    assert len(calls) == 2

    def boom(**kw: Any) -> Any:
        raise RuntimeError("x")

    assert autopilot.daily_hook(now=day + timedelta(days=2), state_dir=tmp_path, runner=boom) is None
    assert autopilot.daily_hook(now=day + timedelta(days=2), state_dir=tmp_path, runner=runner) is None  # marked


def test_async_hook_does_not_block_and_runs_one_thread(tmp_path: Path) -> None:
    import threading
    import time

    started = threading.Event()

    def slow(*, now: datetime, **kw: Any) -> Any:
        started.set()
        time.sleep(2)
        return autopilot.DailyReport()

    t0 = time.monotonic()
    thread = autopilot.daily_hook_async(now=NOW, state_dir=tmp_path, runner=slow)
    assert thread is not None and thread.daemon and time.monotonic() - t0 < 0.5
    assert started.wait(1)
    t1 = time.monotonic()
    assert autopilot.daily_hook_async(now=NOW, state_dir=tmp_path, runner=slow) is None  # one alive per process
    assert time.monotonic() - t1 < 0.5
    thread.join(5)
    assert autopilot.daily_hook_async(now=NOW, state_dir=tmp_path, runner=slow) is None  # marker: done today


def test_default_grill_mirror_leaves_checkout_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    repo = tmp_path / "checkout"
    (repo / ".factory" / "owner_actions").mkdir(parents=True)
    ledger = repo / ".factory" / "owner_actions" / "owner_actions.json"
    ledger.write_text('{"schema_version": 1, "actions": []}\n', encoding="utf-8")
    (repo / ".gitignore").write_text("hub/data/owner_actions/\n.factory/models/pending_grill/\n", encoding="utf-8")
    for cmd in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr(pending, "HUB_OWNER_ACTIONS_DIR", repo / "hub" / "data" / "owner_actions")
    monkeypatch.setattr(pending, "PENDING_DIR", repo / ".factory" / "models" / "pending_grill")
    import core.owner_actions.sync as oa_sync
    monkeypatch.setattr(oa_sync, "outbox_path", lambda: tmp_path / "state" / "outbox")
    monkeypatch.setattr("core.owner_actions.store.DEFAULT_PATH", ledger)

    kw = _kw(tmp_path, Recorder(), _grill_raw, events=[])
    kw.pop("mirror")  # default mirror
    res = autopilot.run_daily(**kw)
    assert res.blocked == ["dev-new"]
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout
    assert status.strip() == ""
    assert (repo / "hub" / "data" / "owner_actions" / "dynamic.json").is_file()
    assert pending.default_decision_reader(pending.decision_action_id("dev-new")) is None
