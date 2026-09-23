"""Tests for core.line.human (HF-27-08 item E / review item 11(e)).

No network, no real git remote beyond a local `git init --bare` origin, no
real Telegram, no real agent CLI (grill's own agent seam is faked exactly
like tests/line/test_stage_grill.py does).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.line import human, stage_grill
from core.line.stage_grill import run_grill
from core.projects.models import ProjectDescriptor
from core.workflow.control_contracts import IntakeCommand, JobKey
from core.workflow.control_store import SQLiteControlStore


def _win_kwargs() -> dict:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs


def _git(args: list[str], cwd: Path) -> "subprocess.CompletedProcess[str]":
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", **_win_kwargs(),
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc


def _init_bare_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", str(origin)], cwd=tmp_path)
    seed = tmp_path / "_seed"
    _git(["clone", str(origin), str(seed)], cwd=tmp_path)
    _git(["checkout", "-B", "main"], cwd=seed)
    _git(["config", "user.email", "seed@example.com"], cwd=seed)
    _git(["config", "user.name", "Seed"], cwd=seed)
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=seed)
    _git(["commit", "-m", "seed commit"], cwd=seed)
    _git(["push", "origin", "main"], cwd=seed)
    return origin


def _seed_run_with_sibling(store: SQLiteControlStore, run_id: str, project_id: str, now: datetime) -> None:
    """Seed the run row plus an unrelated in-flight sibling job for `run_id`."""
    now_iso = now.isoformat()
    conn = store._connect()
    conn.execute(
        "INSERT OR IGNORE INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status, "
        "plan_digest, config_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, project_id, "dem", "1.0", "hf05_sqlite", "autonomous", "active", "d", "1.0", now_iso, now_iso),
    )
    conn.execute(
        "INSERT INTO jobs (run_id,ticket_id,plan_version,stage,iteration,status,role,required_capabilities,"
        "fencing_token,timeout_seconds,retry_count,max_retries,actual_cost,output_refs,evidence_refs,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,0,1800,0,3,0.0,'[]','[]',?,?)",
        (run_id, project_id, "1.0", "integration", 0, "running", "integrator", "[]", now_iso, now_iso),
    )
    conn.commit()
    conn.close()


def _job_status(store: SQLiteControlStore, run_id: str, stage: str) -> str:
    conn = store._connect()
    cur = conn.cursor()
    cur.execute("SELECT status FROM jobs WHERE run_id = ? AND stage = ?", (run_id, stage))
    row = cur.fetchone()
    conn.close()
    assert row is not None
    return row["status"]


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(stage_grill, "default_telegram_sender", lambda: None)


def test_grill_callback_resumes_only_grill_job_sibling_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin = _init_bare_origin(tmp_path)
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "workspaces"))
    project = ProjectDescriptor(id="acme", name="Acme Project", repo_url=str(origin))

    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    now = datetime(2026, 9, 23, tzinfo=UTC)

    cmd = IntakeCommand(
        project_id="acme", channel="test", external_id="human-grill-1", mode="autonomous",
        policy_ref="darkfac://line/v1",
        payload={"title": "t", "problem": "p", "journey": "j", "non_goals": [], "criteria": []},
    )
    receipt = store.accept(cmd, now)
    run_id = receipt.run_id
    assert run_id is not None

    # Claim + run the real grill stage with a blocking business question, so
    # it reaches waiting_human exactly like the production line does.
    reply_json = (
        '{"questions": [{"id": "q1", "text": "Cobrar assinatura?", "kind": "business", '
        '"options": ["sim", "nao"], "recommended": "nao", "why": "MVP"}], '
        '"assumptions": [], "is_product_scale": false}'
    )
    from core.line.agent_cli import AgentResult

    monkeypatch.setattr(
        stage_grill, "run_read_agent",
        lambda *a, **k: AgentResult(ok=True, text=reply_json, harness="claude", duration_s=0.01),
    )

    claim = store.claim("w1", ["grill_engine"], now)
    assert claim is not None
    result = run_grill(project, run_id, "Nova feature de billing", now=now)
    assert result.outcome == "waiting_human"
    store.finish(claim, result, now)

    # Sibling: an unrelated job for the same run, in flight, that must never
    # be touched by resuming the grill job.
    _seed_run_with_sibling(store, run_id, "acme", now)
    assert _job_status(store, run_id, "grill") == "waiting_human"
    assert _job_status(store, run_id, "integration") == "running"

    handler = human.build_telegram_line_grill_handler(store, project_resolver=lambda pid: project if pid == "acme" else None)
    outcome = handler(run_id, "q1", "0", 12345)

    assert outcome["resumed"] is True
    assert _job_status(store, run_id, "grill") == "pending"
    # Sibling untouched.
    assert _job_status(store, run_id, "integration") == "running"


def test_accept_callback_resumes_only_build_deploy_job_sibling_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DARKFAC_WORKSPACES", str(tmp_path / "workspaces"))
    project = ProjectDescriptor(id="acme", name="Acme Project", requires_commercial_acceptance=True)

    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    now = datetime(2026, 9, 23, tzinfo=UTC)
    run_id = "run-accept-1"

    conn = store._connect()
    conn.execute(
        "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status, "
        "plan_digest, config_version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, "acme", "dem", "1.0", "hf05_sqlite", "autonomous", "active", "d", "1.0", now.isoformat(), now.isoformat()),
    )
    # The waiting build_deploy job -- output_refs are irrelevant here since
    # commercial acceptance reads the SHA from get_latest_success_output_refs
    # (the predecessor integration job's output_refs), matching stage_release's
    # own input_refs contract ([pr_url, merge_sha]).
    conn.execute(
        "INSERT INTO jobs (run_id,ticket_id,plan_version,stage,iteration,status,role,required_capabilities,"
        "fencing_token,timeout_seconds,retry_count,max_retries,actual_cost,output_refs,evidence_refs,created_at,updated_at,finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,0,1800,0,3,0.0,'[]','[]',?,?,NULL)",
        (run_id, "acme", "1.0", "build_deploy", 0, "waiting_human", "deployer", "[]", now.isoformat(), now.isoformat()),
    )
    integration_sha = "f" * 40
    conn.execute(
        "INSERT INTO jobs (run_id,ticket_id,plan_version,stage,iteration,status,role,required_capabilities,"
        "fencing_token,timeout_seconds,retry_count,max_retries,actual_cost,output_refs,evidence_refs,created_at,updated_at,finished_at) "
        "VALUES (?,?,?,?,?,?,?,?,0,1800,0,3,0.0,?,'[]',?,?,?)",
        (
            run_id, "acme", "1.0", "integration", 0, "succeeded", "integrator", "[]",
            f'["https://github.com/acme/repo/pull/9", "{integration_sha}"]',
            now.isoformat(), now.isoformat(), now.isoformat(),
        ),
    )
    # Sibling: an unrelated retrospective placeholder job for the same run.
    conn.execute(
        "INSERT INTO jobs (run_id,ticket_id,plan_version,stage,iteration,status,role,required_capabilities,"
        "fencing_token,timeout_seconds,retry_count,max_retries,actual_cost,output_refs,evidence_refs,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,0,1800,0,3,0.0,'[]','[]',?,?)",
        (run_id, "acme", "1.0", "grill", 0, "succeeded", "grill_engine", "[]", now.isoformat(), now.isoformat()),
    )
    conn.commit()
    conn.close()

    called_with = {}

    def _fake_record_commercial_acceptance(state_store, project_id, run_id_arg, sha):
        called_with["project_id"] = project_id
        called_with["run_id"] = run_id_arg
        called_with["sha"] = sha

    import core.line.stage_release as stage_release_mod

    monkeypatch.setattr(stage_release_mod, "record_commercial_acceptance", _fake_record_commercial_acceptance)
    monkeypatch.setattr(stage_release_mod, "default_state_store", lambda project: object())

    handler = human.build_telegram_commercial_acceptance_handler(
        store, project_resolver=lambda pid: project if pid == "acme" else None
    )
    outcome = handler(run_id, 12345)

    assert outcome["resumed"] is True
    assert outcome["sha"] == integration_sha
    assert called_with == {"project_id": "acme", "run_id": run_id, "sha": integration_sha}
    assert _job_status(store, run_id, "build_deploy") == "pending"
    # Siblings untouched.
    assert _job_status(store, run_id, "integration") == "succeeded"
    assert _job_status(store, run_id, "grill") == "succeeded"
