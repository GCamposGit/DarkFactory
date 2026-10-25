"""
Headless business logic layer for DarkHub.
Decoupled from Web UI and HTTP frameworks (Reachability standard).

HubService is composed from per-domain mixins (service_<domain>.py); this module keeps
construction, the live-line cache and the public re-exports used by routers and tests.
"""

import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Optional, Tuple, Any

from core.paths import state_root
from core.usage.ledger import ModelUsageLedger
from core.usage.monitor import AccountUsageMonitor
from core.usage.api_credits import ApiCreditsMonitor
from core.roadmap.service import build_repository_roadmap_service
from core.demands.service import DemandsService
from core.demands.specifier import DemandSpecifier
from core.demands.store import DemandsStore
from core.harness.test_subagent import TestSubagentEngine
from core.infra.inventory import InventoryManager
from core.infra.metrics import InfraMetricsReport
from core.workflow.line_live import LineLiveSnapshot, read_line_live
from hub.backend.usage_sync_watch import UsageSyncWatch
from core.owner_actions.remote_ledger import from_env as owner_actions_remote_from_env
from core.owner_actions.store import ENV_PATH as OWNER_ACTIONS_ENV_PATH, OwnerActionStore

from hub.backend.service_net import (  # noqa: F401  (re-exported public API)
    BLOCKED_METADATA_HOSTS,
    BLOCKED_SSRF_NETWORKS,
    DisallowedDestinationError,
    SafeRedirectHandler,
    is_destination_allowed,
)
from hub.backend.service_line import LineDemandsMixin
from hub.backend.service_interventions import InterventionsMixin
from hub.backend.service_progress import ProgressMixin
from hub.backend.service_catalog import CatalogMixin
from hub.backend.service_usage import UsageMixin
from hub.backend.service_ai import AiStudioMixin
from hub.backend.service_integrations import IntegrationsMixin

logger = logging.getLogger("darkhub.service")

# One control-store read per window, shared by all /api/line/live clients and streams (USR-138).
LINE_LIVE_CACHE_TTL_SECONDS = 2.5
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


class HubService(
    LineDemandsMixin,
    InterventionsMixin,
    ProgressMixin,
    CatalogMixin,
    UsageMixin,
    AiStudioMixin,
    IntegrationsMixin,
):
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        ollama_base_url: str = "http://localhost:11434",
        usage_dir: Optional[Path] = None,
        roadmap_root: Optional[Path] = None,
        state_path: Optional[Path] = None,
        orchestrator_path: Optional[Path] = None,
        project_root: Optional[Path] = None,
        control_store: Optional[Any] = None,
        control_db_path: Optional[Path] = None,
        control_database_url: Optional[str] = None,
        seed_dir: Optional[Path] = None,
        line_control_store: Optional[Any] = None,
    ) -> None:
        self._control_store = control_store
        # Injected store for the production LINE (tests/embedded use). Never inferred from the
        # local control store above: that one is the Hub's own SQLite, which no worker reads.
        self._line_store_override = line_control_store
        self._line_store_cache: Optional[Any] = None
        if data_dir is None:
            # Default to hub/data relative to this file
            self.data_dir = Path(__file__).resolve().parent.parent / "data"
        else:
            self.data_dir = Path(data_dir)

        # v2: the v1 token was served by the public /api/session; a new file name rotates it on deploy.
        self.session_file = self.data_dir / "session_token.v2.txt"
        loaded_token = ""
        if self.session_file.exists():
            try:
                loaded_token = self.session_file.read_text(encoding="utf-8").strip()
            except Exception:
                loaded_token = ""
        self._session_token = loaded_token or secrets.token_urlsafe(32)
        if not loaded_token:
            try:
                self.session_file.parent.mkdir(parents=True, exist_ok=True)
                self.session_file.write_text(self._session_token, encoding="utf-8")
            except Exception:
                pass

        self.services_file = self.data_dir / "services.json"
        self.default_services_file = self.data_dir / "default_services.json"
        self.prompts_file = self.data_dir / "default_prompts.json"
        # Cloud seed dir (USR-44): an image-level copy of hub/data that a Dokploy
        # volume mount cannot shadow. The explicit constructor argument wins over
        # DARKHUB_SEED_DIR so tests and callers can be deterministic.
        if seed_dir is not None:
            self.seed_dir: Optional[Path] = Path(seed_dir)
        else:
            env_seed_dir = os.environ.get("DARKHUB_SEED_DIR", "").strip()
            self.seed_dir = Path(env_seed_dir) if env_seed_dir else None
        self.ollama_base_url = ollama_base_url
        if usage_dir is not None:
            self.usage_dir = Path(usage_dir)
        elif data_dir is not None:
            self.usage_dir = self.data_dir / "usage"
        else:
            self.usage_dir = state_root() / "usage"
        self.model_usage_ledger = ModelUsageLedger(self.usage_dir)
        # USR-232: on the VPS the synced snapshots live in an explicit named-volume directory and the
        # off-VPS providers are read from them only (never probed). Unset keeps the local behaviour.
        snapshot_root_env = os.environ.get("DARKHUB_USAGE_SNAPSHOT_DIR", "").strip()
        snapshot_root = Path(snapshot_root_env) if snapshot_root_env and usage_dir is None else self.usage_dir
        self.account_usage_monitor = AccountUsageMonitor(
            snapshot_root / "providers", synced_only=bool(snapshot_root_env) and usage_dir is None
        )
        self.api_credits_monitor = ApiCreditsMonitor(snapshot_root / "credits")
        # USR-234: refresh requests for the metering host live next to the synced snapshots.
        from core.usage.refresh_requests import RefreshRequestStore
        self.refresh_request_store = RefreshRequestStore(snapshot_root / "refresh_requests.json")
        self._probes_locally = not (bool(snapshot_root_env) and usage_dir is None)
        from core.telemetry.store import TelemetryStore
        self.telemetry_store = TelemetryStore(self.usage_dir.parent / "telemetry.db")
        try:
            self.telemetry_store.import_legacy_if_empty(self.usage_dir / "model_usage.json")
        except Exception:
            pass

        if data_dir is not None:
            self.demands_dir = self.data_dir / "demands"
        else:
            self.demands_dir = Path(__file__).resolve().parents[2] / ".factory" / "demands"
        self.demands_store = DemandsStore(self.demands_dir / "demands.json")
        self.demands_service = DemandsService(
            store=self.demands_store,
            specifier=DemandSpecifier(ollama_url=ollama_base_url),
        )
        self._autonomous_intake_service: Optional[Any] = None

        repository_root = project_root or roadmap_root or Path(__file__).resolve().parents[2]
        self.project_root = repository_root

        # Self-healing volume seed restoration: if .factory is shadowed by an empty Docker volume,
        # restore canonical metadata (roadmap, demands, infra) from .factory_seed.
        factory_dir = repository_root / ".factory"
        factory_seed = repository_root / ".factory_seed"
        if factory_seed.exists() and factory_seed.is_dir():
            import shutil
            factory_dir.mkdir(parents=True, exist_ok=True)
            for seed_item in factory_seed.iterdir():
                dest = factory_dir / seed_item.name
                if not dest.exists():
                    try:
                        if seed_item.is_dir():
                            shutil.copytree(seed_item, dest)
                        else:
                            shutil.copy2(seed_item, dest)
                    except Exception as exc:
                        logger.warning(f"Failed to copy seed item {seed_item.name}: {exc}")

        # Owner action backlog (USR-190). Definitions: tracked in the repository and shipped in the
        # image (or isolated under data_dir for tests/embedded use). Resolutions made in the Hub go
        # to an overlay on the persistent data volume so a redeploy never resurrects an item the
        # owner already closed.
        if data_dir is not None:
            owner_actions_path: Path = self.data_dir / "owner_actions" / "owner_actions.json"
        elif os.environ.get(OWNER_ACTIONS_ENV_PATH, "").strip():
            owner_actions_path = Path(os.environ[OWNER_ACTIONS_ENV_PATH].strip())
        else:
            owner_actions_path = repository_root / ".factory" / "owner_actions" / "owner_actions.json"
        self.owner_action_store = OwnerActionStore(
            owner_actions_path,
            overlay_path=self.data_dir / "owner_actions" / "resolutions.json",
            dynamic_path=self.data_dir / "owner_actions" / "dynamic.json",
            remote_ledger=owner_actions_remote_from_env() if data_dir is None else None,
        )

        self.usage_sync_watch = UsageSyncWatch(self.data_dir / "usage_sync" / "state.json")

        self.task_state_path = Path(state_path) if state_path is not None else state_root() / "state.json"
        self.orchestrator_path = (
            Path(orchestrator_path)
            if orchestrator_path is not None
            else state_root() / "orchestrator.sqlite3"
        )
        # Canonical HF-05 control store read by the task dashboard (USR-42).
        # ``control_database_url=None`` defers to DARKHUB_CONTROL_DATABASE_URL, then DARKFAC_HF02_DATABASE_URL.
        self.control_db_path = (
            Path(control_db_path) if control_db_path is not None else state_root() / "control.db"
        )
        self.control_database_url = control_database_url
        self._line_live_lock = threading.Lock()
        self._line_live_cache: tuple[float, LineLiveSnapshot] | None = None
        self.test_subagent_engine = TestSubagentEngine(project_root=repository_root, log_dir=state_root() / "test_logs")
        self.roadmap = build_repository_roadmap_service(
            repository_root,
            demands_path=self.demands_dir / "demands.json",
            include_demands=True,
            include_hf=True,
            include_infra=True,
        )

        if data_dir is not None:
            self.infra_path = self.data_dir / "infra" / "inventory.json"
        else:
            self.infra_path = Path(__file__).resolve().parents[2] / ".factory" / "infra" / "inventory.json"
        self.infra_manager = InventoryManager(self.infra_path)

        # Multi-Project Portfolio Subsystem (DH-08)
        from core.portfolio.budget_manager import PortfolioBudgetManager
        from core.portfolio.scheduler import PortfolioScheduler
        from core.projects.registry import ProjectRegistry
        self.portfolio_dir = state_root() / "portfolio"
        self.budget_manager = PortfolioBudgetManager(storage_file=self.portfolio_dir / "budgets.json")
        self.portfolio_scheduler = PortfolioScheduler(storage_file=self.portfolio_dir / "scheduler_state.json")
        self.project_registry = ProjectRegistry(repository_root / ".factory" / "projects.json")

        self._ensure_storage()
        self._cached_infra_metrics: Optional[Tuple[float, InfraMetricsReport]] = None

    @property
    def control_store(self) -> Any:
        """Accessor for canonical SQLiteControlStore (HF-13-02)."""
        if self._control_store is None:
            from core.workflow.control_store import SQLiteControlStore
            control_db = state_root() / "control.db"
            control_db.parent.mkdir(parents=True, exist_ok=True)
            self._control_store = SQLiteControlStore(db_path=control_db)
        return self._control_store

    def _line_control_store(self) -> Any:
        """The store the production line's workers claim from.

        Only an explicitly injected LINE store overrides. Otherwise, whenever a line database URL
        is configured (`DARKHUB_LINE_DATABASE_URL` / `DARKFAC_HF02_DATABASE_URL` /
        `DARKHUB_CONTROL_DATABASE_URL`, or the `control_database_url` constructor argument) this is
        the Postgres store the workers claim from, even if the Hub also has its own local control
        store. Without a URL it falls back to that local store (local development).
        """
        if self._line_store_override is not None:
            return self._line_store_override
        from core.line.owner_intake import line_database_url, open_line_store

        url = self.control_database_url or line_database_url()
        if url:
            if self._line_store_cache is None:
                self._line_store_cache = open_line_store(url=url)  # raises if unreachable, never a mock
            return self._line_store_cache
        return self._control_store if self._control_store is not None else self.control_store

    def get_line_live(self) -> LineLiveSnapshot:
        """Stage-by-stage live projection of the autonomous line (USR-138).

        One read of the control store per ``LINE_LIVE_CACHE_TTL_SECONDS`` window, shared by every
        client and SSE stream (single flight: concurrent callers wait for the same read).
        """
        with self._line_live_lock:
            cached = self._line_live_cache
            now_monotonic = time.monotonic()
            if cached is not None and now_monotonic - cached[0] < LINE_LIVE_CACHE_TTL_SECONDS:
                return cached[1]
            tickets: list[dict[str, Any]] = []
            titles: dict[str, str] = {}
            warnings: list[str] = []
            try:
                for ticket in self.demands_service.list_tickets():
                    titles[ticket.id] = ticket.title
                    tickets.append(
                        {
                            "id": ticket.id,
                            "project_id": ticket.project_id,
                            "title": ticket.title,
                            "status": getattr(ticket.status, "value", ticket.status),
                            "horizon": getattr(ticket.horizon, "value", ticket.horizon),
                            "updated_at": ticket.updated_at,
                            "problem_statement": ticket.problem_statement,
                            "non_goals": ticket.non_goals,
                            "acceptance_criteria": ticket.acceptance_criteria,
                            "tags": ticket.tags,
                            "dependencies": ticket.dependencies,
                        }
                    )
            except Exception as exc:  # demands are enrichment: never take the live board down
                logger.warning("Demands unavailable for live line: %s", exc)
                warnings.append(f"demands indisponíveis ({type(exc).__name__})")
            snapshot = read_line_live(
                self.control_db_path,
                database_url=self.control_database_url,
                titles=titles,
                tickets=tickets,
            )
            if warnings:
                snapshot = snapshot.model_copy(update={"warnings": [*snapshot.warnings, *warnings]})
            self._line_live_cache = (time.monotonic(), snapshot)
            return snapshot


DarkHubService = HubService
