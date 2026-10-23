import json

from core.owner_actions.remote_ledger import RemoteLedger
from core.owner_actions.store import OwnerActionStore


def _row(i, status="open"):
    return {"id": i, "title": "Titulo valido", "priority": "high", "status": status, "why": "w",
            "steps": [{"text": "passo um"}], "verify": "verificar", "created_at": "2026-10-22T00:00:00Z"}


def test_ledger_only_merge_updates_queue(tmp_path):
    local = tmp_path / "oa.json"
    local.write_text(json.dumps({"actions": [_row("OA-047")]}), encoding="utf-8")
    box = {"p": {"actions": [_row("OA-053")]}}
    t = {"now": 0.0}
    rl = RemoteLedger("o/r", ttl=100, fetch=lambda: box["p"], clock=lambda: t["now"])
    store = OwnerActionStore(local, remote_ledger=rl, remote_ref=None)
    assert [a.id for a in store.load().actions] == ["OA-053"]
    box["p"] = {"actions": [_row("OA-054")]}
    assert [a.id for a in store.load().actions] == ["OA-053"]  # cached
    t["now"] = 101
    assert [a.id for a in store.load().actions] == ["OA-054"]  # refreshed within TTL


def test_remote_failure_falls_back_to_local(tmp_path):
    local = tmp_path / "oa.json"
    local.write_text(json.dumps({"actions": [_row("OA-047")]}), encoding="utf-8")

    def boom():
        raise OSError("down")

    store = OwnerActionStore(local, remote_ledger=RemoteLedger("o/r", fetch=boom), remote_ref=None)
    assert [a.id for a in store.load().actions] == ["OA-047"]


def test_from_env_default_on_with_default_repo(monkeypatch):
    from core.owner_actions import remote_ledger as m
    for k in (m.ENV_ENABLE, m.ENV_REPO, m.ENV_TOKEN, m.ENV_TOKEN_FALLBACK):
        monkeypatch.delenv(k, raising=False)
    rl = m.from_env()
    assert rl is not None and rl.repo == "GCamposGit/DarkFactory" and rl.token is None


def test_from_env_disabled_and_token_fallback(monkeypatch):
    from core.owner_actions import remote_ledger as m
    monkeypatch.setenv(m.ENV_ENABLE, "off")
    assert m.from_env() is None
    monkeypatch.setenv(m.ENV_ENABLE, "1")
    monkeypatch.delenv(m.ENV_TOKEN, raising=False)
    monkeypatch.setenv(m.ENV_TOKEN_FALLBACK, "pat")
    assert m.from_env().token == "pat"
    monkeypatch.setenv(m.ENV_TOKEN, "gt")
    assert m.from_env().token == "gt"
