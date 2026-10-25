"""Automatic catalog/evolution registration from ticket deliveries (USR-220).

Delivery runs against a temporary git repo (local clone + bare origin) with a scripted ``gh``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.catalog import delivery
from core.catalog.manager import CrossProjectCatalogManager
from core.evolution.engine import FactoryEvolutionEngine
from tests.test_git_autonomy_ci_gate import (  # noqa: F401 - fixtures + helpers
    FakeClock,
    FakeRepoGh,
    _git,
    _repo_template,
    manager,
    repo,
)


@pytest.fixture(autouse=True)
def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(state))
    return state


def _write(local: Path) -> None:
    (local / "core").mkdir(exist_ok=True)
    (local / "core" / "newmod.py").write_text(
        "from pydantic import BaseModel\n\n\nclass Thing(BaseModel):\n    a: int = 1\n", encoding="utf-8"
    )
    (local / "core" / "_private.py").write_text("def _x():\n    pass\n", encoding="utf-8")
    (local / "scripts").mkdir(exist_ok=True)
    (local / "scripts" / "tool.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    skill = local / ".agents" / "skills" / "99-demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    (local / "AGENTS.md").write_text("rules\n", encoding="utf-8")


def _deliver(local: Path, bare: Path, **kw):
    return manager(local, FakeClock()).deliver_branch("USR-90", "Gate", cwd=local, gh_runner=FakeRepoGh(bare, []), **kw)


def test_delivery_registers_components_and_evolutions(repo) -> None:
    local, bare = repo
    _write(local)
    rep = _deliver(local, bare)
    assert rep.ok and rep.action == "merged", rep.message

    comps = {c.id: c for c in CrossProjectCatalogManager().list_components()}
    assert comps["core-newmod"].kind.value == "schema_contract"
    assert comps["script-tool"].kind.value == "script_utility"
    assert comps["skill-99-demo"].metadata["ticket_id"] == "USR-90"
    assert comps["core-newmod"].metadata["pr_url"] == "https://github.com/x/y/pull/1"
    assert "core-private" not in comps
    evo = {p.target_path: p for p in FactoryEvolutionEngine().list_proposals()}
    assert evo["AGENTS.md"].target_kind.value == "context_rule"
    assert evo["AGENTS.md"].metadata["pr_url"].endswith("/pull/1")
    # Re-registration is idempotent.
    before = len(CrossProjectCatalogManager().list_components())
    again, _ = delivery.classify_changes("USR-90", "Gate", [("A", "core/newmod.py")], lambda p: (local / p).read_text())
    delivery.apply_registration(again, [])
    assert len(CrossProjectCatalogManager().list_components()) == before


def test_catalog_failure_does_not_break_delivery(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    local, bare = repo
    _write(local)

    def boom(*a, **k):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(CrossProjectCatalogManager, "register_component", boom)
    rep = _deliver(local, bare)
    assert rep.ok and rep.action == "merged", rep.message

    # Even if the registration entry point itself explodes, delivery is unaffected.
    local2_marker = delivery.register_delivery
    monkeypatch.setattr(delivery, "register_delivery", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert callable(local2_marker)
    mgr = manager(local, FakeClock())
    mgr._register_catalog("USR-90", "Gate", local, "main", "")  # must not raise


def test_queue_delivery_never_registers_and_still_refuses_non_ledger(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    local, bare = repo
    calls: list[str] = []
    monkeypatch.setattr(delivery, "register_delivery", lambda *a, **k: calls.append("x"))
    _write(local)
    rep = _deliver(local, bare, kind="queue")
    assert not rep.ok and rep.action == "error"
    assert calls == []


def test_explicit_tag_groups_changed_files() -> None:
    files = {"core/a.py": "def f():\n    pass\n", "docs/x.md": "d\n"}
    comps, _ = delivery.classify_changes(
        "USR-5", "Thing", [("A", "core/a.py"), ("A", "docs/x.md"), ("A", "tests/test_a.py")],
        files.get, tags=["catalog:My Widget"],
    )
    assert [c.id for c in comps] == ["my-widget"]
    assert sorted(f.path for f in comps[0].files) == ["core/a.py", "docs/x.md"]




def test_api_empty_state_only_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.backend.main import app

    client = TestClient(app)
    data = client.get("/api/catalog/components").json()
    assert data["count"] > 0 and data["empty_reason"] is None
    monkeypatch.setattr(CrossProjectCatalogManager, "list_components", lambda self, kind=None: [])
    data = client.get("/api/catalog/components").json()
    assert data["count"] == 0 and "USR-220" in data["empty_reason"]
    evo = client.get("/api/evolution/status").json()
    assert evo["empty_reason"] and not evo["proposals"]


# ----------------------------------------------------------------------------
# Notebook -> Hub channel (no network)
# ----------------------------------------------------------------------------

def _payload() -> dict:
    comps, evos = delivery.classify_changes(
        "USR-9", "T", [("A", "core/zed.py"), ("M", ".agents/skills/x/SKILL.md")],
        lambda p: "def f():\n    pass\n",
    )
    return delivery.build_payload(comps, evos)


def test_delivery_sync_endpoint_auth_and_idempotent_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    from hub.backend.main import app

    client = TestClient(app)
    monkeypatch.delenv("DARKFAC_TELEMETRY_KEY", raising=False)
    assert client.post("/api/catalog/delivery-sync", json=_payload()).status_code == 503
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "k-secret")
    assert client.post("/api/catalog/delivery-sync", json=_payload()).status_code == 401
    bad = {"X-DarkFac-Telemetry-Key": "nope"}
    assert client.post("/api/catalog/delivery-sync", json=_payload(), headers=bad).status_code == 401
    ok = {"X-DarkFac-Telemetry-Key": "k-secret"}
    r = client.post("/api/catalog/delivery-sync", json=_payload(), headers=ok)
    assert r.status_code == 200 and r.json()["components"] == ["core-zed"] and len(r.json()["evolutions"]) == 1
    r2 = client.post("/api/catalog/delivery-sync", json=_payload(), headers=ok)
    assert r2.json()["evolutions"] == []  # already present
    comps = CrossProjectCatalogManager().list_components()
    assert len([c for c in comps if c.id == "core-zed"]) == 1
    evil = {"components": [{"id": "atrium-seo-validator", "name": "x", "kind": "script_utility",
                            "source_project_id": "p", "description": "hacked"}]}
    client.post("/api/catalog/delivery-sync", json=evil, headers=ok)
    assert CrossProjectCatalogManager().get_component("atrium-seo-validator").description != "hacked"


def test_delivery_sync_caps_items() -> None:
    many = {"components": [{"id": f"c{i}", "name": "n" * 900, "kind": "script_utility",
                            "source_project_id": "p", "description": "d"} for i in range(300)]}
    comps, _ = delivery.sanitize_payload(many)
    assert len(comps) == 200 and len(comps[0].name) == 200


def test_push_failure_never_breaks_delivery(repo, monkeypatch: pytest.MonkeyPatch) -> None:
    local, bare = repo
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "k-secret")
    seen: list[dict] = []

    def failing_post(url, **kw):
        seen.append(kw["headers"])
        raise TimeoutError("network down")

    assert delivery.push_to_hub(_payload(), post=failing_post) is False
    assert seen and seen[0]["X-DarkFac-Telemetry-Key"] == "k-secret"
    monkeypatch.setattr(delivery, "push_to_hub", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    _write(local)
    rep = _deliver(local, bare)
    assert rep.ok and rep.action == "merged", rep.message


def test_push_sends_to_configured_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "k")
    monkeypatch.setenv("DARKHUB_CLOUD_URL", "http://hub.test/")
    calls = []

    class R:
        status_code = 200

    ok = delivery.push_to_hub(_payload(), repo_root=tmp_path, post=lambda url, **kw: calls.append((url, kw["timeout"])) or R())
    assert ok and calls == [("http://hub.test/api/catalog/delivery-sync", 5.0)]
    monkeypatch.delenv("DARKFAC_TELEMETRY_KEY")
    assert delivery.push_to_hub(_payload(), repo_root=tmp_path, post=lambda *a, **k: R()) is False
