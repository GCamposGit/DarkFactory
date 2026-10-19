"""USR-313: protected-file publication OA runs the full gate before the push and checks main CI after."""

from __future__ import annotations

import io
from pathlib import Path

from core.owner_actions import cli
from core.owner_actions.protected_publish import (
    CI_LIST_COMMAND,
    FAST_GUARD_TESTS,
    GATE_COMMAND,
    GATE_PASS_MARKER,
    build_protected_publish_steps,
)
from core.owner_actions.store import OwnerActionStore

ROOT = Path(__file__).resolve().parents[1]


def _index(steps, needle: str) -> int:
    for i, step in enumerate(steps):
        if needle in (step.command or ""):
            return i
    raise AssertionError(f"{needle!r} ausente")


def test_gate_precedes_push_and_ci_check_follows() -> None:
    steps = build_protected_publish_steps("USR-999", ["core/harness/runner.py"])
    gate, push = _index(steps, GATE_COMMAND), _index(steps, "git push origin HEAD:main")
    guards, ci = _index(steps, "pytest"), _index(steps, CI_LIST_COMMAND)
    assert guards < gate < push < ci
    assert GATE_PASS_MARKER in steps[gate].text
    assert "gh run watch" in steps[ci].command
    assert "hotfix" in steps[ci].text
    for test in FAST_GUARD_TESTS:
        assert test in steps[guards].command
        assert (ROOT / test).is_file()
    assert "--quick" in GATE_COMMAND and "focados" in steps[gate].text


def test_cli_registers_protected_publish_action(tmp_path: Path) -> None:
    store = OwnerActionStore(tmp_path / "oa.json", overlay_path=tmp_path / "res.json")
    out = io.StringIO()
    code = cli.run(["protected-publish", "USR-999", "--files", "core/harness/x.py", "--no-notify"], store=store, out=out)
    assert code == 0
    action = store.get("OA-001")
    assert action.blocks == ["USR-999"]
    assert [s.command for s in action.steps if s.command and "git push" in s.command]
