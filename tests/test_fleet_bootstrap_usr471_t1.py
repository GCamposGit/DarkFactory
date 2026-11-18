from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from core.line import fleet_bootstrap
from core.line import operating_harness


def _fleet_roots(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "project"
    line_dir = project_root / "core" / "line"
    line_dir.mkdir(parents=True)
    (line_dir / "fleet_bootstrap.py").write_text("", encoding="utf-8")
    (line_dir / "fleet_dispatcher.py").write_text("", encoding="utf-8")
    state_root = tmp_path / "state"
    state_root.mkdir()
    return project_root, state_root


def test_fleet_bootstrap_requires_explicit_roots() -> None:
    with pytest.raises(SystemExit):
        fleet_bootstrap.main(["--loop"])


def test_fleet_bootstrap_declares_headless_identity_and_explicit_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    project_root, state_root = _fleet_roots(tmp_path)
    env = {
        "DARKFAC_OPERATING_HARNESS": "antigravity",
        "ANTIGRAVITY_AGENT": "1",
        "CODEX_THREAD_ID": "interactive-session",
        "DARKFAC_PROCESS_HARNESS": "grok",
        "DARKFAC_EXECUTION_CONTEXT": "interactive",
        "PROJECT_ROOT": str(tmp_path / "wrong-project"),
        "STATE_ROOT": str(tmp_path / "wrong-state"),
        "OPENROUTER_API_KEY": "test-secret-never-log",
    }
    monkeypatch.setattr(operating_harness.os, "environ", env)
    monkeypatch.setattr(fleet_bootstrap.runpy, "run_module", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["fleet_bootstrap"])

    assert fleet_bootstrap.main([
        "--project-root", str(project_root), "--state-root", str(state_root), "--loop",
    ]) == 0

    assert env["DARKFAC_EXECUTION_CONTEXT"] == "headless"
    assert env["DARKFAC_OPERATING_HARNESS"] == ""
    assert env["DARKFAC_PROCESS_HARNESS"] == "none"
    assert env["DARKFAC_PROJECT_ROOT"] == str(project_root.resolve())
    assert env["DARKFAC_STATE_ROOT"] == str(state_root.resolve())
    assert env["PROJECT_ROOT"] == str(project_root.resolve())
    assert env["STATE_ROOT"] == str(state_root.resolve())
    assert not any(name in env for name in ("ANTIGRAVITY_AGENT", "CODEX_THREAD_ID"))
    output = capsys.readouterr().out.strip()
    record = json.loads(output)
    assert record["event"] == "dispatcher_process_identity"
    assert record["harness"] is None
    assert record["origin"] == "headless"
    assert record["process_harness"] is None
    assert record["process_harness_origin"] == "declared_none"
    assert record["model"] is None
    assert record["mode"] == "write"
    assert record["execution_context"] == "headless"
    assert record["write_capable"] is True
    assert record["project_root"] == str(project_root.resolve())
    assert record["state_root"] == str(state_root.resolve())
    assert "test-secret-never-log" not in output
