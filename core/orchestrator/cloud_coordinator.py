from __future__ import annotations

import os
import sys
import logging
import signal
import secrets
import threading
from datetime import UTC, datetime
from typing import Any, Callable, Sequence
from pydantic import BaseModel, ConfigDict, Field

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_db import probe_cloud_database, sanitize_database_url
from core.workflow.control_contracts import (
    FleetCapacityExceededError,
    FleetReservation,
    HostBusyError,
    IdempotencyConflict,
    IntakeCommand,
    IntakeReceipt,
    RuntimeOwner,
    TicketAlreadyActiveError,
    fleet_managed_enabled,
)
from core.workflow.reconciliation import reconcile_portfolio_state

logger = logging.getLogger("darkfac.cloud_coordinator")

DEFAULT_FLEET_HOSTS: tuple[str, ...] = ("grokbox", "notebook", "desktop")


class CoordinatorStatus(BaseModel):
    """Structured health and status of the cloud coordinator."""

    model_config = ConfigDict(frozen=True)

    role: str = "coordinator"
    application_version: str = "v1"
    database_status: str = Field(description="'ready', 'waiting_access', or 'error'")
    dbos_engine: str = Field(description="'active', 'absent', or 'offline'")
    max_concurrent_slots: int = 3
    active_workflows_count: int = 0
    active_fleet_reservations_count: int = 0
    fleet_hosts: list[str] = Field(default_factory=lambda: list(DEFAULT_FLEET_HOSTS))
    recovered_workflows_count: int = 0
    error_message: str | None = None
    http_port: int = 8001


class CloudCoordinator:
    """Headless cloud coordinator managing durable workflow lifecycle."""

    def __init__(
        self,
        database_url: str | None = None,
        max_concurrent_slots: int | None = None,
        application_version: str = "v1",
        port: int | None = None,
        store: Any | None = None,
        fleet_hosts: Sequence[str] | None = None,
        host_health_prober: Callable[[str], bool | None] | None = None,
        feeder_cycle: Any | None = None,
        capacity_probe: Callable[[], int] | None = None,
    ) -> None:
        self.database_url = database_url or os.environ.get("DARKFAC_HF02_DATABASE_URL")
        self.max_slots = (
            max_concurrent_slots
            if max_concurrent_slots is not None
            else int(os.environ.get("DARKFAC_MAX_CONCURRENT_SLOTS", "3"))
        )
        self.application_version = application_version
        self.port = port if port is not None else int(os.environ.get("DARKFAC_COORDINATOR_PORT", "8001"))
        self.host_health_prober = host_health_prober
        self.capacity_probe = capacity_probe
        if fleet_hosts is not None:
            self.fleet_hosts = list(fleet_hosts)
        else:
            raw_hosts = os.environ.get("DARKFAC_FLEET_HOSTS", "grokbox,notebook,desktop")
            self.fleet_hosts = [h.strip() for h in raw_hosts.split(",") if h.strip()]
        self._dbos_instance: Any = None
        self._running = False
        self._stop_event: threading.Event | None = None
        self._recovered_ids: list[str] = []
        self._store_instance: Any = store
        self._uvicorn_server: Any = None
        # USR-304: ledger -> line feeder, bounded by interval. Explicit cycle wins; otherwise built
        # lazily from env only against a real cloud database (never against an injected test store).
        self._feeder_cycle: Any = feeder_cycle
        self._feeder_resolved = feeder_cycle is not None

    def _resolve_feeder_cycle(self) -> Any:
        if not self._feeder_resolved:
            self._feeder_resolved = True
            if self.database_url:
                try:
                    from core.line.feeder_cycle import FeederCycle

                    self._feeder_cycle = FeederCycle.from_env(self.store, max_active=self.max_slots)
                except Exception as exc:
                    logger.warning("Backlog feeder cycle unavailable: %s", exc)
        return self._feeder_cycle

    @property
    def store(self) -> Any:
        if self._store_instance is None:
            self._store_instance = PostgresControlStore(
                database_url=self.database_url,
                runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value,
                lease_duration_sec=300,
            )
        return self._store_instance

    def inspect_status(self) -> CoordinatorStatus:
        """Inspect coordinator operational status without throwing exceptions."""
        if self._store_instance is not None:
            db_status = "ready"
            probe_err = None
        else:
            probe = probe_cloud_database(self.database_url)
            db_status = "ready" if probe.status == "ready" else "waiting_access"
            probe_err = probe.error_message

        dbos_available = False
        try:
            import dbos  # type: ignore[import-not-found]
            dbos_available = True
        except ImportError:
            pass

        engine_status = "active" if (dbos_available and db_status == "ready") else ("absent" if not dbos_available else "offline")

        active_reservations_count = 0
        if db_status == "ready" and hasattr(self.store, "list_active_reservations"):
            try:
                active_reservations_count = len(self.store.list_active_reservations())
            except Exception:
                active_reservations_count = 0

        return CoordinatorStatus(
            role="coordinator",
            application_version=self.application_version,
            database_status=db_status,
            dbos_engine=engine_status,
            max_concurrent_slots=self.max_slots,
            active_workflows_count=0,
            active_fleet_reservations_count=active_reservations_count,
            fleet_hosts=list(self.fleet_hosts),
            recovered_workflows_count=len(self._recovered_ids),
            error_message=probe_err if db_status != "ready" else None,
            http_port=self.port,
        )

    def _host_verified_healthy(self, host: str) -> bool:
        """USR-306: in managed mode only a host with verified health is eligible (no prober => unknown => no).

        A prober answering anything but True (False, None = unknown/stale) or raising is not eligible. Unmanaged
        (legacy) mode keeps presuming hosts available when no prober is configured.
        """
        if self.host_health_prober is None:
            return not fleet_managed_enabled()
        try:
            verdict = self.host_health_prober(host)
            return verdict is not None and bool(verdict)
        except Exception as exc:
            logger.warning("Host health probe failed for %s: %s", host, exc)
            return False

    def _local_host_has_capacity(self, host: str) -> bool:
        """USR-337: the local host is only admitted when measured gate capacity (RAM workers) allows it.

        Same rule as ``fleet_policy.select_host_for_ticket(verified_capacity=...)``: capacity <= 0 means no new
        cycle. Remote hosts are not measured here. Probe failure is fail-closed only in managed mode.
        """
        try:
            from core.line.line_lock import resolve_host_id

            if host.lower() != resolve_host_id().lower():
                return True
            if self.capacity_probe is not None:
                workers = int(self.capacity_probe())
            elif fleet_managed_enabled():
                from core.line.gate_ram import measure_workers

                workers = measure_workers().workers
            else:
                return True
        except Exception as exc:
            logger.warning("Capacity probe failed for %s: %s", host, exc)
            return not fleet_managed_enabled()
        if workers <= 0:
            logger.info("Host %s skipped: verified capacity %d", host, workers)
            return False
        return True

    def admit_eligible_cycles(self, now: datetime | None = None) -> list[FleetReservation]:
        """Admit eligible pending cycles to free fleet hosts (USR-282).

        Enforces:
        - At most 3 concurrent cycles across the fleet.
        - First healthy, free, and compatible host in order Grokbox, Notebook, Desktop.
        - Real availability check without fixed priority delays.
        - Human blocks (waiting_human) remain parked without exceeding ceiling.
        - Idempotent: tickets already reserved stay on their reserved host.
        """
        now_utc = now or datetime.now(UTC)

        if not hasattr(self.store, "list_active_reservations") or not hasattr(self.store, "reserve_cycle"):
            return []

        # 0. Keep reservations of runs between stages / waiting on CI alive (USR-306)
        if hasattr(self.store, "renew_open_run_reservations"):
            try:
                self.store.renew_open_run_reservations(now_utc)
            except Exception as exc:
                logger.warning("Fleet reservation renewal warning: %s", exc)

        # 1. Sweep expired reservations
        if hasattr(self.store, "reconcile_fleet_reservations"):
            try:
                self.store.reconcile_fleet_reservations(now_utc)
            except Exception as exc:
                logger.warning("Fleet reservation sweep warning: %s", exc)

        # 2. Check active reservations
        try:
            active_reservations = self.store.list_active_reservations()
        except Exception as exc:
            logger.warning("Failed to list active reservations: %s", exc)
            return []

        occupied_hosts = {r.host_id.lower() for r in active_reservations}
        active_tickets = {(r.project_id, r.ticket_id) for r in active_reservations}
        active_runs = {r.run_id for r in active_reservations}

        available_slots = self.max_slots - len(active_reservations)
        if available_slots <= 0:
            return []

        # 3. Determine free hosts in priority order (skipping occupied, draining, or offline hosts)
        free_hosts = [
            h for h in self.fleet_hosts
            if h.lower() not in occupied_hosts
            and self._host_verified_healthy(h)
            and self._local_host_has_capacity(h)
        ]
        if not free_hosts:
            return []

        # 4. Query pending jobs eligible for admission
        if not hasattr(self.store, "list_eligible_admission_jobs"):
            return []

        try:
            eligible_jobs = self.store.list_eligible_admission_jobs(limit=50)
        except Exception as exc:
            logger.warning("Failed to list eligible admission jobs: %s", exc)
            return []

        admitted: list[FleetReservation] = []

        for job in eligible_jobs:
            if not free_hosts or len(admitted) >= available_slots:
                break
            proj_id = job["project_id"]
            tick_id = job["ticket_id"]
            r_id = job["run_id"]

            if (proj_id, tick_id) in active_tickets or r_id in active_runs:
                continue

            reserved = False
            while free_hosts and len(admitted) < available_slots:
                host = free_hosts.pop(0)
                try:
                    reservation = self.store.reserve_cycle(
                        project_id=proj_id,
                        ticket_id=tick_id,
                        run_id=r_id,
                        host_id=host,
                        now=now_utc,
                    )
                    admitted.append(reservation)
                    active_tickets.add((proj_id, tick_id))
                    active_runs.add(r_id)
                    occupied_hosts.add(host.lower())
                    logger.info(
                        "Admitted ticket %s (run %s) on host %s (reservation %s)",
                        tick_id, r_id, host, reservation.reservation_id,
                    )
                    reserved = True
                    break
                except (HostBusyError, TicketAlreadyActiveError, FleetCapacityExceededError) as exc:
                    logger.debug("Could not reserve %s on %s: %s", tick_id, host, exc)
                    continue
                except Exception as exc:
                    logger.warning("Unexpected error reserving %s on %s: %s", tick_id, host, exc)
                    continue
            if not reserved and not free_hosts:
                break

        return admitted

    def scan_and_recover_pending(self) -> list[str]:
        """Scan system database for pending/interrupted workflows and queue recovery."""
        status = self.inspect_status()
        if status.database_status != "ready":
            logger.warning("Cannot recover workflows: database is in %s state", status.database_status)
            return []

        # 1. Sweep expired leases and orphan claims
        now_utc = datetime.now(UTC)
        try:
            recon_report = reconcile_portfolio_state(self.store, now_utc, limit=50)
            if recon_report.repaired_leases_count > 0:
                logger.info("Reconciled %d expired leases in portfolio state", recon_report.repaired_leases_count)
        except Exception as exc:
            logger.warning("Lease reconciliation scan warning: %s", exc)

        # 2. Materialize pending outbox events
        try:
            pending_outbox = self.store.get_pending_outbox(limit=50)
            for evt in pending_outbox:
                try:
                    self.store.materialize(evt, now_utc)
                except Exception as exc:
                    logger.warning("Outbox materialization warning for event %s: %s", evt.get("outbox_id"), exc)
        except Exception as exc:
            logger.warning("Outbox sweep warning: %s", exc)

        # 3. Sweep fleet reservations and reconcile unknown operations (USR-282)
        try:
            if hasattr(self.store, "reconcile_fleet_reservations"):
                self.store.reconcile_fleet_reservations(now_utc)
            if hasattr(self.store, "reconcile_unknown_operations"):
                self.store.reconcile_unknown_operations(now_utc)
        except Exception as exc:
            logger.warning("Fleet reconciliation scan warning: %s", exc)

        # 4. Admit eligible cycles to available fleet hosts (USR-282)
        try:
            admitted = self.admit_eligible_cycles(now_utc)
            if admitted:
                logger.info("Fleet admission admitted %d new cycle(s)", len(admitted))
        except Exception as exc:
            logger.warning("Fleet cycle admission scan warning: %s", exc)

        # 4b. Backlog feeder: import eligible ledger tickets into the line (USR-304, every 15 min)
        try:
            cycle = self._resolve_feeder_cycle()
            if cycle is not None:
                cycle.maybe_run(now=now_utc)
        except Exception as exc:
            logger.warning("Backlog feeder cycle warning: %s", exc)

        # 5. DBOS recovery if available
        recovered: list[str] = []
        try:
            import dbos  # type: ignore[import-not-found]
            self._recovered_ids = recovered
        except Exception as exc:
            logger.error("Error during workflow recovery scan: %s", exc)
        return self._recovered_ids

    def _supervision_loop(self, stop_event: threading.Event, poll_interval_sec: float) -> None:
        """Background supervision loop periodically scanning state and outbox."""
        while not stop_event.is_set():
            try:
                st = self.inspect_status()
                if st.database_status == "ready":
                    self.scan_and_recover_pending()
                else:
                    logger.warning(
                        "Database not ready (status=%s, error=%s); coordinator standing by",
                        st.database_status,
                        st.error_message,
                    )
            except Exception as exc:
                logger.warning("Transient error in coordinator supervision loop: %s", exc)
            stop_event.wait(timeout=poll_interval_sec)

    def run_forever(
        self,
        stop_event: threading.Event | None = None,
        poll_interval_sec: float = 5.0,
        enable_http: bool = True,
    ) -> int:
        """Run continuous supervision loop and optional background HTTP API ingress."""
        import uvicorn

        if stop_event is None:
            stop_event = threading.Event()
        self._stop_event = stop_event
        self._running = True

        def _signal_handler(signum: int, frame: Any) -> None:
            logger.info("Signal %s received; initiating coordinator shutdown", signum)
            stop_event.set()
            if self._uvicorn_server is not None:
                self._uvicorn_server.should_exit = True

        orig_sigint = None
        orig_sigterm = None
        try:
            orig_sigint = signal.signal(signal.SIGINT, _signal_handler)
        except (ValueError, AttributeError):
            pass

        try:
            orig_sigterm = signal.signal(signal.SIGTERM, _signal_handler)
        except (ValueError, AttributeError):
            pass

        # If HTTP ingress enabled, run uvicorn in a daemon thread
        if enable_http and self.port > 0:
            def _run_server() -> None:
                try:
                    app = self.create_app()
                    config = uvicorn.Config(
                        app=app,
                        host="0.0.0.0",
                        port=self.port,
                        log_level="warning",
                        access_log=False,
                    )
                    self._uvicorn_server = uvicorn.Server(config)
                    logger.info("Cloud coordinator HTTP API listening on 0.0.0.0:%d", self.port)
                    self._uvicorn_server.run()
                except Exception as exc:
                    logger.warning("Could not start HTTP server on port %d: %s", self.port, exc)

            http_thread = threading.Thread(
                target=_run_server,
                daemon=True,
                name="cloud-coordinator-http",
            )
            http_thread.start()

        logger.info(
            "Cloud coordinator started supervision loop (poll_interval=%.1fs)",
            poll_interval_sec,
        )
        try:
            self._supervision_loop(stop_event, poll_interval_sec)
        finally:
            self.shutdown()
            if orig_sigint is not None:
                try:
                    signal.signal(signal.SIGINT, orig_sigint)
                except (ValueError, AttributeError):
                    pass
            if orig_sigterm is not None:
                try:
                    signal.signal(signal.SIGTERM, orig_sigterm)
                except (ValueError, AttributeError):
                    pass
            logger.info("Cloud coordinator supervision loop terminated cleanly")
        return 0

    def create_app(self) -> Any:
        """Create FastAPI application for cloud coordinator health, ingress, and status."""
        from fastapi import Depends, FastAPI, Header, HTTPException

        def require_api_token(authorization: str | None = Header(default=None)) -> None:
            token = os.environ.get("DARKFAC_COORDINATOR_API_TOKEN", "")
            if len(token) < 32:
                raise HTTPException(status_code=503, detail="Coordinator API credentials are not configured")
            if authorization is None or not secrets.compare_digest(authorization, f"Bearer {token}"):
                raise HTTPException(
                    status_code=401,
                    detail="Unauthorized",
                    headers={"WWW-Authenticate": "Bearer"},
                )

        app = FastAPI(title="Dark Factory Cloud Coordinator", version=self.application_version)

        @app.get("/healthz")
        def get_health() -> dict[str, str]:
            """Expose process liveness only; this is not an activation oracle."""
            return {"status": "ok"}

        @app.get("/readyz", dependencies=[Depends(require_api_token)])
        def get_readiness() -> CoordinatorStatus:
            status = self.inspect_status()
            if status.database_status != "ready" or os.environ.get(
                "DARKFAC_INTAKE_ENABLED", "false"
            ).lower() != "true":
                raise HTTPException(status_code=503, detail="Coordinator is not ready for intake")
            return status

        @app.get("/status", dependencies=[Depends(require_api_token)])
        def get_status() -> CoordinatorStatus:
            return self.inspect_status()

        @app.post("/api/v1/tasks", status_code=202, dependencies=[Depends(require_api_token)])
        def submit_task(command: IntakeCommand) -> dict[str, Any]:
            if os.environ.get("DARKFAC_INTAKE_ENABLED", "false").lower() != "true":
                raise HTTPException(status_code=503, detail="Autonomous intake is not activated")
            if command.project_id != os.environ.get("DARKFAC_ALLOWED_PROJECT_ID", "darkfac"):
                raise HTTPException(status_code=403, detail="Project is not authorized")
            try:
                receipt = self.store.accept(command, datetime.now(UTC))
                logger.info(
                    "Ingress accepted demand %s for run %s (mode=%s)",
                    receipt.demand_id,
                    receipt.run_id,
                    receipt.mode,
                )
                return {
                    "status": "accepted",
                    "run_id": receipt.run_id,
                    "demand_id": receipt.demand_id,
                    "mode": receipt.mode,
                    "initial_job_id": receipt.initial_job_id,
                    "committed_at": receipt.committed_at,
                }
            except IdempotencyConflict as exc:
                raise HTTPException(status_code=409, detail="Idempotency conflict") from exc
            except Exception as exc:
                logger.error("Failed to accept intake command (%s)", type(exc).__name__)
                raise HTTPException(status_code=500, detail="Task intake failed") from exc

        @app.get("/api/v1/tasks/{run_id}", dependencies=[Depends(require_api_token)])
        def get_task_status(run_id: str) -> dict[str, Any]:
            status = self.store.get_run_status(run_id)
            if not status:
                raise HTTPException(status_code=404, detail="Run not found")
            if status.get("project_id") != os.environ.get("DARKFAC_ALLOWED_PROJECT_ID", "darkfac"):
                raise HTTPException(status_code=404, detail="Run not found")
            return status

        return app

    def shutdown(self) -> None:
        """Clean shutdown of coordinator background threads and connections."""
        self._running = False
        if self._stop_event is not None and not self._stop_event.is_set():
            self._stop_event.set()
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        if self._dbos_instance is not None:
            try:
                import dbos  # type: ignore[import-not-found]
                dbos.DBOS.destroy()
            except Exception:
                pass
            self._dbos_instance = None


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Dark Factory Cloud Coordinator")
    parser.add_argument(
        "--status",
        action="store_true",
        help="Inspect and output CoordinatorStatus in JSON and exit without starting daemon loop",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Polling interval in seconds for the supervision loop",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8001,
        help="Port for the HTTP API ingress server",
    )
    parser.add_argument(
        "--no-http",
        action="store_true",
        help="Disable background HTTP ingress server",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    coordinator = CloudCoordinator(port=args.port)

    if args.status:
        status = coordinator.inspect_status()
        print(status.model_dump_json(indent=2))
        return 0 if status.database_status == "ready" else 2

    return coordinator.run_forever(
        poll_interval_sec=args.poll_interval,
        enable_http=not args.no_http,
    )


if __name__ == "__main__":
    sys.exit(main())
