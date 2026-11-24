"""USR-483 T4: scripts/line_briefing.py consults and assumes from the shared registry."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "line_briefing.py"
SIGNAL_KEYS = (
    "DARKFAC_OPERATING_HARNESS",
    "DARKFAC_SESSION_ID",
    "DARKFAC_SESSION_MODEL",
    "DARKFAC_SESSION_HOST",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "CODEX_SESSION_ID",
    "CODEX_SANDBOX",
    "CODEX_SANDBOX_NETWORK_DISABLED",
    "CODEX_MODEL",
    "GROK_AGENT",
    "GROK_SESSION_ID",
    "GROK_CLI",
    "ANTIGRAVITY_AGENT",
    "ANTIGRAVITY_CONVERSATION_ID",
    "ANTIGRAVITY_SESSION",
    "ANTIGRAVITY_SESSION_ID",
    "ANTIGRAVITY_MODEL",
    "TERM_PROGRAM",
    "DARKFAC_HF02_DATABASE_URL",
    "DARKHUB_CONTROL_DATABASE_URL",
    "DARKHUB_LINE_DATABASE_URL",
)


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key in SIGNAL_KEYS or (key.startswith("DARKFAC_") and key.endswith("_USAGE_JSON")):
            env.pop(key, None)
    state = tmp_path / "state"
    provider_dir = state / "usage" / "providers"
    provider_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    fresh = {
        "status": "connected",
        "checked_at": now.isoformat().replace("+00:00", "Z"),
        "windows": [{"quota_id": "week", "label": "semanal", "remaining_percent": 80}],
    }
    stale = {
        "status": "connected",
        "checked_at": (now - timedelta(hours=155)).isoformat().replace("+00:00", "Z"),
        "windows": [{"quota_id": "week", "label": "semanal", "remaining_percent": 12}],
    }
    (provider_dir / "anthropic.json").write_text(json.dumps(fresh), encoding="utf-8")
    (provider_dir / "openai.json").write_text(json.dumps(stale), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "pr.json").write_text(
        json.dumps(
            [{
                "number": 7,
                "title": "PR de teste",
                "url": "https://example.test/pr/7",
                "updatedAt": now.isoformat().replace("+00:00", "Z"),
            }]
        ),
        encoding="utf-8",
    )
    (bin_dir / "gh.cmd").write_text('@echo off\r\ntype "%~dp0pr.json"\r\n', encoding="ascii")
    posix_gh = bin_dir / "gh"
    posix_gh.write_bytes(b'#!/bin/sh\ncat "$(dirname "$0")/pr.json"\n')
    posix_gh.chmod(0o755)
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["DARKFAC_STATE_ROOT"] = str(state)
    env["DARKFAC_SHARED_STATE_PATH"] = str(state / "shared.sqlite3")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.update(extra)
    return env


def _run(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO,
        env=env or _env(tmp_path),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )


def test_consult_reads_shared_providers_and_not_local_narrative(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    text = result.stdout
    for header in ("## Coordenador", "## Sessoes paralelas", "## Fila", "## Cotas", "## Runs vivos", "## PRs"):
        assert header in text
    assert "ausente causa=no_coordinator" in text
    assert "control_store_absent" in text
    assert "frescor=stale" in text
    assert "confirmado=nao" in text
    assert "PR de teste" in text
    assert "HANDOFF" not in text
    assert "FASE_ATUAL" not in text
    assert "Troca de harness ou PC operador" not in text

    structured = _run(tmp_path, "--json", env=_env(tmp_path, DARKFAC_SESSION_ID="sess-consult"))
    assert structured.returncode == 0, structured.stderr
    report = json.loads(structured.stdout)
    assert report["absence_reason"] == "no_coordinator"
    assert report["queue"]["cause"] == "control_store_absent"
    assert report["queue"]["source"] == "control_store"
    quotas = {item["harness"]: item for item in report["quotas"]["items"]}
    assert quotas["claude"]["freshness"] == "current"
    assert quotas["claude"]["confirmed"] is True
    assert quotas["codex"]["freshness"] == "stale"
    assert quotas["codex"]["confirmed"] is False
    assert report["pull_requests"]["items"][0]["number"] == 7
    assert report["live_runs"]["freshness"] == "unavailable"
    assert "observed_at" in report
    assert isinstance(report["parallel_sessions"], list)


def test_assumption_requires_the_owner_mark_and_then_swaps_immediately(tmp_path: Path) -> None:
    base = _env(
        tmp_path,
        DARKFAC_SESSION_ID="sess-codex",
        DARKFAC_OPERATING_HARNESS="antigravity",
        CODEX_THREAD_ID="thread-real",
        CODEX_MODEL="codex-test-model",
    )
    refused = _run(tmp_path, "assumir", "--json", env=base)
    assert refused.returncode == 2
    assert json.loads(refused.stdout)["cause"] == "owner_decision_required"

    still = _run(tmp_path, "--json", env=base)
    assert json.loads(still.stdout)["coordinator"] is None

    taken = _run(tmp_path, "assumir", "--decisao-owner", "--json", env=base)
    assert taken.returncode == 0, taken.stderr
    report = json.loads(taken.stdout)
    assert report["assumed"] is True
    assert report["coordinator"]["harness"] == "codex"
    assert report["coordinator"]["session_id"] == "sess-codex"
    assert report["coordinator"]["origin"] == "owner_decision"
    assert report["coordinator"]["model"] == "codex-test-model"
    assert report["coordinator"]["freshness"] == "current"
    lease_id = report["coordinator"]["lease_id"]

    other = _env(tmp_path, DARKFAC_SESSION_ID="sess-other", CLAUDECODE="1")
    parallel = _run(tmp_path, "--json", env=other)
    assert parallel.returncode == 0, parallel.stderr
    viewed = json.loads(parallel.stdout)
    assert viewed["coordinator"]["lease_id"] == lease_id
    assert viewed["caller"]["harness"] == "claude"
    assert viewed["caller"]["is_coordinator"] is False
    assert any(item["session_id"] == "sess-other" for item in viewed["parallel_sessions"])


def test_unknown_harness_consults_and_assumes_only_for_the_owner(tmp_path: Path) -> None:
    env = _env(tmp_path, DARKFAC_SESSION_ID="sess-unknown", DARKFAC_OPERATING_HARNESS="codex")
    consulted = _run(tmp_path, "--json", env=env)
    assert consulted.returncode == 0, consulted.stderr
    report = json.loads(consulted.stdout)
    assert report["caller"]["harness"] is None
    assert report["coordinator"] is None

    refused = _run(tmp_path, "assume", env=env)
    assert refused.returncode == 2
    assert "decisao do owner" in refused.stderr
    assert json.loads(_run(tmp_path, "--json", env=env).stdout)["coordinator"] is None

    taken = _run(tmp_path, "assume", "--decisao-owner", "--json", env=env)
    assert taken.returncode == 0, taken.stderr
    body = json.loads(taken.stdout)
    assert body["coordinator"]["harness"] == "unknown"
    assert body["coordinator"]["session_id"] == "sess-unknown"
    assert body["coordinator"]["origin"] == "owner_decision"


def test_two_invocations_of_one_claude_session_share_id_and_renew_the_lease(tmp_path: Path) -> None:
    env = _env(tmp_path, CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="claude-desktop")
    assumed = _run(tmp_path, "assumir", "--decisao-owner", "--json", env=env)
    assert assumed.returncode == 0, assumed.stderr
    first = json.loads(assumed.stdout)
    consulted = _run(tmp_path, "--json", env=env)
    assert consulted.returncode == 0, consulted.stderr
    second = json.loads(consulted.stdout)

    assert second["caller"]["session_id"] == first["caller"]["session_id"]
    assert second["caller"]["is_coordinator"] is True
    assert second["coordinator"]["expires_at"] >= first["coordinator"]["expires_at"]
