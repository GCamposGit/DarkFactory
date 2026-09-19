from __future__ import annotations

import os
import sys
import logging
import signal
import threading
from datetime import UTC, datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from core.orchestrator.adapters.control_postgres import PostgresControlStore
from core.orchestrator.cloud_db import probe_cloud_database, sanitize_database_url
from core.workflow.control_contracts import (
    IdempotencyConflict,
    IntakeCommand,
    IntakeReceipt,
    RuntimeOwner,
)
from core.workflow.reconciliation import reconcile_portfolio_state

logger = logging.getLogger("darkfac.cloud_coordinator")


class CoordinatorStatus(BaseModel):
    """Structured health and status of the cloud coordinator."""

    model_config = ConfigDict(frozen=True)

    role: str = "coordinator"
    application_version: str = "v1"
    database_status: str = Field(description="'ready', 'waiting_access', or 'error'")
    dbos_engine: str = Field(description="'active', 'absent', or 'offline'")
    max_concurrent_slots: int = 2
    active_workflows_count: int = 0
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
    ) -> None:
        self.database_url = database_url or os.environ.get("DARKFAC_HF02_DATABASE_URL")
        self.max_slots = (
            max_concurrent_slots
            if max_concurrent_slots is not None
            else int(os.environ.get("DARKFAC_MAX_CONCURRENT_SLOTS", "2"))
        )
        self.application_version = application_version
        self.port = port if port is not None else int(os.environ.get("DARKFAC_COORDINATOR_PORT", "8001"))
        self._dbos_instance: Any = None
        self._running = False
        self._stop_event: threading.Event | None = None
        self._recovered_ids: list[str] = []
        self._store_instance: PostgresControlStore | None = None
        self._uvicorn_server: Any = None

    @property
    def store(self) -> PostgresControlStore:
        if self._store_instance is None:
            self._store_instance = PostgresControlStore(
                database_url=self.database_url,
                runtime_owner=RuntimeOwner.CLOUD_DBOS_POSTGRES.value,
                lease_duration_sec=300,
            )
        return self._store_instance

    def inspect_status(self) -> CoordinatorStatus:
        """Inspect coordinator operational status without throwing exceptions."""
        probe = probe_cloud_database(self.database_url)
        db_status = "ready" if probe.status == "ready" else "waiting_access"

        dbos_available = False
        try:
            import dbos  # type: ignore[import-not-found]
            dbos_available = True
        except ImportError:
            pass

        engine_status = "active" if (dbos_available and db_status == "ready") else ("absent" if not dbos_available else "offline")

        return CoordinatorStatus(
            role="coordinator",
            application_version=self.application_version,
            database_status=db_status,
            dbos_engine=engine_status,
            max_concurrent_slots=self.max_slots,
            active_workflows_count=0,
            recovered_workflows_count=len(self._recovered_ids),
            error_message=probe.error_message if db_status != "ready" else None,
            http_port=self.port,
        )

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

        # 3. DBOS recovery if available
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
        from fastapi import FastAPI, HTTPException

        app = FastAPI(title="Dark Factory Cloud Coordinator", version=self.application_version)

        @app.get("/healthz")
        @app.get("/status")
        def get_status() -> CoordinatorStatus:
            return self.inspect_status()

        @app.post("/api/v1/tasks", status_code=202)
        def submit_task(command: IntakeCommand) -> dict[str, Any]:
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
                raise HTTPException(status_code=409, detail=str(exc))
            except Exception as exc:
                logger.error("Failed to accept intake command: %s", exc)
                raise HTTPException(status_code=500, detail=f"Failed to accept task: {exc}")

        @app.get("/api/v1/tasks/{run_id}")
        def get_task_status(run_id: str) -> dict[str, Any]:
            status = self.store.get_run_status(run_id)
            if not status:
                raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
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
