"""USR-209: hooks do Claude Code e wrapper de sessao registram execucoes via CLI (falso)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "scripts" / "hooks" / "claude_execution_hook.py"
WRAPPER = REPO / "scripts" / "harness_session.py"

# One file per call: concurrent appends from several processes to one file interleave/lose lines on
# Windows (seen in CI 20/10/2026); the time_ns prefix keeps the call order when reading them back.
FAKE_CLI = """
import sys, os, time
log = os.environ["FAKE_CLI_LOG"]
with open(f"{log}.{time.time_ns():020d}.{os.getpid()}", "w", encoding="utf-8") as fh:
    fh.write(" ".join(sys.argv[1:]))
if sys.argv[1] == "start":
    print("exec-123")
"""


def _env(tmp_path: Path, monkeypatch, cli: Path | None = None) -> Path:
    log = tmp_path / "cli.log"
    fake = tmp_path / "fake_cli.py"
    fake.write_text(FAKE_CLI, encoding="utf-8")
    monkeypatch.setenv("DARKFAC_EXECUTION_CLI", str(cli or fake))
    monkeypatch.setenv("FAKE_CLI_LOG", str(log))
    monkeypatch.setenv("DARKFAC_EXEC_HOOK_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("DARKFAC_EXEC_HOOK_HEARTBEAT_S", "0.2")
    monkeypatch.setenv("DARKFAC_TICKET_ID", "usr-209")
    return log


def _lines(log: Path) -> list[str]:
    files = sorted(log.parent.glob(f"{log.name}.*"), key=lambda p: p.name)
    lines = [p.read_text(encoding="utf-8").strip() for p in files]
    return [line for line in lines if line]  # a file caught between open() and write() is still empty


def _wait(log: Path, predicate, timeout: float = 15.0) -> list[str]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        lines = _lines(log)
        if predicate(lines):
            return lines
        time.sleep(0.1)
    return _lines(log)


def _hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=10
    )


def test_hook_registers_session_and_subagent_then_finishes(tmp_path, monkeypatch):
    log = _env(tmp_path, monkeypatch)
    assert _hook({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(tmp_path)}).returncode == 0
    lines = _wait(log, lambda ls: any(x.startswith("start") for x in ls))
    assert any("--source claude_session" in x and "--harness claude" in x and "--ticket USR-209" in x for x in lines)
    _wait(log, lambda ls: (tmp_path / "state" / "ses-s1.id").exists())

    assert _hook({"hook_event_name": "SubagentStart", "session_id": "s1", "agent_id": "a1"}).returncode == 0
    lines = _wait(log, lambda ls: any("--source subagent" in x for x in ls))
    assert any("--source subagent" in x and "--parent exec-123" in x for x in lines)

    assert _hook({"hook_event_name": "SubagentStop", "session_id": "s1", "agent_id": "a1"}).returncode == 0
    assert _hook({"hook_event_name": "SessionEnd", "session_id": "s1", "reason": "other"}).returncode == 0
    lines = _wait(log, lambda ls: sum(x.startswith("finish") for x in ls) >= 2)
    finishes = [x for x in lines if x.startswith("finish")]
    assert len(finishes) >= 2
    assert any("--status cancelled" in x for x in finishes)
    assert any(x.startswith("heartbeat exec-123") for x in lines)


def test_hook_is_fast_and_never_fails(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, cli=tmp_path / "missing_cli.py")
    start = time.monotonic()
    proc = _hook({"hook_event_name": "SessionStart", "session_id": "s2"})
    assert proc.returncode == 0
    assert time.monotonic() - start < 5
    assert subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True).returncode == 0
    assert subprocess.run([sys.executable, str(HOOK)], input="", capture_output=True, text=True).returncode == 0


def test_wrapper_registers_start_heartbeat_finish_and_keeps_exit_code(tmp_path, monkeypatch):
    log = _env(tmp_path, monkeypatch)
    code = "import time,sys; time.sleep(1.5); sys.exit(3)"
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), "codex", "--", sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 3
    lines = _lines(log)
    assert lines[0].startswith("start --source codex_session --harness codex --stage interactive_session")
    assert "--ticket USR-209" in lines[0]
    assert any(x.startswith("heartbeat exec-123") for x in lines)
    assert lines[-1].startswith("finish exec-123 --status failed")


def test_wrapper_survives_missing_cli(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, cli=tmp_path / "missing_cli.py")
    proc = subprocess.run(
        [sys.executable, str(WRAPPER), "grok", "--", sys.executable, "-c", "pass"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0


def test_wrapper_requires_command(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    proc = subprocess.run([sys.executable, str(WRAPPER), "grok"], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 2


def test_settings_json_declares_only_existing_events_and_keeps_permissions():
    data = json.loads((REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "permissions" in data
    assert {"SessionStart", "SessionEnd", "SubagentStart", "SubagentStop"} <= set(data["hooks"])
    for event in ("SessionStart", "SessionEnd", "SubagentStart", "SubagentStop"):
        assert "claude_execution_hook.py" in data["hooks"][event][0]["hooks"][0]["command"]
    spec = importlib.util.spec_from_file_location("usr209_hook", HOOK)
    assert spec is not None
