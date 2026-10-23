"""USR-350: guard must reject cleanly (no traceback) when authorization deps are missing."""
from __future__ import annotations

import builtins

from core.orchestrator import guard


def test_missing_dependency_is_unauthorized_without_traceback(monkeypatch, capsys) -> None:
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "core.governance.authorization":
            raise ModuleNotFoundError("No module named 'pydantic'", name="pydantic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert guard.is_governance_evolution_authorized(["AGENTS.md"]) is False
    err = capsys.readouterr().err
    assert "missing dependency: pydantic" in err
    assert "treating as unauthorized" in err
    assert "Traceback" not in err
