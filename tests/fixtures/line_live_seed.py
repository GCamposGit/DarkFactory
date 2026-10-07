"""Realistic control-store scenario for the live line board (USR-138).

``seed_line_live_demo(path, now)`` creates (through the real ``SQLiteControlStore``
schema) a control database with eight runs across two projects:

========================  ==========================  ========================================
key                       project                     situation
========================  ==========================  ========================================
``dev_live``              darkfac                     development running for 12 min, active claim
``val_retry``             darkfac                     validation iteration 1 running after a retry
``grill_wait``            darkfac-canary              grill waiting for the owner (waiting_human)
``stalled``               darkfac-canary              development running past its timeout
``done``                  darkfac                     finished 3 h ago through all eight stages
``done_old``              darkfac                     finished ~26 h ago (outside the 24 h KPIs)
``failed``                darkfac                     integration failed, run closed 5 h ago
``queued``                darkfac-canary              grill pending, waiting for a worker
========================  ==========================  ========================================

Seeing the page locally
-----------------------
Hub reads ``<state root>/control.db`` unless a control database URL is configured, so seed a
scratch directory and point the Hub at it (PowerShell, from the repository root)::

    python -m tests.fixtures.line_live_seed C:\\temp\\line_demo
    Remove-Item Env:DARKHUB_CONTROL_DATABASE_URL, Env:DARKFAC_HF02_DATABASE_URL -ErrorAction SilentlyContinue
    $env:DARKFAC_STATE_ROOT = "C:\\temp\\line_demo"
    python run_hub.py            # then open http://127.0.0.1:8888/live

The CLI prints the database path and re-seeds relative to the current clock every time it runs
(run it again to refresh the timestamps; the file is recreated).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.workflow.control_store import SQLiteControlStore

ALL_STAGES: tuple[str, ...] = (
    "grill",
    "planning",
    "development",
    "validation",
    "independent_review",
    "integration",
    "build_deploy",
    "retrospective",
)
_ROLES: dict[str, str] = {
    "grill": "grill_engine",
    "planning": "planner",
    "development": "developer",
    "validation": "validator",
    "independent_review": "reviewer",
    "integration": "integrator",
    "build_deploy": "release_manager",
    "retrospective": "retrospective_agent",
}


class _Seeder:
    def __init__(self, store: SQLiteControlStore, now: datetime) -> None:
        self.conn = store._connect()
        self.now = now
        self._lease = 0
        self._external = 0

    def at(self, minutes_ago: float) -> str:
        return (self.now - timedelta(minutes=minutes_ago)).isoformat()

    def run(
        self,
        run_id: str,
        *,
        project_id: str,
        demand_id: str,
        title: str,
        created_min: float,
        status: str = "active",
        completed_min: float | None = None,
        updated_min: float | None = None,
    ) -> None:
        updated = updated_min if updated_min is not None else (completed_min if completed_min is not None else 1)
        self.conn.execute(
            "INSERT INTO runs (run_id, project_id, demand_id, demand_version, runtime_owner, mode, status,"
            " plan_digest, config_version, created_at, updated_at, completed_at)"
            " VALUES (?, ?, ?, 'v1', 'cloud_dbos_postgres', 'autonomous', ?, 'digest', 'cfg', ?, ?, ?)",
            (
                run_id,
                project_id,
                demand_id,
                status,
                self.at(created_min),
                self.at(updated),
                self.at(completed_min) if completed_min is not None else None,
            ),
        )
        self._external += 1
        self.conn.execute(
            "INSERT INTO intake_commands (channel, external_id, project_id, payload_digest, payload, mode,"
            " policy_ref, demand_id, demand_version, run_id, initial_job_id, committed_at)"
            " VALUES ('cli', ?, ?, 'digest', ?, 'autonomous', 'policy-v1', ?, 'v1', ?, 'job-0', ?)",
            (
                f"seed-{self._external}",
                project_id,
                json.dumps({"title": title}),
                demand_id,
                run_id,
                self.at(created_min),
            ),
        )

    def job(
        self,
        run_id: str,
        ticket_id: str,
        stage: str,
        *,
        status: str,
        started_min: float | None = None,
        finished_min: float | None = None,
        updated_min: float | None = None,
        iteration: int = 0,
        cost: float = 0.0,
        cause: str | None = None,
        retry_count: int = 0,
        timeout: int = 1800,
        evidence: list[str] | None = None,
        claim: tuple[str, str, float] | None = None,
    ) -> None:
        """Insert one job attempt; ``claim`` is ``(owner, route_ref, lease_expires_in_minutes)``."""
        reference = started_min if started_min is not None else (updated_min if updated_min is not None else 0)
        updated = updated_min if updated_min is not None else (
            finished_min if finished_min is not None else reference
        )
        self.conn.execute(
            "INSERT INTO jobs (run_id, ticket_id, plan_version, stage, iteration, status, role, timeout_seconds,"
            " retry_count, max_retries, cause_code, actual_cost, evidence_refs, created_at, updated_at,"
            " started_at, finished_at, ready_at)"
            " VALUES (?, ?, '1.0', ?, ?, ?, ?, ?, ?, 3, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                ticket_id,
                stage,
                iteration,
                status,
                _ROLES.get(stage, "worker"),
                timeout,
                retry_count,
                cause,
                cost,
                json.dumps(evidence or [f"artifact://{run_id}/{stage}/{iteration}"]),
                self.at(reference),
                self.at(updated),
                self.at(started_min) if started_min is not None else None,
                self.at(finished_min) if finished_min is not None else None,
                self.at(reference),
            ),
        )
        if claim is None:
            return
        owner, route, lease_in = claim
        self._lease += 1
        self.conn.execute(
            "INSERT INTO claims (lease_id, run_id, ticket_id, plan_version, stage, iteration, owner,"
            " fencing_token, reservation_id, route_ref, acquired_at, expires_at, status)"
            " VALUES (?, ?, ?, '1.0', ?, ?, ?, 1, ?, ?, ?, ?, 'active')",
            (
                f"lease-{self._lease}",
                run_id,
                ticket_id,
                stage,
                iteration,
                owner,
                f"res-{self._lease}",
                route,
                self.at(started_min if started_min is not None else 0),
                (self.now + timedelta(minutes=lease_in)).isoformat(),
            ),
        )

    def succeeded_until(
        self,
        run_id: str,
        ticket_id: str,
        stages: tuple[str, ...],
        *,
        first_start_min: float,
        step_min: float,
        cost: float,
    ) -> float:
        """Insert succeeded jobs back to back; returns the minute mark where the chain ended."""
        cursor = first_start_min
        for stage in stages:
            self.job(
                run_id,
                ticket_id,
                stage,
                status="succeeded",
                started_min=cursor,
                finished_min=cursor - step_min,
                cost=cost,
            )
            cursor -= step_min
        return cursor

    def commit(self) -> None:
        self.conn.commit()
        self.conn.close()


def seed_line_live_demo(path: Path, now: datetime) -> dict[str, str]:
    """Create ``path`` (a ``control.db``) with the demo scenario; returns ``{key: run_id}``."""
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    store = SQLiteControlStore(db_path=path)
    seeder = _Seeder(store, now)
    ids: dict[str, str] = {
        key: f"run-{key.replace('_', '-')}"
        for key in ("dev_live", "val_retry", "grill_wait", "stalled", "done", "done_old", "failed", "queued")
    }

    # 1. development running for 12 min with an active claim.
    rid = ids["dev_live"]
    seeder.run(rid, project_id="darkfac", demand_id="USR-138", title="Esteira ao vivo no DarkHub", created_min=50)
    seeder.succeeded_until(rid, "darkfac", ("grill", "planning"), first_start_min=50, step_min=14, cost=0.42)
    seeder.job(
        rid,
        "darkfac",
        "development",
        status="running",
        started_min=12,
        updated_min=1,
        cost=0.91,
        timeout=3600,
        claim=("cloud-worker-1", "anthropic:claude-sonnet-5-5", 3),
    )

    # 2. validation retried once (iteration 1 running).
    rid = ids["val_retry"]
    seeder.run(rid, project_id="darkfac", demand_id="USR-139", title="Backoff do pump de linha", created_min=95)
    seeder.succeeded_until(
        rid, "darkfac", ("grill", "planning", "development"), first_start_min=95, step_min=20, cost=0.6
    )
    seeder.job(
        rid,
        "darkfac",
        "validation",
        status="retry",
        iteration=0,
        started_min=35,
        finished_min=29,
        updated_min=29,
        cost=0.05,
        cause="tests_red",
        retry_count=1,
    )
    seeder.job(
        rid,
        "darkfac",
        "validation",
        status="running",
        iteration=1,
        started_min=6,
        updated_min=1,
        cost=0.04,
        retry_count=1,
        claim=("cloud-worker-2", "openai:gpt-5-codex", 4),
    )

    # 3. grill waiting for the owner.
    rid = ids["grill_wait"]
    seeder.run(
        rid,
        project_id="darkfac-canary",
        demand_id="CAN-07",
        title="Renovar certificados do canary",
        created_min=40,
        updated_min=25,
    )
    seeder.job(
        rid,
        "darkfac-canary",
        "grill",
        status="waiting_human",
        started_min=40,
        updated_min=25,
        cost=0.08,
        cause="grill_pending",
    )

    # 4. development running well past its timeout, lease expired.
    rid = ids["stalled"]
    seeder.run(
        rid,
        project_id="darkfac-canary",
        demand_id="CAN-05",
        title="Migrar fila de notificações",
        created_min=150,
        updated_min=70,
    )
    seeder.succeeded_until(
        rid, "darkfac-canary", ("grill", "planning"), first_start_min=150, step_min=25, cost=0.3
    )
    seeder.job(
        rid,
        "darkfac-canary",
        "development",
        status="running",
        started_min=95,
        updated_min=70,
        cost=1.4,
        timeout=1800,
        claim=("cloud-worker-3", "xai:grok-build", -65),
    )

    # 5. finished 3 h ago through every stage.
    rid = ids["done"]
    seeder.run(
        rid,
        project_id="darkfac",
        demand_id="USR-130",
        title="Painel de cotas por provedor",
        created_min=300,
        status="completed",
        completed_min=180,
    )
    seeder.succeeded_until(rid, "darkfac", ALL_STAGES, first_start_min=300, step_min=15, cost=0.55)

    # 6. finished ~26 h ago (throughput history, outside the 24 h window).
    rid = ids["done_old"]
    seeder.run(
        rid,
        project_id="darkfac",
        demand_id="USR-120",
        title="Relatório semanal de custos",
        created_min=26 * 60 + 150,
        status="completed",
        completed_min=26 * 60,
    )
    seeder.succeeded_until(
        rid, "darkfac", ALL_STAGES, first_start_min=26 * 60 + 150, step_min=18.75, cost=0.35
    )

    # 7. integration failed; the run was closed.
    rid = ids["failed"]
    seeder.run(
        rid,
        project_id="darkfac",
        demand_id="USR-131",
        title="Refatorar roteador de harness",
        created_min=390,
        status="completed",
        completed_min=300,
    )
    seeder.succeeded_until(
        rid,
        "darkfac",
        ("grill", "planning", "development", "validation", "independent_review"),
        first_start_min=390,
        step_min=15,
        cost=0.5,
    )
    seeder.job(
        rid,
        "darkfac",
        "integration",
        status="failed",
        started_min=315,
        finished_min=300,
        cost=0.2,
        cause="merge_conflict",
        retry_count=3,
    )

    # 8. waiting for a worker.
    rid = ids["queued"]
    seeder.run(
        rid,
        project_id="darkfac-canary",
        demand_id="CAN-08",
        title="Auditar permissões do canary",
        created_min=2,
        updated_min=2,
    )
    seeder.job(rid, "darkfac-canary", "grill", status="pending", started_min=None, updated_min=2)

    seeder.commit()
    return ids


_LOCAL_HOST = "DESKTOP-TEST"

# (minutes after the run started, phase, status, message, cause, harness)
_LocalStep = tuple[float, str, str, str, str | None, str | None]


def _emit_local_run(
    db_path: Path,
    now: datetime,
    *,
    run_id: str,
    ticket_id: str,
    title: str,
    started_min_ago: float,
    steps: list[_LocalStep],
    finish: tuple[float, bool, str | None] | None = None,
) -> None:
    """Publish a scripted ``run_ticket`` run through the real publisher and the real SQLite sink."""
    from core.line.local_progress import ProgressPublisher, SqliteSink

    start = now - timedelta(minutes=started_min_ago)
    current = [start]
    publisher = ProgressPublisher(
        ticket_id=ticket_id,
        project_id="darkfac",
        title=title,
        run_id=run_id,
        sink=SqliteSink(db_path),
        worker=_LOCAL_HOST,
        clock=lambda: current[0],
    )
    for minutes, phase, status, message, cause, harness in steps:
        current[0] = start + timedelta(minutes=minutes)
        publisher.phase(phase, status, message, cause=cause, harness=harness)  # type: ignore[arg-type]
    if finish is not None:
        current[0] = start + timedelta(minutes=finish[0])
        if finish[1]:
            publisher.finish(True)
        else:
            publisher.set_outcome(finish[2] or "falhou", "delivery_failed")
            publisher.finish(False)


_LOCAL_DONE_STEPS: list[_LocalStep] = [
    (0, "preflight", "running", "cota e roteamento", None, None),
    (1, "preflight", "succeeded", "rota codex", None, "codex"),
    (1, "workspace", "running", "criando worktree", None, None),
    (2, "workspace", "succeeded", "worktree pronta", None, None),
    (2, "agent", "running", "desenvolvimento via codex", None, "codex"),
    (20, "agent", "succeeded", "3 arquivo(s) alterado(s)", None, "codex"),
    (20, "gate", "running", "portao oficial", None, None),
    (26, "gate", "succeeded", "portao oficial aprovado", None, None),
    (26, "commit", "running", "commit", None, None),
    (27, "commit", "succeeded", "deadbeef", None, None),
    (27, "pr", "running", "push e PR", None, None),
    (28, "pr", "succeeded", "https://github.com/x/y/pull/9", None, None),
    (28, "ci", "running", "aguardando checks", None, None),
    (36, "ci", "succeeded", "ci_gate=green", None, None),
    (36, "merge", "running", "squash merge", None, None),
    (37, "merge", "succeeded", "merge cafe", None, None),
    (37, "deploy", "skipped", "deploy fora do run_ticket", None, None),
]


def seed_local_runs(db_path: Path, now: datetime) -> dict[str, str]:
    """Add four ``run_ticket`` runs (USR-140) to an already seeded control database.

    ==================  =============================================================
    ``local_live``      agent running for 12 min on claude (preflight/workspace done)
    ``local_done``      every phase succeeded 2 h ago, deploy skipped, run closed
    ``local_gate_red``  the official gate failed 3 h ago, run closed as failed
    ``local_abandoned`` agent started 9 h ago and the process never reported again
    ==================  =============================================================
    """
    ids = {
        "local_live": "local-USR-140-live",
        "local_done": "local-USR-141-done",
        "local_gate_red": "local-USR-142-gate",
        "local_abandoned": "local-USR-143-gone",
    }
    _emit_local_run(
        db_path, now, run_id=ids["local_live"], ticket_id="USR-140", title="Esteira ao vivo: run_ticket local",
        started_min_ago=15,
        steps=[
            (0, "preflight", "running", "cota e roteamento", None, None),
            (1, "preflight", "succeeded", "rota claude", None, "claude"),
            (1, "workspace", "running", "criando worktree", None, None),
            (3, "workspace", "succeeded", "usr-140-1 (ticket/usr-140@abc123def456)", None, None),
            (3, "agent", "running", "desenvolvimento via claude", None, "claude"),
        ],
    )
    _emit_local_run(
        db_path, now, run_id=ids["local_done"], ticket_id="USR-141", title="Ticket local entregue",
        started_min_ago=120 + 38, steps=_LOCAL_DONE_STEPS, finish=(38, True, None),
    )
    _emit_local_run(
        db_path, now, run_id=ids["local_gate_red"], ticket_id="USR-142", title="Ticket local com portao vermelho",
        started_min_ago=180,
        steps=[
            (0, "preflight", "succeeded", "rota grok", None, "grok"),
            (1, "workspace", "succeeded", "worktree pronta", None, None),
            (1, "agent", "succeeded", "1 arquivo(s) alterado(s)", None, "grok"),
            (10, "gate", "running", "portao oficial", None, None),
            (14, "gate", "failed", "portao oficial falhou (codigo 1)", "gate_failed", None),
        ],
        finish=(14, False, "entrega falhou: gate_failed"),
    )
    _emit_local_run(
        db_path, now, run_id=ids["local_abandoned"], ticket_id="USR-143", title="Ticket local abandonado",
        started_min_ago=540,
        steps=[
            (0, "preflight", "succeeded", "rota claude", None, "claude"),
            (1, "workspace", "succeeded", "worktree pronta", None, None),
            (1, "agent", "running", "desenvolvimento via claude", None, "claude"),
        ],
    )
    return ids


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m tests.fixtures.line_live_seed <state_dir>")
        return 2
    directory = Path(argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    db_path = directory / "control.db"
    for suffix in ("", "-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    now = datetime.now(timezone.utc)
    seed_line_live_demo(db_path, now)
    seed_local_runs(db_path, now)  # four run_ticket runs (USR-140) on top of the eight line runs
    print(f"seeded {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

