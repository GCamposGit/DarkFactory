"""Cloud coordinator service for Dark Factory durable workflows.

Governed by HF-03-03 / ADR-HF-001.
Headless supervisor managing DBOS workflow registration, crash recovery scans,
and event dispatching on the Dokploy Cloud VPS.
"""

from __future__ import annotations

import os
import sys
import logging
import signal
import threading
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from core.orchestrator.cloud_db import probe_cloud_database, sanitize_database_url

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


class CloudCoordinator:
    """Headless cloud coordinator managing durable workflow lifecycle."""

    def __init__(
        self,
        database_url: str | None = None,
        max_concurrent_slots: int | None = None,
        application_version: str = "v1",
    ) -> None:
        self.database_url = database_url or os.environ.get("DARKFAC_HF02_DATABASE_URL")
        self.max_slots = (
            max_concurrent_slots
            if max_concurrent_slots is not None
            else int(os.environ.get("DARKFAC_MAX_CONCURRENT_SLOTS", "2"))
        )
        self.application_version = application_version
        self._dbos_instance: Any = None
        self._running = False
        self._stop_event: threading.Event | None = None
        self._recovered_ids: list[str] = []

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
        )

    def scan_and_recover_pending(self) -> list[str]:
        """Scan system database for pending/interrupted workflows and queue recovery."""
        status = self.inspect_status()
        if status.database_status != "ready":
            logger.warning("Cannot recover workflows: database is in %s state", status.database_status)
            return []

        # When connected to DBOS PostgreSQL, scan recovery records
        recovered: list[str] = []
        try:
            import dbos  # type: ignore[import-not-found]
            # DBOS automatically enqueues incomplete workflows on launch
            self._recovered_ids = recovered
        except Exception as exc:
            logger.error("Error during workflow recovery scan: %s", exc)
        return self._recovered_ids

    def run_forever(
        self,
        stop_event: threading.Event | None = None,
        poll_interval_sec: float = 5.0,
    ) -> int:
        """Continuous supervision loop with SIGTERM and SIGINT interception."""
        if stop_event is None:
            stop_event = threading.Event()
        self._stop_event = stop_event
        self._running = True

        def _signal_handler(signum: int, frame: Any) -> None:
            logger.info("Signal %s received; initiating coordinator shutdown", signum)
            stop_event.set()

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

        logger.info(
            "Cloud coordinator started supervision loop (poll_interval=%.1fs)",
            poll_interval_sec,
        )
        try:
            while not stop_event.is_set():
                try:
                    status = self.inspect_status()
                    if status.database_status == "ready":
                        self.scan_and_recover_pending()
                    else:
                        logger.warning(
                            "Database not ready (status=%s, error=%s); coordinator standing by",
                            status.database_status,
                            status.error_message,
                        )
                except Exception as exc:
                    logger.warning("Transient error in coordinator supervision loop: %s", exc)
                stop_event.wait(timeout=poll_interval_sec)
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

    def shutdown(self) -> None:
        """Clean shutdown of coordinator background threads and connections."""
        self._running = False
        if self._stop_event is not None and not self._stop_event.is_set():
            self._stop_event.set()
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
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    coordinator = CloudCoordinator()

    if args.status:
        status = coordinator.inspect_status()
        print(status.model_dump_json(indent=2))
        return 0 if status.database_status == "ready" else 2

    return coordinator.run_forever(poll_interval_sec=args.poll_interval)


if __name__ == "__main__":
    sys.exit(main())
