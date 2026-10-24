"""USR-371: real project_id reaches pick/deliver_branch and export enforcement blocks."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.enterprise.enforcement import DataPolicyError

ROOT = Path(__file__).resolve().parents[1]


def test_run_ticket_passes_project_id_everywhere():
    src = (ROOT / "run_ticket.py").read_text(encoding="utf-8")
    assert src.count("project_id=ticket.project_id") >= 5
    assert "mode=DEVELOPMENT_MODE)" not in src


def test_export_blocked_for_local_only_project(tmp_path, monkeypatch):
    from core.enterprise.enforcement import enforce_data_policy

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    (tmp_path / "enterprise").mkdir()
    (tmp_path / "enterprise" / "configs.json").write_text(
        json.dumps([{"project_id": "secure", "enabled": True, "residency_mode": "local_only"}]), encoding="utf-8")
    with pytest.raises(DataPolicyError):
        enforce_data_policy("secure", "export", provider="github")
    enforce_data_policy("darkfac", "export")  # default unchanged


def test_hub_export_calls_enforcement(monkeypatch):
    from hub.backend import service as svc
    import core.enterprise.enforcement as enf

    calls = []
    monkeypatch.setattr(enf, "enforce_data_policy", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(svc.HubService, "list_services", lambda self: [])
    obj = object.__new__(svc.HubService)
    obj.export_services_data()
    assert calls == [("darkfac", "export")]

    def boom(*a, **k):
        raise DataPolicyError("x", "darkfac", "export", None, "blocked")

    monkeypatch.setattr(enf, "enforce_data_policy", boom)
    with pytest.raises(DataPolicyError):
        obj.export_services_data()
