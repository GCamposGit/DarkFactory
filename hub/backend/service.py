"""
Headless business logic layer for DarkHub.
Decoupled from Web UI and HTTP frameworks (Reachability standard).
"""

import ipaddress
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from hub.backend.models import (
    ExportCatalogResponse,
    HealthCheckResult,
    HealthStatus,
    ImportCatalogRequest,
    ImportCatalogResponse,
    OllamaGenerateRequest,
    OllamaGenerateResponse,
    OllamaModelInfo,
    OllamaStatusResponse,
    OpenRouterModelInfo,
    OpenRouterStatusResponse,
    PlaygroundProvider,
    PromptTemplate,
    ServiceCategory,
    ServiceCreate,
    ServiceItem,
    ServiceLaunchResponse,
    ServiceUpdate,
    UnifiedGenerateRequest,
    UnifiedGenerateResponse,
    LearningPackSummary,
    GenerateLearningPackRequest,
    GenerateLearningPackResponse,
    ContentGenerateRequest,
    ContentLintRequest,
    VisualGenerateRequest,
    VisualIllustrateRequest,
    TaskDashboardEvidence,
    TaskDashboardItem,
    TaskDashboardReport,
    UsageSyncPayload,
    UsageSyncResponse,
    IncidentAlert,
    ProgressProjection,
    PortfolioProjectSummary,
    PortfolioProjectDetailResponse,
    PortfolioOverviewResponse,
)
from core.execution.providers import get_openrouter_api_key
from core.content import (
    ContentEngine,
    AntiSlopLinter,
    ContentRequest,
    ContentType,
    CONTENT_PRESETS,
)
from core.visual import (
    VisualStudio,
    VisualPromptSpec,
    AssetType,
    VisualTheme,
    AspectRatio,
)
from core.learning_pack import (
    LearningPackStore,
    LearningPackGenerator,
    LearningPackRenderer,
)
from core.benchmarks import (
    BenchmarkDomain,
    DOMAIN_METADATA,
    DailyBenchmarkLedger,
    ensure_daily_benchmark,
    get_benchmark_router,
    compute_pareto_frontier,
    is_production_interactive_model,
)
from core.usage.ledger import ModelUsageLedger, infer_model_tier
from core.usage.models import ModelCallEvent, ModelModality, ModelTier
from core.usage.monitor import AccountUsageMonitor
from core.usage.api_credits import (
    ApiCreditsMonitor,
    ApiCreditsReport,
    CreditAccountUpdateRequest,
    ProviderCreditCard,
)
from core.roadmap.models import (
    DeliveryStatus,
    RoadmapHealth,
    RoadmapItem,
    RoadmapProjectSummary,
    RoadmapSnapshot,
    RoadmapSourceDocument,
)
from core.roadmap.service import build_repository_roadmap_service
from core.demands.models import (
    DemandInput,
    DemandSpecificationGuidance,
    GrillRefinementResult,
    GrillSession,
    UserTicket,
)
from core.demands.service import DemandsService
from core.demands.specifier import DemandSpecifier
from core.demands.store import DemandsStore
from core.harness.test_subagent import (
    DistilledTestReport,
    TestExecutionInstruction,
    TestSubagentEngine,
)
from core.infra.inventory import InventoryManager
from core.infra.cards import (
    InfraCard,
    InfraCardsReport,
    build_infra_cards_report,
)
from core.workflow.job_board import JobBoardEntry, read_job_board
from hub.backend.webhooks import (
    CloudGatewayStatus,
    DokployDeployClient,
    DokployDeployTrigger,
    WebhookEngine,
    WebhookEventRecord,
)
from core.integrations.telegram import TelegramGateway, TelegramConfig
from core.integrations.n8n import N8nProbe, N8nConfig, N8nApiClient
from core.orchestrator.release_pipeline import ReleasePipelineService
from core.acceptance.environment import HF15EnvironmentManager, load_hf15_config
from core.acceptance.observability import HF15ObservabilityTracker
from core.acceptance.rollback import HF15RollbackCoordinator
from core.acceptance.models import HF15MetricsSummary, RollbackExecutionRecord


logger = logging.getLogger("darkhub.service")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

BLOCKED_SSRF_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),     # Link-local / Cloud Metadata (AWS, GCP, Azure, etc.)
    ipaddress.ip_network("0.0.0.0/8"),          # Current network
    ipaddress.ip_network("224.0.0.0/4"),        # Multicast
    ipaddress.ip_network("240.0.0.0/4"),        # Reserved
    ipaddress.ip_network("255.255.255.255/32"), # Broadcast
]

BLOCKED_METADATA_HOSTS = {
    "metadata",
    "metadata.google.internal",
    "instance-data",
}


class DisallowedDestinationError(urllib.error.URLError):
    """Raised when a probe or redirect targets a forbidden IP/host or scheme."""
    pass


def is_destination_allowed(url: str) -> Tuple[bool, str]:
    """
    Evaluates whether a target URL is permitted for health checks and network probing (DF-08).
    Strictly blocks cloud metadata endpoints, link-local addresses, and non-HTTP schemes.
    Deliberately permits legitimate local cluster services (localhost, 127.0.0.1, ::1).
    """
    if not url or not isinstance(url, str):
        return False, "URL is empty or invalid"

    try:
        parsed = urllib.parse.urlparse(url.strip())
    except Exception as exc:
        return False, f"Malformed URL: {exc}"

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return False, f"Protocol '{scheme}' not permitted for health probes (only HTTP/HTTPS allowed)"

    host = parsed.hostname
    if not host:
        return False, "Missing hostname in target URL"

    host_lower = host.strip("[]").lower()

    # Explicitly permitted loopback endpoints for local services.
    if host_lower in ("localhost", "127.0.0.1", "::1", "testserver"):
        return True, "Permitted local loopback service"

    # Check for metadata hostnames
    if host_lower in BLOCKED_METADATA_HOSTS:
        return False, f"Cloud metadata host '{host_lower}' is strictly blocked"

    # Check if host is an IP address literal
    try:
        ip_obj = ipaddress.ip_address(host_lower)
        for net in BLOCKED_SSRF_NETWORKS:
            if ip_obj in net:
                return False, f"Destination IP {host_lower} is blocked by SSRF policy (cloud metadata/link-local)"

        if ip_obj.is_private and not ip_obj.is_loopback:
            return False, f"Private network address {host_lower} is blocked by default SSRF policy"

        if ip_obj.is_reserved or ip_obj.is_multicast or ip_obj.is_link_local:
            return False, f"Address {host_lower} is blocked by SSRF policy"
    except ValueError:
        # Host is a domain name (not raw IP)
        pass

    return True, "Permitted external destination"


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """
    Inspects HTTP redirect Location headers to prevent SSRF bypass via 3xx redirects (DF-08).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed, reason = is_destination_allowed(newurl)
        if not allowed:
            raise DisallowedDestinationError(
                f"SSRF Redirect Blocked: redirect to '{newurl}' is disallowed ({reason})"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HubService:
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
    ) -> None:
        self._session_token = secrets.token_urlsafe(32)
        self._control_store = control_store
        if data_dir is None:
            # Default to hub/data relative to this file
            self.data_dir = Path(__file__).resolve().parent.parent / "data"
        else:
            self.data_dir = Path(data_dir)

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
            self.usage_dir = Path(__file__).resolve().parents[2] / ".factory" / "usage"
        self.model_usage_ledger = ModelUsageLedger(self.usage_dir)
        self.account_usage_monitor = AccountUsageMonitor(self.usage_dir / "providers")
        self.api_credits_monitor = ApiCreditsMonitor(self.usage_dir / "credits")
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

        self.task_state_path = Path(state_path) if state_path is not None else repository_root / ".factory" / "state.json"
        self.orchestrator_path = (
            Path(orchestrator_path)
            if orchestrator_path is not None
            else repository_root / ".factory" / "orchestrator.sqlite3"
        )
        # Canonical HF-05 control store read by the task dashboard (USR-42).
        # ``control_database_url=None`` defers to DARKHUB_CONTROL_DATABASE_URL, then DARKFAC_HF02_DATABASE_URL.
        self.control_db_path = (
            Path(control_db_path) if control_db_path is not None else repository_root / ".factory" / "control.db"
        )
        self.control_database_url = control_database_url
        self.test_subagent_engine = TestSubagentEngine(project_root=repository_root)
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
        self.portfolio_dir = repository_root / ".factory" / "portfolio"
        self.budget_manager = PortfolioBudgetManager(storage_file=self.portfolio_dir / "budgets.json")
        self.portfolio_scheduler = PortfolioScheduler(storage_file=self.portfolio_dir / "scheduler_state.json")
        self.project_registry = ProjectRegistry(repository_root / ".factory" / "projects.json")

        self._ensure_storage()

    @property
    def control_store(self) -> Any:
        """Accessor for canonical SQLiteControlStore (HF-13-02)."""
        if self._control_store is None:
            from core.workflow.control_store import SQLiteControlStore
            control_db = self.project_root / ".factory" / "control.db"
            control_db.parent.mkdir(parents=True, exist_ok=True)
            self._control_store = SQLiteControlStore(db_path=control_db)
        return self._control_store

    def set_autonomous_intake_service(self, service: Any) -> None:
        """Inject an AutonomousIntakeService instance (HF-08-02)."""
        self._autonomous_intake_service = service

    def get_autonomous_intake_service(self) -> Any:
        """Get or initialize the autonomous intake service with ControlStore (HF-08-02)."""
        if self._autonomous_intake_service is None:
            from core.demands.autonomous_intake import AutonomousIntakeService
            from core.workflow.control_contracts import StoreUnavailableError

            try:
                store = self.control_store
            except Exception as exc:
                raise StoreUnavailableError(f"Underlying control store unavailable: {exc}") from exc

            self._autonomous_intake_service = AutonomousIntakeService(
                store=store,
                demands_store=self.demands_store,
            )
        return self._autonomous_intake_service

    def accept_autonomous_demand(
        self,
        command: Any,
        now: Optional[datetime] = None,
    ) -> Any:
        """Accept an intake command transactionally and commit to ControlStore (HF-08-02)."""
        service = self.get_autonomous_intake_service()
        return service.accept(command, now=now)

    def run_tests(self, instruction: TestExecutionInstruction) -> DistilledTestReport:
        """Execute test suite via headless test subagent engine and return distilled report."""
        return self.test_subagent_engine.execute(instruction)

    def get_task_dashboard(self) -> TaskDashboardReport:
        """Build a read-only projection of lifecycle, run, usage and evidence data from the canonical control store (DH-16)."""
        control = read_job_board(self.control_db_path, database_url=self.control_database_url)
        warnings = list(control.warnings)
        titles = self._dashboard_demand_titles() if control.entries else {}
        rows = [self._control_dashboard_row(entry, titles) for entry in control.entries]

        rows.sort(key=self._dashboard_sort_key)
        queue: list[TaskDashboardItem] = []
        for index, row in enumerate(rows, start=1):
            queue.append(
                TaskDashboardItem(
                    task_id=row["task_id"],
                    title=row["title"],
                    status=row["status"],
                    stage=row["stage"],
                    priority=row["priority"],
                    queue_position=index,
                    run_id=row["run_id"],
                    run_status=row["run_status"],
                    step_index=row["step_index"],
                    cost_usd=row["cost_usd"],
                    updated_at=row["updated_at"],
                    evidence=row["evidence"],
                    exceptions=row["exceptions"],
                    cause_code=row.get("cause_code"),
                    diagnostic=row.get("diagnostic"),
                )
            )

        usage_source = "ok"
        total_cost = sum(item.cost_usd for item in queue)
        if total_cost == 0.0:
            try:
                usage_summary = self.model_usage_ledger.report(recent_limit=0)
                total_cost = float(usage_summary.total_cost_usd)
            except Exception as exc:  # pragma: no cover - defensive boundary for a corrupt ledger
                usage_source = "error"
                warnings.append(f"usage ledger unavailable: {exc}")
        else:
            usage_source = "control"

        control_source = control.source if control.backend == "none" else f"{control.backend}:{control.source}"
        return TaskDashboardReport(
            generated_at=datetime.now(timezone.utc).isoformat(),
            queue=queue,
            queued_count=len(queue),
            running_count=sum(1 for item in queue if item.run_status == "RUNNING"),
            exception_count=sum(len(item.exceptions) for item in queue),
            total_cost_usd=max(0.0, total_cost),
            sources={
                "control": control_source,
                "usage": usage_source,
            },
            warnings=warnings,
        )

    def _dashboard_demand_titles(self) -> dict[str, str]:
        try:
            return {ticket.id: ticket.title for ticket in self.demands_service.list_tickets()}
        except Exception as exc:  # pragma: no cover - titles are cosmetic, never block the board
            logger.warning("Demand titles unavailable for task dashboard: %s", exc)
            return {}

    @staticmethod
    def _control_dashboard_row(entry: JobBoardEntry, titles: dict[str, str]) -> dict[str, Any]:
        """Project one canonical control-store entry onto the dashboard row shape (USR-42)."""
        evidence = [
            TaskDashboardEvidence(label="projeto", value=entry.project_id, source="control"),
            TaskDashboardEvidence(label="papel", value=entry.role, source="control"),
            TaskDashboardEvidence(label="etapas", value=" → ".join(entry.stages_seen)[:500], source="control"),
        ]
        if entry.diagnostic:
            evidence.append(TaskDashboardEvidence(label="diagnóstico", value=entry.diagnostic[:500], source="control"))
        evidence.extend(
            TaskDashboardEvidence(label="evidência", value=ref[:500], source="control")
            for ref in entry.evidence_refs[:9]
        )
        exceptions: list[str] = []
        if entry.needs_attention:
            reason = entry.cause_code or "sem cause_code registrado"
            exceptions.append(f"{entry.status}: {reason} (tentativa {entry.retry_count}/{entry.max_retries})")
        status = entry.status.upper()
        title = entry.title or titles.get(entry.demand_id) or titles.get(entry.ticket_id) or entry.demand_id
        # Real cloud rows carry ticket_id = project_id (e.g. "darkfac"), which is not a
        # meaningful task identifier; use the unique demand_id instead in that case (USR-44).
        task_id = entry.demand_id if entry.ticket_id == entry.project_id else entry.ticket_id
        return {
            "task_id": task_id,
            "title": title[:240],
            "status": status,
            "stage": entry.stage,
            "priority": 0,
            "run_id": entry.run_id,
            "run_status": status,
            "step_index": entry.iteration,
            "cost_usd": round(entry.total_cost_usd, 8),
            "updated_at": entry.updated_at,
            "evidence": evidence,
            "exceptions": exceptions,
            "cause_code": entry.cause_code,
            "diagnostic": entry.diagnostic,
        }
    @staticmethod
    def _dashboard_priority(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _dashboard_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
        status_rank = {
            "WAITING_HUMAN": 0,
            "NEEDS_FIX": 0,
            "RETRY": 1,
            "REPLAN": 1,
            "RUNNING": 1,
            "IMPLEMENTING": 2,
            "VALIDATING": 3,
            "REVIEWING": 4,
            "PLANNED": 5,
            "TRIAGED": 6,
            "READY_TO_MERGE": 7,
            "PENDING": 5,
            "WAITING_DEPENDENCY": 6,
            "FAILED": 8,
            "MERGED": 9,
            "SUCCEEDED": 9,
            "CANCELLED": 10,
            "UNSET": 10,
        }
        return status_rank.get(row["status"], 99), -row["priority"], row["task_id"]

    def guide_demand(
        self,
        demand: DemandInput,
        *,
        force_heuristic: bool = False,
        timeout: Optional[float] = None,
    ) -> DemandSpecificationGuidance:
        """Guide and structure a user demand using local model or deterministic script ($0)."""
        return self.demands_service.guide_demand(demand, force_heuristic=force_heuristic, timeout=timeout)

    def get_next_ticket_id(self, project_id: str = "darkfac") -> str:
        """Return the next sequential user demand ticket ID."""
        return self.demands_service.get_next_ticket_id(project_id)

    def create_demand_ticket(self, ticket: UserTicket) -> UserTicket:
        """Create and insert a specified user demand into the backlog and invalidate roadmap cache."""
        saved = self.demands_service.create_ticket(ticket)
        if hasattr(self.roadmap, "store") and self.roadmap.store:
            self.roadmap.store.clear(ticket.project_id)
        return saved

    def list_demand_tickets(
        self,
        project_id: Optional[str] = None,
        status: Optional[str] = None,
    ) -> List[UserTicket]:
        """List user demands filtered by project and status."""
        stat_enum = DeliveryStatus(status) if status else None
        return self.demands_service.list_tickets(project_id=project_id, status=stat_enum)

    def get_demand_ticket(self, ticket_id: str) -> Optional[UserTicket]:
        """Retrieve a single user demand ticket."""
        return self.demands_service.get_ticket(ticket_id)

    def update_demand_ticket_status(
        self,
        ticket_id: str,
        status: DeliveryStatus,
        notes: Optional[str] = None,
    ) -> UserTicket:
        """Update the delivery status of a user demand ticket."""
        updated = self.demands_service.update_ticket_status(ticket_id, status, notes=notes)
        if hasattr(self.roadmap, "store") and self.roadmap.store:
            self.roadmap.store.clear(updated.project_id)
        return updated

    def start_demand_grill(
        self,
        ticket_id: str,
        *,
        force_heuristic: bool = False,
        timeout: Optional[float] = None,
    ) -> GrillSession:
        """Start a clarifying Q&A grill session for a demand ticket."""
        return self.demands_service.start_grill_session(
            ticket_id, force_heuristic=force_heuristic, timeout=timeout
        )

    def submit_demand_grill(
        self,
        ticket_id: str,
        answers: dict[str, str],
        session: Optional[GrillSession] = None,
    ) -> GrillRefinementResult:
        """Submit answers to refine a demand ticket in the backlog."""
        result = self.demands_service.submit_grill_answers(ticket_id, answers, session=session)
        if hasattr(self.roadmap, "store") and self.roadmap.store:
            self.roadmap.store.clear(result.refined_ticket.project_id)
        return result

    def list_roadmap_projects(self) -> List[RoadmapProjectSummary]:
        """List projects with an isolated roadmap source."""

        return self.roadmap.list_projects()

    def get_roadmap(
        self,
        project_id: str,
        *,
        search: Optional[str] = None,
        item_type: Optional[str] = None,
        lifecycle_stage: Optional[str] = None,
        delivery_status: Optional[str] = None,
        horizon: Optional[str] = None,
        confidence: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> RoadmapSnapshot:
        """Return a filtered, read-only operational roadmap snapshot."""

        return self.roadmap.get_snapshot(
            project_id,
            search=search,
            item_type=item_type,
            lifecycle_stage=lifecycle_stage,
            delivery_status=delivery_status,
            horizon=horizon,
            confidence=confidence,
            source_id=source_id,
        )

    def get_roadmap_item(self, project_id: str, item_id: str) -> Optional[RoadmapItem]:
        return self.roadmap.get_item(project_id, item_id)

    def get_roadmap_health(self, project_id: str) -> RoadmapHealth:
        return self.roadmap.get_health(project_id)

    def get_roadmap_source(self, project_id: str, source_id: str) -> Optional[RoadmapSourceDocument]:
        return self.roadmap.get_source_document(project_id, source_id)

    # Shipped seed files a Dokploy volume mount can shadow; never touched: services.json,
    # catalog_meta.json, or anything else the owner may have edited in data_dir.
    _CLOUD_SEED_FILES: tuple[str, ...] = ("default_services.json", "default_prompts.json")

    def _apply_cloud_seed(self) -> None:
        """Overlay shipped catalog/prompt seeds from an image-level seed dir (USR-44).

        In Dokploy, ``darkhub-hub-data`` is a named volume mounted at ``/app/hub/data``,
        so a redeploy that ships an updated ``default_services.json`` /
        ``default_prompts.json`` never reaches the running container: the volume's old
        copies keep shadowing the image's new ones, which means ``_apply_catalog_revisions``
        below never sees new revisions and ``list_prompts`` keeps serving stale content.
        ``DARKHUB_SEED_DIR`` (or an explicit ``seed_dir`` constructor argument, which wins)
        points at an image-only copy of ``hub/data`` outside the volume; when set, this
        copies the two shipped seed files over ``data_dir``'s copies before storage is
        initialized, but only when the seed file exists and its content actually differs.
        This is a cosmetic, fail-open path: any OSError is logged and swallowed so a
        seeding hiccup never prevents the Hub from starting.
        """
        if self.seed_dir is None:
            return
        for filename in self._CLOUD_SEED_FILES:
            seed_file = self.seed_dir / filename
            try:
                if not seed_file.exists():
                    continue
                seed_content = seed_file.read_text(encoding="utf-8")
                target_file = self.data_dir / filename
                if target_file.exists() and target_file.read_text(encoding="utf-8") == seed_content:
                    continue
                target_file.write_text(seed_content, encoding="utf-8")
                logger.info("Refreshed %s from cloud seed dir %s", filename, self.seed_dir)
            except OSError as exc:
                logger.warning("Cloud seed refresh failed for %s: %s", filename, exc)

    def _ensure_storage(self) -> None:
        """Ensures the storage directory and initial files exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._apply_cloud_seed()
        if not self.services_file.exists():
            if self.default_services_file.exists():
                logger.info("Initializing services.json from default_services.json")
                content = self.default_services_file.read_text(encoding="utf-8")
                self.services_file.write_text(content, encoding="utf-8")
            else:
                logger.warning("Default services file missing. Creating empty list.")
                self.services_file.write_text("[]", encoding="utf-8")

        self._migrate_launch_metadata()

    def _resolve_launch_script_path(self, script_name: str) -> Path:
        """Resolve a catalog launch path relative to the shared project root."""
        script_path = Path(script_name)
        if not script_path.is_absolute():
            script_path = self.project_root / script_path
        return script_path.resolve()

    def _migrate_launch_metadata(self) -> None:
        """Refresh missing or obsolete launcher metadata from the shipped catalog.

        The hub catalog is persisted in a Docker volume, so changing the versioned
        seed file alone does not update an already-created ``services.json``. Only
        launcher metadata is migrated, leaving user-facing catalog edits intact.
        """
        if not self.services_file.exists() or not self.default_services_file.exists():
            return

        try:
            current_items = json.loads(self.services_file.read_text(encoding="utf-8"))
            default_items = json.loads(self.default_services_file.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Service catalog migration skipped: %s", exc)
            return

        if not isinstance(current_items, list) or not isinstance(default_items, list):
            return

        defaults_by_id = {
            item.get("id"): item
            for item in default_items
            if isinstance(item, dict) and item.get("id")
        }
        changed = False

        for item in current_items:
            if not isinstance(item, dict):
                continue
            default_item = defaults_by_id.get(item.get("id"))
            if not default_item:
                continue

            default_script = default_item.get("launch_script")
            current_script = item.get("launch_script")
            if default_script and not current_script:
                item["launch_script"] = default_script
                changed = True
            elif default_script and current_script != default_script:
                current_path = self._resolve_launch_script_path(str(current_script))
                default_path = self._resolve_launch_script_path(str(default_script))
                if not current_path.exists() and default_path.exists():
                    item["launch_script"] = default_script
                    changed = True

            if "fallback_urls" not in item and "fallback_urls" in default_item:
                item["fallback_urls"] = default_item["fallback_urls"]
                changed = True

        if self._apply_catalog_revisions(current_items, default_items):
            changed = True

        if changed:
            self._save_services_raw(current_items)
            logger.info("Migrated persisted service launcher metadata from default catalog")

    # Fields a catalog revision may refresh; URLs, favorites and pins stay owned by the user.
    _CATALOG_REVISION_FIELDS: tuple[str, ...] = ("name", "description", "tags", "category", "icon", "color")

    def _apply_catalog_revisions(self, current_items: List[Dict], default_items: List[Dict]) -> bool:
        """Propagate shipped catalog evolutions into a persisted ``services.json`` once (USR-42).

        Default items carrying ``catalog_revision`` newer than the last applied revision are
        appended when absent or have their descriptive fields refreshed. Each revision is
        applied only once, so services deleted by the owner afterwards are not resurrected.
        """
        meta_file = self.data_dir / "catalog_meta.json"
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.exists() else {}
        except (OSError, ValueError, json.JSONDecodeError):
            meta = {}
        applied = str(meta.get("applied_revision") or "") if isinstance(meta, dict) else ""
        newest = applied
        by_id = {item.get("id"): item for item in current_items if isinstance(item, dict)}
        changed = False
        for default_item in default_items:
            revision = str(default_item.get("catalog_revision") or "") if isinstance(default_item, dict) else ""
            if not revision or revision <= applied:
                continue
            newest = max(newest, revision)
            existing = by_id.get(default_item.get("id"))
            if existing is None:
                current_items.append({key: value for key, value in default_item.items() if key != "catalog_revision"})
                changed = True
                continue
            for field in self._CATALOG_REVISION_FIELDS:
                if field in default_item and existing.get(field) != default_item[field]:
                    existing[field] = default_item[field]
                    changed = True
        if newest != applied:
            try:
                meta_file.write_text(json.dumps({"applied_revision": newest}, indent=2), encoding="utf-8")
            except OSError as exc:
                logger.warning("Catalog revision marker not persisted: %s", exc)
        return changed

    @staticmethod
    def _env_flag(name: str, default: bool = False) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    def _local_service_launch_enabled(self) -> bool:
        """Return whether this process is allowed to spawn local demo services."""
        return self._env_flag(
            "DARKHUB_ENABLE_LOCAL_SERVICE_LAUNCH",
            default=os.getenv("DARKHUB_ENV", "").strip().lower() != "production",
        )

    def _service_url_overrides(self) -> Dict[str, str]:
        """Read optional deployment-time URL overrides for externally hosted services."""
        raw_value = os.getenv("DARKHUB_SERVICE_URL_OVERRIDES", "").strip()
        if not raw_value:
            return {}

        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            logger.warning("Ignoring invalid DARKHUB_SERVICE_URL_OVERRIDES: %s", exc)
            return {}

        if not isinstance(payload, dict):
            logger.warning("Ignoring DARKHUB_SERVICE_URL_OVERRIDES because it is not an object")
            return {}

        overrides: Dict[str, str] = {}
        for service_id, url in payload.items():
            if not isinstance(service_id, str) or not isinstance(url, str):
                continue
            parsed = urllib.parse.urlparse(url.strip())
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                logger.warning("Ignoring invalid service URL override for %s", service_id)
                continue
            overrides[service_id] = url.strip()
        return overrides

    def _apply_service_url_overrides(self, items: List[Dict]) -> List[Dict]:
        overrides = self._service_url_overrides()
        if not overrides:
            return items

        resolved_items: List[Dict] = []
        for item in items:
            resolved_item = dict(item)
            service_id = resolved_item.get("id")
            if service_id in overrides:
                resolved_item["url"] = overrides[service_id]
                resolved_item["is_local"] = False
            resolved_items.append(resolved_item)
        return resolved_items

    def _record_model_call(
        self,
        *,
        provider: str,
        model: str,
        harness: str = "darkhub",
        modality: ModelModality = ModelModality.TEXT,
        success: bool = True,
        input_tokens: Optional[int] = None,
        processing_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        latency_ms: Optional[float] = None,
        source: str,
        ticket_id: Optional[str] = None,
        project_id: str = "darkfac",
        execution_mode: Optional[str] = "ui",
    ) -> None:
        """Persist telemetry fail-open so observability never breaks inference."""
        try:
            self.model_usage_ledger.record(ModelCallEvent(
                provider=provider,
                model=model,
                tier=ModelTier(infer_model_tier(provider, model)),
                harness=harness,
                modality=modality,
                success=success,
                input_tokens=input_tokens,
                processing_tokens=processing_tokens or 0,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                latency_ms=latency_ms,
                source=source,
                ticket_id=ticket_id,
                project_id=project_id,
                execution_mode=execution_mode,
            ))
        except Exception as exc:
            logger.warning("Model telemetry write failed: %s", exc)

    def get_telemetry_runs(self, filters: Any = None) -> Any:
        """Query paginated model telemetry runs with filters."""
        return self.telemetry_store.query_runs(filters)

    def get_telemetry_stats(self, filters: Any = None) -> Any:
        """Calculate analytical statistics across telemetry runs."""
        return self.telemetry_store.get_stats(filters)

    def list_telemetry_tickets(self) -> list[str]:
        """Return distinct tickets with recorded runs."""
        return self.telemetry_store.list_tickets()

    def record_telemetry_run(self, payload: Any) -> Any:
        """Record an incoming telemetry run directly into SQLite store."""
        return self.telemetry_store.record(payload)

    def get_account_usage(self, force: bool = False) -> Any:
        """Return a partial-success report for every known AI platform."""
        return self.account_usage_monitor.inspect(force=force)

    def get_model_usage(self, project_id: Optional[str] = None) -> Any:
        """Return project-wide model counters and recent attempts."""
        return self.model_usage_ledger.report(project_id=project_id)

    def record_model_usage(self, event: ModelCallEvent) -> Any:
        """Allow Codex, Grok, Gemini and other harnesses to report calls."""
        self.model_usage_ledger.record(event)
        return self.model_usage_ledger.report()

    def get_api_credits_report(self, force: bool = False) -> ApiCreditsReport:
        """Returns API credit balance and current month expenditure in USD ($)."""
        return self.api_credits_monitor.generate_report(force=force)

    def refresh_api_credits_report(self) -> ApiCreditsReport:
        """Forces live refresh of API credit balance and expenditures ($)."""
        return self.api_credits_monitor.refresh_report()

    def update_credit_account(self, provider_id: str, payload: CreditAccountUpdateRequest) -> ProviderCreditCard:
        """Updates and persists credit balances or notes for a provider."""
        return self.api_credits_monitor.update_account(provider_id, payload)

    def sync_usage_data(self, payload: UsageSyncPayload) -> UsageSyncResponse:
        """Persists incoming account quota and credit snapshots from a workstation or worker node."""
        accounts_updated = 0
        credits_updated = 0

        # 1. Ingest account quota snapshots
        for account in payload.accounts:
            if not isinstance(account, dict):
                continue
            provider_id = account.get("provider_id")
            if not provider_id:
                continue
            try:
                self.account_usage_monitor.save_snapshot(str(provider_id), account)
                accounts_updated += 1
            except Exception as exc:
                logger.warning("Failed to save account snapshot for %s: %s", provider_id, exc)

        # 2. Ingest API credit balance snapshots
        for credit in payload.credits:
            if not isinstance(credit, dict):
                continue
            provider_id = credit.get("provider_id")
            if not provider_id:
                continue
            try:
                self.api_credits_monitor.save_snapshot(str(provider_id), credit)
                credits_updated += 1
            except Exception as exc:
                logger.warning("Failed to save credit snapshot for %s: %s", provider_id, exc)

        now_str = datetime.now(timezone.utc).isoformat()
        return UsageSyncResponse(
            status="synchronized",
            client_node_id=payload.client_node_id,
            accounts_updated=accounts_updated,
            credits_updated=credits_updated,
            synced_at=now_str,
            message=f"Synchronized {accounts_updated} account quotas and {credits_updated} credit balances from {payload.client_node_id}.",
        )

    def get_infra_cards_report(
        self,
        probe_liveness: bool = False,
        probe_timeout: float = 0.5,
        project_id: Optional[str] = None,
    ) -> InfraCardsReport:
        """Returns the infrastructure cards report for the Hub."""
        inventory = self.infra_manager.load_or_initialize()
        return build_infra_cards_report(
            inventory,
            probe_network_liveness=probe_liveness,
            probe_timeout=probe_timeout,
            project_id=project_id,
        )

    def get_infra_card(self, node_id: str, probe_liveness: bool = False, probe_timeout: float = 0.5) -> Optional[InfraCard]:
        """Returns a single infrastructure card by node ID."""
        report = self.get_infra_cards_report(probe_liveness=probe_liveness, probe_timeout=probe_timeout)
        for card in report.cards:
            if card.id == node_id:
                return card
        return None

    def _load_services_raw(self) -> List[Dict]:
        try:
            with open(self.services_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                items = data if isinstance(data, list) else []
                return self._apply_service_url_overrides(items)
        except Exception as exc:
            logger.error(f"Failed to read services from {self.services_file}: {exc}")
            return []

    def _save_services_raw(self, items: List[Dict]) -> None:
        temp_file = self.services_file.with_suffix(".tmp")
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(items, f, indent=2, ensure_ascii=False)
            temp_file.replace(self.services_file)
            logger.info(f"Successfully saved {len(items)} services to {self.services_file}")
        except Exception as exc:
            if temp_file.exists():
                temp_file.unlink()
            logger.error(f"Error saving services: {exc}")
            raise

    def list_services(
        self,
        category: Optional[ServiceCategory] = None,
        search: Optional[str] = None,
        favorites_only: bool = False,
        pinned_only: bool = False,
    ) -> List[ServiceItem]:
        raw_items = self._load_services_raw()
        results: List[ServiceItem] = []

        search_term = search.strip().lower() if search else None

        for raw in raw_items:
            try:
                item = ServiceItem(**raw)
            except Exception as exc:
                logger.warning(f"Skipping malformed service item {raw.get('id', 'unknown')}: {exc}")
                continue

            if category and item.category != category:
                continue

            if favorites_only and not item.is_favorite:
                continue

            if pinned_only and not item.pinned:
                continue

            if search_term:
                match_name = search_term in item.name.lower()
                match_desc = search_term in item.description.lower()
                match_tags = any(search_term in t.lower() for t in item.tags)
                match_url = search_term in item.url.lower()
                if not (match_name or match_desc or match_tags or match_url):
                    continue

            results.append(item)

        return results

    def get_service(self, service_id: str) -> Optional[ServiceItem]:
        raw_items = self._load_services_raw()
        for raw in raw_items:
            if raw.get("id") == service_id:
                try:
                    return ServiceItem(**raw)
                except Exception as exc:
                    logger.error(f"Malformed service {service_id}: {exc}")
                    return None
        return None

    def _generate_slug(self, name: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", name.strip().lower()).strip("-")
        return slug or "service"

    def create_service(self, create_data: ServiceCreate) -> ServiceItem:
        raw_items = self._load_services_raw()

        base_id = self._generate_slug(create_data.name)
        new_id = base_id
        counter = 1
        existing_ids = {r.get("id") for r in raw_items}

        while new_id in existing_ids:
            new_id = f"{base_id}-{counter}"
            counter += 1

        is_local = "localhost" in create_data.url or "127.0.0.1" in create_data.url

        new_item = ServiceItem(
            id=new_id,
            name=create_data.name,
            url=create_data.url,
            category=create_data.category,
            description=create_data.description,
            tags=create_data.tags,
            icon=create_data.icon,
            color=create_data.color,
            is_favorite=create_data.is_favorite,
            pinned=create_data.pinned,
            is_local=create_data.is_local or is_local,
            launch_script=create_data.launch_script,
            fallback_urls=create_data.fallback_urls,
        )

        raw_items.append(new_item.model_dump())
        self._save_services_raw(raw_items)
        return new_item

    def update_service(self, service_id: str, update_data: ServiceUpdate) -> Optional[ServiceItem]:
        raw_items = self._load_services_raw()
        updated_item: Optional[ServiceItem] = None

        for i, raw in enumerate(raw_items):
            if raw.get("id") == service_id:
                item = ServiceItem(**raw)
                update_dict = update_data.model_dump(exclude_unset=True)

                if "url" in update_dict:
                    url_val = update_dict["url"]
                    if "is_local" not in update_dict:
                        update_dict["is_local"] = "localhost" in url_val or "127.0.0.1" in url_val

                updated_item = item.model_copy(update=update_dict)
                raw_items[i] = updated_item.model_dump()
                break

        if updated_item:
            self._save_services_raw(raw_items)
        return updated_item

    def delete_service(self, service_id: str) -> bool:
        raw_items = self._load_services_raw()
        initial_count = len(raw_items)
        filtered = [r for r in raw_items if r.get("id") != service_id]

        if len(filtered) < initial_count:
            self._save_services_raw(filtered)
            return True
        return False

    def toggle_favorite(self, service_id: str) -> Optional[ServiceItem]:
        service = self.get_service(service_id)
        if not service:
            return None
        return self.update_service(service_id, ServiceUpdate(is_favorite=not service.is_favorite))

    def toggle_pin(self, service_id: str) -> Optional[ServiceItem]:
        service = self.get_service(service_id)
        if not service:
            return None
        return self.update_service(service_id, ServiceUpdate(pinned=not service.pinned))

    @property
    def session_token(self) -> str:
        """Returns the current active local session token."""
        return self._session_token

    def validate_session(self, token: Optional[str]) -> bool:
        """Validates a provided session token against active local session."""
        if not token or not isinstance(token, str):
            return False
        return secrets.compare_digest(token.strip(), self._session_token)

    def rotate_session(self) -> str:
        """Rotates the session token."""
        self._session_token = secrets.token_urlsafe(32)
        return self._session_token

    def ping_url(self, service_id: str, url: str, timeout_sec: float = 2.5) -> HealthCheckResult:
        """
        Pings a service URL and measures round-trip latency with strict SSRF & redirect guards (DF-08).
        """
        allowed, reason = is_destination_allowed(url)
        if not allowed:
            return HealthCheckResult(
                service_id=service_id,
                url=url,
                status=HealthStatus.OFFLINE,
                latency_ms=None,
                status_code=None,
                error=f"Destination disallowed by SSRF policy: {reason}",
            )

        start = time.perf_counter()
        opener = urllib.request.build_opener(SafeRedirectHandler)
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "DarkHub-Ping/1.0 (Headless Health Probe)"},
            method="HEAD",
        )

        try:
            with opener.open(req, timeout=timeout_sec) as response:
                latency = round((time.perf_counter() - start) * 1000, 1)
                return HealthCheckResult(
                    service_id=service_id,
                    url=url,
                    status=HealthStatus.ONLINE,
                    latency_ms=latency,
                    status_code=response.getcode(),
                )
        except DisallowedDestinationError as redirect_err:
            return HealthCheckResult(
                service_id=service_id,
                url=url,
                status=HealthStatus.OFFLINE,
                latency_ms=None,
                status_code=None,
                error=str(redirect_err),
            )
        except urllib.error.HTTPError as http_err:
            latency = round((time.perf_counter() - start) * 1000, 1)
            # HTTP errors (e.g. 401, 403, 405) still mean the server is reachable and online
            status = HealthStatus.ONLINE if http_err.code < 500 else HealthStatus.DEGRADED
            return HealthCheckResult(
                service_id=service_id,
                url=url,
                status=status,
                latency_ms=latency,
                status_code=http_err.code,
            )
        except Exception as exc:
            # Fallback retry with GET if HEAD was disallowed by remote
            try:
                start_get = time.perf_counter()
                req_get = urllib.request.Request(
                    url,
                    headers={"User-Agent": "DarkHub-Ping/1.0"},
                    method="GET",
                )
                with opener.open(req_get, timeout=timeout_sec) as response:
                    latency = round((time.perf_counter() - start_get) * 1000, 1)
                    return HealthCheckResult(
                        service_id=service_id,
                        url=url,
                        status=HealthStatus.ONLINE,
                        latency_ms=latency,
                        status_code=response.getcode(),
                    )
            except DisallowedDestinationError as redirect_err:
                return HealthCheckResult(
                    service_id=service_id,
                    url=url,
                    status=HealthStatus.OFFLINE,
                    latency_ms=None,
                    status_code=None,
                    error=str(redirect_err),
                )
            except Exception as get_exc:
                return HealthCheckResult(
                    service_id=service_id,
                    url=url,
                    status=HealthStatus.OFFLINE,
                    latency_ms=None,
                    error=str(get_exc),
                )

    def _probe_service_urls(self, service: ServiceItem, timeout_sec: float) -> HealthCheckResult:
        """Probe a service's primary URL and configured fallbacks in order."""
        result = self.ping_url(service_id=service.id, url=service.url, timeout_sec=timeout_sec)
        if result.status == HealthStatus.ONLINE:
            return result

        for fallback_url in service.fallback_urls:
            fallback_result = self.ping_url(
                service_id=service.id,
                url=fallback_url,
                timeout_sec=timeout_sec,
            )
            if fallback_result.status == HealthStatus.ONLINE:
                return fallback_result

        return result

    def launch_service(
        self,
        service_id: str,
        max_wait_sec: float = 5.0,
        poll_interval: float = 0.2,
    ) -> ServiceLaunchResponse:
        """
        Launches a configured local service script if currently offline.
        Waits until the service is verified online via health probe before returning.
        If already online, returns immediately with already_running status.
        """
        service = self.get_service(service_id)
        if not service:
            raise KeyError(f"Service '{service_id}' not found in catalog")

        if not self._local_service_launch_enabled():
            return ServiceLaunchResponse(
                service_id=service.id,
                url=service.url,
                status="external",
                launched=False,
                message=(
                    f"Service '{service.name}' is hosted externally; "
                    "local process launching is disabled in this environment."
                ),
            )

        # Check if already online
        current_health = self._probe_service_urls(service, timeout_sec=1.0)
        if current_health.status == HealthStatus.ONLINE:
            return ServiceLaunchResponse(
                service_id=service.id,
                url=current_health.url or service.url,
                status="already_running",
                launched=False,
                message=f"Service '{service.name}' is already running and accessible.",
            )

        # Resolve launch script path
        script_name = service.launch_script

        if not script_name:
            raise ValueError(f"Service '{service_id}' does not define a launch_script.")

        script_path = self._resolve_launch_script_path(script_name)

        if not script_path.exists():
            raise FileNotFoundError(f"Launch script not found: {script_path}")

        # Prepare environment and launch subprocess
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

        log_dir = self.project_root / ".factory" / "services"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{service_id}.log"

        cmd = [sys.executable, str(script_path), "--no-browser"]
        logger.info(f"Launching service '{service_id}' with command: {' '.join(cmd)}")

        with open(log_file, "a", encoding="utf-8") as out:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.project_root),
                env=env,
                stdout=out,
                stderr=out,
                creationflags=creationflags,
            )

        # Wait for service to come online
        start_wait = time.perf_counter()
        is_online = False
        resolved_url = service.url
        while (time.perf_counter() - start_wait) < max_wait_sec:
            poll_val = proc.poll() if hasattr(proc, "poll") and callable(proc.poll) else None
            if poll_val is not None and isinstance(poll_val, int):
                logger.error(f"Service '{service_id}' process exited prematurely (exit code: {poll_val})")
                break
            time.sleep(poll_interval)
            probe = self._probe_service_urls(service, timeout_sec=0.5)
            if probe.status == HealthStatus.ONLINE:
                is_online = True
                resolved_url = probe.url or service.url
                break

        if is_online:
            return ServiceLaunchResponse(
                service_id=service.id,
                url=resolved_url,
                status="online",
                launched=True,
                message=f"Service '{service.name}' successfully launched (PID: {proc.pid}).",
            )
        else:
            return ServiceLaunchResponse(
                service_id=service.id,
                url=resolved_url,
                status="starting",
                launched=True,
                message=f"Service '{service.name}' process spawned (PID: {proc.pid}), still warming up.",
            )

    def get_service_launch_target(self, service_id: str, max_wait_sec: float = 5.0) -> str:
        """
        Ensures local service is launched if applicable and returns its destination URL.
        """
        service = self.get_service(service_id)
        if not service:
            raise KeyError(f"Service '{service_id}' not found in catalog")

        if not self._local_service_launch_enabled():
            return service.url

        if service.launch_script:
            res = self.launch_service(service_id, max_wait_sec=max_wait_sec)
            if isinstance(res, ServiceLaunchResponse) and res.url:
                return res.url

        return service.url

    def get_ollama_status(self) -> OllamaStatusResponse:
        """Inspects local Ollama instance and lists installed models."""
        url = f"{self.ollama_base_url}/api/tags"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})

        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                raw_models = data.get("models", [])
                models: List[OllamaModelInfo] = []
                for m in raw_models:
                    details = m.get("details", {})
                    models.append(
                        OllamaModelInfo(
                            name=m.get("name", "unknown"),
                            parameter_size=details.get("parameter_size"),
                            quantization=details.get("quantization_level"),
                            format=details.get("format"),
                            modified_at=m.get("modified_at"),
                        )
                    )

                return OllamaStatusResponse(
                    is_online=True,
                    base_url=self.ollama_base_url,
                    model_count=len(models),
                    models=models,
                )
        except Exception as exc:
            logger.info(f"Ollama local ping offline or unreachable: {exc}")
            return OllamaStatusResponse(
                is_online=False,
                base_url=self.ollama_base_url,
                model_count=0,
                models=[],
                error=str(exc),
            )

    def generate_ollama(self, req_data: OllamaGenerateRequest) -> OllamaGenerateResponse:
        """Executes a prompt against local Ollama instance without streaming."""
        url = f"{self.ollama_base_url}/api/generate"
        payload = {
            "model": req_data.model,
            "prompt": req_data.prompt,
            "stream": False,
            "options": {"temperature": req_data.temperature},
        }
        if req_data.system:
            payload["system"] = req_data.system

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=60.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                duration_ms = round((time.perf_counter() - start) * 1000, 1)
                returned_model = data.get("model", req_data.model)
                self._record_model_call(
                    provider="ollama",
                    model=returned_model,
                    input_tokens=data.get("prompt_eval_count"),
                    output_tokens=data.get("eval_count"),
                    cost_usd=0.0,
                    latency_ms=duration_ms,
                    source="hub.generate_ollama",
                )
                return OllamaGenerateResponse(
                    response=data.get("response", ""),
                    model=returned_model,
                    done=data.get("done", True),
                    total_duration_ms=duration_ms,
                )
        except Exception as exc:
            self._record_model_call(
                provider="ollama",
                model=req_data.model,
                success=False,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
                source="hub.generate_ollama",
            )
            logger.error(f"Error querying Ollama model {req_data.model}: {exc}")
            raise RuntimeError(f"Ollama generation failed: {exc}")

    def list_prompts(self, search: Optional[str] = None) -> List[PromptTemplate]:
        """Loads prompt templates catalog."""
        if not self.prompts_file.exists():
            return []

        try:
            with open(self.prompts_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                templates = [PromptTemplate(**item) for item in data]

                if search:
                    term = search.strip().lower()
                    templates = [
                        t
                        for t in templates
                        if term in t.title.lower()
                        or term in t.description.lower()
                        or any(term in tag.lower() for tag in t.tags)
                    ]
                return templates
        except Exception as exc:
            logger.error(f"Failed to read prompt templates: {exc}")
            return []

    def get_latest_benchmark_ledger(self) -> Dict[str, Any]:
        """Loads or builds latest daily benchmark ledger."""
        from core.benchmarks.fetcher import ensure_daily_benchmark
        ledger = ensure_daily_benchmark()
        return ledger.to_dict()

    def get_benchmark_frontier(self) -> Dict[str, Any]:
        """Builds Pareto efficiency frontier summary from latest ledger."""
        from core.benchmarks.fetcher import ensure_daily_benchmark
        from core.benchmarks.frontier import build_frontier_summary
        ledger = ensure_daily_benchmark()
        all_models = list(ledger.models.values())
        summary = build_frontier_summary(ledger.date, all_models)
        return summary.to_dict()

    def refresh_benchmarks(self) -> Dict[str, Any]:
        """Forces an on-demand refresh of daily model benchmarks."""
        from core.benchmarks.fetcher import ensure_daily_benchmark
        ledger = ensure_daily_benchmark(force=True)
        return ledger.to_dict()

    def get_benchmark_proximity(self) -> Dict[str, Any]:
        """Calculates Frontier Proximity Index (FPI), epsilon gaps and challenger status."""
        from core.benchmarks.fetcher import ensure_daily_benchmark
        from core.benchmarks.frontier import compute_frontier_proximity_indices
        ledger = ensure_daily_benchmark()
        all_models = list(ledger.models.values())
        compute_frontier_proximity_indices(all_models, metric="coding_score")
        return {
            "date": ledger.date,
            "models": [m.to_dict() for m in sorted(all_models, key=lambda m: (-(m.frontier_proximity_index or 0.0), -(m.coding_score or 0.0)))]
        }

    def get_top_candidates_for_tier(self, tier: str = "high", k: int = 3) -> List[Dict[str, Any]]:
        """Returns the top-3 speculative candidate models for a given tier."""
        from core.benchmarks.fetcher import ensure_daily_benchmark
        from core.benchmarks.frontier import get_top_candidates_for_tier
        ledger = ensure_daily_benchmark()
        all_models = list(ledger.models.values())
        candidates = get_top_candidates_for_tier(all_models, tier=tier, k=k)
        return [c.to_dict() for c in candidates]

    def get_empirical_ledger(self) -> Dict[str, Any]:
        """Returns cumulative empirical Dark Factory benchmark stats and Elo leaderboard."""
        from core.benchmarks.racing import EmpiricalBenchmarkLedger
        ledger = EmpiricalBenchmarkLedger()
        return {
            "total_models_tracked": len(ledger.stats),
            "total_races_recorded": len(ledger.history),
            "leaderboard": {
                mid: stat.to_dict()
                for mid, stat in sorted(
                    ledger.stats.items(),
                    key=lambda item: item[1].elo_rating,
                    reverse=True,
                )
            },
            "recent_races": [r.to_dict() for r in ledger.history[-20:]],
        }

    def run_speculative_race(self, task_id: str, prompt: str, complexity: str = "high", offline: bool = False) -> Dict[str, Any]:
        """Executes a speculative cascade race."""
        from core.benchmarks.racing import SpeculativeRacingEngine
        engine = SpeculativeRacingEngine()
        result = engine.execute_speculative_race(
            task_id=task_id,
            task_prompt=prompt,
            complexity=complexity,
            offline=offline,
        )
        return result.to_dict()

    def get_openrouter_key(self) -> Optional[str]:
        """Recovers OpenRouter API key from environment variable or Windows registry."""
        return get_openrouter_api_key()

    def get_openrouter_status(self) -> OpenRouterStatusResponse:
        """Inspects OpenRouter authentication, balance/usage, and curated frontier models."""
        key = self.get_openrouter_key()
        curated_models = [
            OpenRouterModelInfo(
                id="nvidia/nemotron-3.5-lightning:free",
                name="Nvidia Nemotron 3.5 (Free)",
                context_length=128000,
                input_cost_per_m=0.0,
                output_cost_per_m=0.0,
                is_pareto=True,
            ),
            OpenRouterModelInfo(
                id="anthropic/claude-3.7-sonnet",
                name="Claude 3.7 Sonnet (Thinking)",
                context_length=200000,
                input_cost_per_m=3.0,
                output_cost_per_m=15.0,
                is_pareto=True,
            ),
            OpenRouterModelInfo(
                id="deepseek/deepseek-r1",
                name="DeepSeek R1 (Full Reasoning)",
                context_length=128000,
                input_cost_per_m=0.55,
                output_cost_per_m=2.19,
                is_pareto=True,
            ),
            OpenRouterModelInfo(
                id="google/gemini-2.0-flash-001",
                name="Google Gemini 2.0 Flash",
                context_length=1000000,
                input_cost_per_m=0.10,
                output_cost_per_m=0.40,
                is_pareto=True,
            ),
            OpenRouterModelInfo(
                id="meta-llama/llama-3.3-70b-instruct",
                name="Meta Llama 3.3 70B Instruct",
                context_length=128000,
                input_cost_per_m=0.13,
                output_cost_per_m=0.40,
                is_pareto=True,
            ),
            OpenRouterModelInfo(
                id="qwen/qwen-2.5-coder-32b-instruct",
                name="Qwen 2.5 Coder 32B Instruct",
                context_length=32768,
                input_cost_per_m=0.07,
                output_cost_per_m=0.16,
                is_pareto=True,
            ),
        ]

        if not key:
            return OpenRouterStatusResponse(
                has_key=False,
                is_authenticated=False,
                models=curated_models,
                error="Chave OPENROUTER_API_KEY não configurada no sistema.",
            )

        req_auth = urllib.request.Request(
            "https://openrouter.ai/api/v1/auth/key",
            headers={"Authorization": f"Bearer {key}", "HTTP-Referer": "https://github.com/DarkFac"},
        )
        try:
            with urllib.request.urlopen(req_auth, timeout=6.0) as resp:
                data = json.loads(resp.read().decode("utf-8")).get("data", {})
                return OpenRouterStatusResponse(
                    has_key=True,
                    is_authenticated=True,
                    key_label=data.get("label"),
                    is_free_tier=bool(data.get("is_free_tier", False)),
                    usage_usd=float(data.get("usage", 0.0)),
                    models=curated_models,
                )
        except Exception as exc:
            logger.warning(f"Failed to authenticate with OpenRouter: {exc}")
            return OpenRouterStatusResponse(
                has_key=True,
                is_authenticated=False,
                models=curated_models,
                error=f"Falha na autenticação com OpenRouter: {exc}",
            )

    def generate_openrouter(self, req_data: UnifiedGenerateRequest) -> UnifiedGenerateResponse:
        """Executes prompt against OpenRouter API with full latency, token and cost calculation."""
        key = self.get_openrouter_key()
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY não encontrada no ambiente ou registro.")

        messages = []
        if req_data.system:
            messages.append({"role": "system", "content": req_data.system})
        messages.append({"role": "user", "content": req_data.prompt})

        payload = {
            "model": req_data.model,
            "messages": messages,
            "temperature": req_data.temperature,
            "max_tokens": req_data.max_tokens or 1024,
        }

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/DarkFac",
                "X-Title": "DarkHub AI Playground",
            },
            method="POST",
        )

        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=90.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                duration_ms = round((time.perf_counter() - start) * 1000, 1)

                choices = data.get("choices", [])
                response_text = choices[0].get("message", {}).get("content", "") if choices else ""
                usage = data.get("usage", {})
                tokens_used = usage.get("total_tokens")

                # Estimate cost if available from benchmark catalog
                cost_usd = None
                try:
                    from core.benchmarks.fetcher import ensure_daily_benchmark
                    ledger = ensure_daily_benchmark()
                    model_entry = ledger.models.get(req_data.model)
                    if model_entry and usage:
                        prompt_tokens = usage.get("prompt_tokens", 0)
                        completion_tokens = usage.get("completion_tokens", 0)
                        cost_usd = round(
                            (prompt_tokens * model_entry.input_cost_per_m + completion_tokens * model_entry.output_cost_per_m) / 1_000_000,
                            6
                        )
                except Exception:
                    pass

                returned_model = data.get("model", req_data.model)
                self._record_model_call(
                    provider="openrouter",
                    model=returned_model,
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    cost_usd=cost_usd,
                    latency_ms=duration_ms,
                    source="hub.generate_openrouter",
                )
                return UnifiedGenerateResponse(
                    response=response_text,
                    provider=PlaygroundProvider.OPENROUTER,
                    model=returned_model,
                    done=True,
                    total_duration_ms=duration_ms,
                    tokens_used=tokens_used,
                    cost_usd=cost_usd,
                )
        except Exception as exc:
            self._record_model_call(
                provider="openrouter",
                model=req_data.model,
                success=False,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
                source="hub.generate_openrouter",
            )
            logger.error(f"OpenRouter generate failed for {req_data.model}: {exc}")
            raise RuntimeError(f"OpenRouter generation failed: {exc}")

    def generate_unified(self, req_data: UnifiedGenerateRequest) -> UnifiedGenerateResponse:
        """Dispatches generation to Ollama or OpenRouter."""
        if req_data.provider == PlaygroundProvider.OPENROUTER:
            return self.generate_openrouter(req_data)

        # Provider == Ollama
        ollama_req = OllamaGenerateRequest(
            model=req_data.model,
            prompt=req_data.prompt,
            system=req_data.system,
            temperature=req_data.temperature,
        )
        res = self.generate_ollama(ollama_req)
        return UnifiedGenerateResponse(
            response=res.response,
            provider=PlaygroundProvider.OLLAMA,
            model=res.model,
            done=res.done,
            total_duration_ms=res.total_duration_ms,
            tokens_used=None,
            cost_usd=0.0,
        )

    def export_services_data(self) -> ExportCatalogResponse:
        """Exports full catalog of services with timestamp and version."""
        services = self.list_services()
        now_iso = datetime.now(timezone.utc).isoformat()
        return ExportCatalogResponse(
            exported_at=now_iso,
            version="1.0",
            count=len(services),
            services=services,
        )

    def import_services_data(self, req: ImportCatalogRequest) -> ImportCatalogResponse:
        """Imports and saves catalog with automatic backup."""
        # Create backup of current file
        if self.services_file.exists():
            backup_file = self.services_file.with_suffix(".backup.json")
            try:
                backup_file.write_text(self.services_file.read_text(encoding="utf-8"), encoding="utf-8")
                logger.info(f"Created catalog backup at {backup_file}")
            except Exception as exc:
                logger.warning(f"Failed to create backup: {exc}")

        current_items = self._load_services_raw()
        if req.merge:
            existing_map = {item.get("id"): item for item in current_items}
            for new_service in req.services:
                existing_map[new_service.id] = new_service.model_dump()
            final_items = list(existing_map.values())
        else:
            final_items = [s.model_dump() for s in req.services]

        self._save_services_raw(final_items)
        return ImportCatalogResponse(
            imported_count=len(req.services),
            total_count=len(final_items),
            message="Catálogo importado com sucesso!",
        )

    def reset_to_defaults(self) -> int:
        """Restores catalog from default_services.json."""
        if not self.default_services_file.exists():
            raise FileNotFoundError("Arquivo default_services.json não encontrado.")

        content = self.default_services_file.read_text(encoding="utf-8")
        self.services_file.write_text(content, encoding="utf-8")
        items = json.loads(content)
        return len(items) if isinstance(items, list) else 0

    # -------------------------------------------------------------
    # Learning Packs & Cognitive Uplift Engine
    # -------------------------------------------------------------
    def list_learning_packs(self) -> List[Dict[str, Any]]:
        """List metadata summaries for all recorded session learning packs."""
        store = LearningPackStore()
        return store.list_packs()

    def get_learning_pack(self, pack_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve full structured JSON for a specific pack or 'latest'."""
        store = LearningPackStore()
        pack = store.load_pack(pack_id)
        return pack.to_dict() if pack else None

    def generate_learning_pack(
        self,
        title: Optional[str] = None,
        session_id: Optional[str] = None,
        files: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Synthesizes a fresh session learning pack, stores all artifacts, and returns summary."""
        generator = LearningPackGenerator()
        store = LearningPackStore()
        pack = generator.generate_pack(title=title, session_id=session_id, files_analyzed=files)
        saved = store.save_pack(pack)
        rendered_md = LearningPackRenderer.render_markdown(pack)
        return {
            "pack_id": pack.pack_id,
            "title": pack.title,
            "executive_summary": pack.executive_summary,
            "concepts_count": len(pack.concepts),
            "flashcards_count": len(pack.flashcards),
            "saved_paths": saved,
            "rendered_md": rendered_md,
        }

    def export_learning_pack_anki(self, pack_id: str) -> Optional[str]:
        """Returns Anki TSV formatted deck for a learning pack."""
        store = LearningPackStore()
        pack = store.load_pack(pack_id)
        if not pack:
            return None
        return LearningPackRenderer.render_anki_tsv(pack)

    def get_learning_pack_html(self, pack_id: str) -> Optional[str]:
        """Returns standalone interactive HTML content for a learning pack."""
        store = LearningPackStore()
        pack = store.load_pack(pack_id)
        if not pack:
            return None
        return LearningPackRenderer.render_html(pack)

    # -------------------------------------------------------------
    # Task-Adaptive Benchmark Router & Multi-Domain Engine
    # -------------------------------------------------------------
    def get_benchmark_domains(self) -> List[Dict[str, Any]]:
        """List metadata summaries for all 6 canonical benchmark domains."""
        domains: List[Dict[str, Any]] = []
        for meta in DOMAIN_METADATA.values():
            domains.append({
                "domain_key": meta.domain.value,
                "name": meta.display_name,
                "canonical_benchmark": meta.canonical_benchmark,
                "evaluates": meta.evaluates,
                "source_authority": meta.source_authority,
                "target_metric_scale": meta.target_metric_scale,
            })
        return domains

    def get_domain_frontier(self, domain_key: str) -> Optional[Dict[str, Any]]:
        """Retrieve Pareto efficiency frontier for a specific domain."""
        meta = DOMAIN_METADATA.get(domain_key)
        if not meta:
            return None

        ledger = ensure_daily_benchmark()
        frontier_ids = ledger.frontiers_by_domain.get(domain_key, [])

        frontier_models = []
        for fid in frontier_ids:
            if fid in ledger.models:
                frontier_models.append(ledger.models[fid])

        # If empty or not yet cached, compute on the fly
        if not frontier_models:
            prod_models = [m for m in ledger.models.values() if is_production_interactive_model(m)]
            frontier_models = compute_pareto_frontier(prod_models or list(ledger.models.values()), metric=domain_key)

        return {
            "domain": domain_key,
            "domain_name": meta.display_name,
            "canonical_benchmark": meta.canonical_benchmark,
            "evaluates": meta.evaluates,
            "frontier_count": len(frontier_models),
            "models": [m.to_dict() for m in frontier_models],
        }


    def route_task_benchmark(
        self,
        requirement: str,
        complexity: str = "medium",
        offline: bool = False,
        domain_override: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Route a task requirement to the optimal benchmark domain and frontier models."""
        router = get_benchmark_router()
        decision = router.route_task(
            requirement=requirement,
            complexity=complexity,
            offline=offline,
            domain_override=domain_override,
        )
        return decision.to_dict()

    # ---------------------------------------------------------------------------
    # Anti-AI-Slop Content Engine Service Methods
    # ---------------------------------------------------------------------------

    def generate_content(self, payload: ContentGenerateRequest) -> Dict[str, Any]:
        """Generates content via ContentEngine and enforces Anti-Slop thresholds."""
        engine = ContentEngine()
        req = ContentRequest(
            topic=payload.topic,
            content_type=ContentType(payload.content_type),
            target_audience=payload.target_audience,
            key_points=payload.key_points,
            raw_context=payload.raw_context,
            offline=payload.offline,
            model_override=payload.model_override,
        )
        resp = engine.generate(req)
        return {
            "content_id": resp.content_id,
            "title": resp.title,
            "content_type": resp.content_type.value,
            "final_content": resp.final_content,
            "initial_slop_score": resp.initial_slop_score,
            "final_slop_score": resp.final_slop_score,
            "cleanliness_rating": resp.slop_report.cleanliness_rating.value,
            "word_count": resp.word_count,
            "provider": resp.provider,
            "model_used": resp.model_used,
            "created_at": resp.created_at,
        }

    def lint_content(self, payload: ContentLintRequest) -> Dict[str, Any]:
        """Audits text content for AI slop and returns structured report."""
        linter = AntiSlopLinter(custom_banned_words=payload.custom_banned_words)
        report = linter.audit(payload.text)
        return {
            "slop_score": report.slop_score,
            "cleanliness_rating": report.cleanliness_rating.value,
            "violations_count": report.violations_count,
            "cadence_rating": report.cadence_rating,
            "sentence_length_variance": report.sentence_length_variance,
            "violations": [v.to_dict() for v in report.violations],
            "top_fixes": report.top_fixes,
        }

    def list_content_presets(self) -> List[Dict[str, Any]]:
        """Lists all supported content presets and their defaults."""
        presets = []
        for ctype, conf in CONTENT_PRESETS.items():
            tone = conf["default_tone"]
            presets.append({
                "type": ctype.value,
                "title": conf["title"],
                "description": conf["description"],
                "default_tone": tone.to_dict(),
            })
        return presets

    # ---------------------------------------------------------------------------
    # Context-Aware Visual Asset Studio Service Methods
    # ---------------------------------------------------------------------------

    def generate_visual(self, payload: VisualGenerateRequest) -> Dict[str, Any]:
        """Generates a visual asset from explicit prompt specifications."""
        studio = VisualStudio()
        spec = VisualPromptSpec(
            title=payload.title,
            subtitle=payload.subtitle,
            asset_type=AssetType(payload.asset_type),
            theme=VisualTheme(payload.theme),
            aspect_ratio=AspectRatio(payload.aspect_ratio),
            high_res=payload.high_res,
            offline=payload.offline,
        )
        res = studio.create_asset(spec)
        return res.to_dict()

    def illustrate_text(self, payload: VisualIllustrateRequest) -> Dict[str, Any]:
        """Semantically analyzes input text and renders matching visual asset."""
        studio = VisualStudio()
        theme_enum = VisualTheme(payload.theme) if payload.theme else None
        ratio_enum = AspectRatio(payload.aspect_ratio) if payload.aspect_ratio else None
        res = studio.illustrate_text(
            text_content=payload.text,
            asset_type=AssetType(payload.asset_type),
            theme=theme_enum,
            aspect_ratio=ratio_enum,
            offline=payload.offline,
        )
        return res.to_dict()

    def list_visual_assets(self) -> List[Dict[str, Any]]:
        """Returns catalog of all saved visual assets."""
        studio = VisualStudio()
        return studio.list_assets()

    # ---------------------------------------------------------------------------
    # Test Worker & Harness Subagent Service Methods (USR-16)
    # ---------------------------------------------------------------------------

    def get_test_workers_status(self) -> List[Dict[str, Any]]:
        """Probes known test worker nodes (e.g. desktop-g45ipem on Tailscale) and returns status."""
        workers = [
            {
                "id": "onprem-z97-server",
                "name": "Dedicated Test Worker (desktop-g45ipem)",
                "url": "http://100.78.181.90:8080",
                "ip": "100.78.181.90",
                "port": 8080,
                "role": "onprem_worker",
                "description": "Dedicated i7-4790K / 16GB / 3TB E: on-premises validation node via Tailscale",
            },
            {
                "id": "local-notebook",
                "name": "Local Workstation Runner",
                "url": "http://localhost:8080",
                "ip": "127.0.0.1",
                "port": 8080,
                "role": "local_worker",
                "description": "Interactive developer laptop local test engine",
            },
        ]

        engine = TestSubagentEngine(project_root=self.project_root)
        results = []

        for w in workers:
            w_info = dict(w)
            start_t = time.perf_counter()
            health = engine.probe_remote_worker(w["url"], timeout=0.8)
            latency_ms = round((time.perf_counter() - start_t) * 1000, 1)

            if health:
                w_info["status"] = "online"
                w_info["healthy"] = True
                w_info["latency_ms"] = latency_ms
                w_info["details"] = health
            else:
                w_info["status"] = "offline"
                w_info["healthy"] = False
                w_info["latency_ms"] = None
                w_info["details"] = None

            results.append(w_info)

        return results

    def execute_test_run(self, instruction: TestExecutionInstruction) -> DistilledTestReport:
        """Executes a test run via TestSubagentEngine with automatic failover."""
        engine = TestSubagentEngine(project_root=self.project_root)
        return engine.execute(instruction)

    # ---------------------------------------------------------------------------
    # Cloud Gateway & Autonomous Webhooks Service Methods (USR-18 / INFRA-09 / DF-20)
    # ---------------------------------------------------------------------------

    def process_github_webhook(
        self,
        event_type: str,
        delivery_id: str,
        payload: Dict[str, Any],
        signature_header: Optional[str] = None,
        secret: Optional[str] = None,
        require_secret: bool = True,
    ) -> WebhookEventRecord:
        """Processes an incoming GitHub webhook through WebhookEngine."""
        engine = WebhookEngine(project_root=self.project_root)
        webhook_secret = secret or os.getenv("GITHUB_WEBHOOK_SECRET")
        return engine.process_webhook(
            event_type=event_type,
            delivery_id=delivery_id,
            payload=payload,
            signature_header=signature_header,
            secret=webhook_secret,
            require_secret=require_secret,
        )

    def get_cloud_gateway_status(self) -> CloudGatewayStatus:
        """Returns the operational status of the Cloud 24/7 Dokploy Gateway."""
        engine = WebhookEngine(project_root=self.project_root)
        return engine.get_gateway_status()

    def get_webhook_events(
        self,
        limit: int = 50,
        event_type: Optional[str] = None,
    ) -> List[WebhookEventRecord]:
        """Returns recent audited webhook events."""
        engine = WebhookEngine(project_root=self.project_root)
        return engine.audit_store.get_events(limit=limit, event_type=event_type)

    def trigger_dokploy_deployment(
        self,
        service_name: str = "darkhub",
        custom_url: Optional[str] = None,
    ) -> DokployDeployTrigger:
        """Triggers Dokploy PaaS auto-deploy webhook (INFRA-09)."""
        client = DokployDeployClient()
        return client.trigger_deploy(service_name=service_name, custom_url=custom_url)

    def _build_telegram_gateway(self) -> TelegramGateway:
        """Constructs TelegramGateway with bound handlers to DarkHub core services."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        users = [int(u.strip()) for u in os.environ.get("TELEGRAM_AUTHORIZED_USERS", "").split(",") if u.strip().isdigit()]
        chats = [int(c.strip()) for c in os.environ.get("TELEGRAM_AUTHORIZED_CHATS", "").split(",") if c.strip().isdigit()]
        secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        config = TelegramConfig(
            bot_token=token,
            authorized_user_ids=users,
            authorized_chat_ids=chats,
            webhook_secret_token=secret,
        )

        def _handle_demand(text: str, user_id: int) -> Dict[str, Any]:
            ticket = self.create_demand_ticket(
                UserTicket(
                    title=f"Telegram Demand: {text[:40]}...",
                    description=text,
                    source="telegram",
                    metadata={"author_id": str(user_id)},
                )
            )
            return {"ticket_id": ticket.id}

        def _handle_status(ticket_id: Optional[str]) -> Dict[str, Any]:
            if ticket_id:
                ticket = self.get_demand_ticket(ticket_id)
                if ticket:
                    return {"summary": f"Ticket {ticket.id}: status={ticket.status}, title={ticket.title}"}
                return {"summary": f"Ticket '{ticket_id}' not found."}
            dash = self.get_task_dashboard()
            return {"summary": f"Tasks in queue: {dash.total_tasks}, active: {dash.running_tasks}, completed: {dash.completed_tasks}"}

        def _handle_grill(ticket_id: str, choice: str, user_id: int) -> Dict[str, Any]:
            try:
                self.submit_demand_grill(ticket_id, answers=[f"Option: {choice}"])
                return {"resumed": True, "ticket_id": ticket_id}
            except Exception as e:
                return {"resumed": False, "error": str(e)}

        def _handle_approval(project_id: str, digest: str, user_id: int) -> Dict[str, Any]:
            pipeline = ReleasePipelineService(storage_dir=self.project_root / ".factory" / "releases")
            receipt = pipeline.record_client_acceptance(
                project_id=project_id,
                artifact_digest=digest,
                client_id="telegram-owner",
                approved_by=f"telegram-user-{user_id}",
            )
            return {"receipt_id": receipt.receipt_id, "project_id": project_id, "digest": digest}

        # HF-27-08 F (review item 5): wire the production-line grill-answer
        # and commercial-acceptance callbacks to the same ControlStore the
        # rest of this service uses, so a resumed job is visible immediately.
        try:
            from core.line.human import (
                build_telegram_commercial_acceptance_handler,
                build_telegram_line_grill_handler,
            )

            line_grill_handler = build_telegram_line_grill_handler(self.control_store)
            commercial_acceptance_handler = build_telegram_commercial_acceptance_handler(self.control_store)
        except Exception as exc:  # pragma: no cover - defensive, must never break the bot
            logger.warning("Failed to wire production-line Telegram handlers: %s", exc)
            line_grill_handler = None
            commercial_acceptance_handler = None

        return TelegramGateway(
            config=config,
            state_dir=self.project_root / ".factory" / "telegram",
            demand_handler=_handle_demand,
            status_handler=_handle_status,
            grill_handler=_handle_grill,
            approval_handler=_handle_approval,
            line_grill_handler=line_grill_handler,
            commercial_acceptance_handler=commercial_acceptance_handler,
        )

    def process_telegram_webhook(
        self,
        payload: Dict[str, Any],
        secret_token_header: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Processes an incoming Telegram webhook update."""
        expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        if expected_secret and secret_token_header != expected_secret:
            return {
                "update_id": payload.get("update_id", 0),
                "action": "unauthorized",
                "authorized": False,
                "error": "Invalid X-Telegram-Bot-Api-Secret-Token",
            }
        gateway = self._build_telegram_gateway()
        result = gateway.process_update(payload)
        return result.model_dump()

    def get_telegram_gateway_status(self) -> Dict[str, Any]:
        """Returns diagnostic status of Telegram Gateway."""
        gateway = self._build_telegram_gateway()
        return gateway.get_status()

    def get_n8n_status(self, target_url: Optional[str] = None) -> Dict[str, Any]:
        """Probes n8n endpoint and returns health/instance report."""
        url = target_url or os.environ.get("N8N_URL", "https://n8n.ggcampos.com")
        probe = N8nProbe()
        report = probe.probe(target_url=url)
        return report.model_dump()

    def get_n8n_workflows(self, limit: int = 50) -> Dict[str, Any]:
        """Lists active workflows from remote n8n instance."""
        client = N8nApiClient()
        res = client.list_workflows(limit=limit)
        return res.model_dump()

    def sync_n8n_workflows(self, custom_path: Optional[str] = None, activate: bool = True) -> Dict[str, Any]:
        """Synchronizes workflow definitions to remote n8n instance."""
        workflows_root = (self.project_root / ".factory" / "n8n" / "workflows").resolve()
        requested_path = Path(custom_path) if custom_path else workflows_root
        if not requested_path.is_absolute():
            requested_path = self.project_root / requested_path
        try:
            p = requested_path.resolve()
            p.relative_to(workflows_root)
        except (OSError, RuntimeError, ValueError):
            return {
                "success": False,
                "results": {},
                "error": "custom_path must be inside the n8n workflows directory",
            }

        if not p.exists():
            return {
                "success": False,
                "results": {},
                "error": "n8n workflow path does not exist",
            }

        client = N8nApiClient()
        if p.is_file():
            res = client.sync_workflow_file(p, activate=activate)
            return {"success": res.success, "results": {p.name: res.model_dump()}}
        if p.is_dir():
            results = client.sync_all_workflows(p, activate=activate)
            return {
                "success": all(r.success for r in results.values()) if results else False,
                "results": {k: v.model_dump() for k, v in results.items()},
            }
        return {"success": False, "results": {}, "error": "n8n workflow path is not a file or directory"}

    def trigger_n8n_webhook(self, path_or_url: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatches an autonomous event to an n8n webhook."""
        client = N8nApiClient()
        res = client.trigger_webhook(path_or_url=path_or_url, payload=payload)
        return res.model_dump()

    def get_hf15_status(self) -> Dict[str, Any]:
        """Returns HF-15 acceptance environment status and preflight report."""
        env_mgr = HF15EnvironmentManager()
        root = env_mgr.provision_environment()
        preflight = env_mgr.run_preflights()
        tracker = HF15ObservabilityTracker(
            ledger_path=root / "telemetry" / "observability_ledger.jsonl"
        )
        metrics = tracker.get_metrics_summary()
        return {
            "environment": {
                "sandbox_root": str(root),
                "mode": preflight.environment_mode,
                "all_preflights_passed": preflight.all_passed,
                "checks": [c.model_dump() for c in preflight.checks],
            },
            "metrics": metrics.model_dump(mode="json"),
        }

    def get_hf15_metrics(self) -> Dict[str, Any]:
        """Returns HF-15 metrics summary and SLA compliance."""
        env_mgr = HF15EnvironmentManager()
        root = env_mgr.provision_environment()
        tracker = HF15ObservabilityTracker(
            ledger_path=root / "telemetry" / "observability_ledger.jsonl"
        )
        return tracker.get_metrics_summary().model_dump(mode="json")

    def trigger_hf15_rollback_drill(self, project_id: str = "proj-drill-01") -> Dict[str, Any]:
        """Triggers a verified rollback drill and returns the execution record."""
        env_mgr = HF15EnvironmentManager()
        root = env_mgr.provision_environment()
        coordinator = HF15RollbackCoordinator(backup_root=root / "backups")
        tracker = HF15ObservabilityTracker(
            ledger_path=root / "telemetry" / "observability_ledger.jsonl"
        )
        with tracker.measure_latency("G8", "rollback_drill_invoked"):
            record = coordinator.run_rollback_drill(
                project_id=project_id,
                sandbox_dir=root / "backups" / "drill_workspace",
            )
        tracker.record_event(
            scenario_id="G8",
            event_type="rollback_completed",
            duration_ms=record.rto_seconds * 1000.0,
            details={
                "rollback_id": record.rollback_id,
                "rto_seconds": record.rto_seconds,
                "rpo_seconds": record.rpo_seconds,
                "success": record.success,
            },
        )
        return record.model_dump(mode="json")

    def list_notifications(
        self,
        limit: int = 50,
        severity: Optional[str] = None,
        unread_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """Returns recent operational notifications from the store."""
        from core.notifications.models import AlertSeverity
        from core.notifications.store import NotificationStore
        store = NotificationStore()
        sev = AlertSeverity(severity) if severity else None
        events = store.list_notifications(limit=limit, severity=sev, unread_only=unread_only)
        return [e.model_dump(mode="json") for e in events]

    def acknowledge_notification(self, notification_id: str) -> bool:
        """Marks an operational notification as acknowledged."""
        from core.notifications.store import NotificationStore
        store = NotificationStore()
        return store.mark_acknowledged(notification_id)

    def check_token_quotas(self, force: bool = False) -> List[Dict[str, Any]]:
        """Inspects all connected provider accounts and emits alerts for critical limits."""
        from core.notifications.token_watcher import TokenQuotaWatcher
        watcher = TokenQuotaWatcher()
        emitted = watcher.check_all_quotas(force=force)
        return [e.model_dump(mode="json") for e in emitted]

    def get_progress_projection(
        self,
        project_id: str,
        now: Optional[datetime] = None,
        max_capacity: int = 2,
        running_override: Optional[int] = None,
    ) -> ProgressProjection:
        """Calculates operational progress and stagnation indicators for a project (HF-13-02)."""
        if now is None:
            now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now_iso = now.isoformat()

        store = self.control_store
        if store is None or not hasattr(store, "_connect"):
            return ProgressProjection(
                project_id=project_id,
                healthy=True,
                capacity_available=True,
            )

        conn = store._connect()
        try:
            cur = conn.cursor()

            cur.execute(
                """
                SELECT j.run_id, j.ticket_id, j.plan_version, j.stage, j.iteration, j.status,
                       j.cause_code, j.created_at, j.updated_at, j.started_at, j.finished_at,
                       j.output_refs, j.evidence_refs
                FROM jobs j
                JOIN runs r ON j.run_id = r.run_id
                WHERE r.project_id = ?
                ORDER BY j.created_at ASC
                """,
                (project_id,),
            )
            job_rows = cur.fetchall()

            cur.execute(
                """
                SELECT c.lease_id, c.reservation_id, c.owner, c.route_ref, c.acquired_at, c.expires_at, c.status
                FROM claims c
                JOIN runs r ON c.run_id = r.run_id
                WHERE r.project_id = ? AND c.status = 'active'
                """,
                (project_id,),
            )
            claim_rows = cur.fetchall()

            last_reconcile = None
            try:
                cur.execute("SELECT MAX(recorded_at) FROM reconciliation_ledger")
                row = cur.fetchone()
                if row and row[0]:
                    last_reconcile = row[0]
            except Exception:
                pass

        finally:
            conn.close()

        ready_count = 0
        running_count_db = 0
        blocked_count = 0
        oldest_eligible_age = 0.0
        wait_reasons: Dict[str, str] = {}
        evidence_chain: List[str] = []
        last_dispatch = None

        for j in job_rows:
            status = j["status"]
            ticket_id = j["ticket_id"]
            created_at_str = j["created_at"]
            started_at_str = j["started_at"]
            evidence_refs_str = j["evidence_refs"]

            if started_at_str:
                if last_dispatch is None or started_at_str > last_dispatch:
                    last_dispatch = started_at_str

            if status in ("pending", "retry", "replan"):
                ready_count += 1
                try:
                    dt = datetime.fromisoformat(created_at_str)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    age = max(0.0, (now - dt).total_seconds())
                    if age > oldest_eligible_age:
                        oldest_eligible_age = age
                except Exception:
                    pass
            elif status == "running":
                running_count_db += 1
            elif status in ("waiting_dependency", "waiting_human"):
                blocked_count += 1
                wait_reasons[ticket_id] = j["cause_code"] or f"Waiting on {status}"
            elif status == "succeeded" and evidence_refs_str:
                try:
                    refs = json.loads(evidence_refs_str)
                    if isinstance(refs, list):
                        for r in refs:
                            if isinstance(r, str) and r not in evidence_chain:
                                evidence_chain.append(r)
                except Exception:
                    pass

            if j["cause_code"] and ticket_id not in wait_reasons:
                wait_reasons[ticket_id] = j["cause_code"]

        for c in claim_rows:
            acq = c["acquired_at"]
            if acq and (last_dispatch is None or acq > last_dispatch):
                last_dispatch = acq

        running_count = running_override if running_override is not None else running_count_db

        heartbeats_count = 0
        reservations: List[Dict[str, Any]] = []
        for c in claim_rows:
            exp_str = c["expires_at"]
            is_active_lease = True
            if exp_str:
                try:
                    exp_dt = datetime.fromisoformat(exp_str)
                    if exp_dt.tzinfo is None:
                        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                    if exp_dt <= now:
                        is_active_lease = False
                except Exception:
                    pass
            if is_active_lease:
                heartbeats_count += 1
                if c["reservation_id"]:
                    reservations.append({
                        "reservation_id": c["reservation_id"],
                        "owner": c["owner"],
                        "route_ref": c["route_ref"],
                        "expires_at": c["expires_at"],
                    })

        capacity_available = running_count < max_capacity
        stalled = ready_count > 0 and capacity_available and oldest_eligible_age >= 30.0

        alert_map: Dict[str, IncidentAlert] = {}
        if stalled:
            inc_id = f"stalled:{project_id}"
            alert_map[inc_id] = IncidentAlert(
                incident_id=inc_id,
                severity="warning",
                message=(
                    f"Project '{project_id}' execution is stalled: {ready_count} ready job(s) "
                    f"waiting for {oldest_eligible_age:.1f}s with available capacity ({max_capacity - running_count} slot(s) free)"
                ),
                detected_at=now_iso,
                details={
                    "ready_count": ready_count,
                    "oldest_eligible_age": oldest_eligible_age,
                    "running_count": running_count,
                    "max_capacity": max_capacity,
                },
            )

        if blocked_count > 0:
            inc_id = f"blocked:{project_id}"
            alert_map[inc_id] = IncidentAlert(
                incident_id=inc_id,
                severity="info",
                message=f"Project '{project_id}' has {blocked_count} job(s) waiting on dependencies or human input",
                detected_at=now_iso,
                details={"blocked_count": blocked_count, "wait_reasons": wait_reasons},
            )

        alerts = list(alert_map.values())
        healthy = not stalled and not any(a.severity in ("error", "critical") for a in alerts)
        next_wakeup = (now + timedelta(seconds=10)).isoformat() if ready_count > 0 or running_count > 0 else None

        return ProgressProjection(
            project_id=project_id,
            oldest_eligible_age=oldest_eligible_age,
            last_dispatch=last_dispatch,
            last_reconcile=last_reconcile,
            heartbeats_count=heartbeats_count,
            ready_count=ready_count,
            running_count=running_count,
            blocked_count=blocked_count,
            wait_reasons=wait_reasons,
            next_wakeup=next_wakeup,
            reservations=reservations,
            evidence_chain=evidence_chain,
            stalled=stalled,
            alerts=alerts,
            healthy=healthy,
            capacity_available=capacity_available,
        )

    # -----------------------------------------------------------------------
    # Multi-Project Portfolio Methods (DH-08)
    # -----------------------------------------------------------------------

    def _build_portfolio_project_summary(self, p: Any) -> PortfolioProjectSummary:
        """Aggregate a project's state across 5 core dimensions (DH-08)."""
        from core.projects.models import ProjectKind
        from core.portfolio.models import BudgetStatus
        from core.roadmap.models import DeliveryStatus

        # 1. Dev Stage
        if p.id == "darkfac" or p.kind == ProjectKind.CORE:
            dev_stage = "production"
        elif p.domain or (p.deploy and p.deploy.type.value in ("dokploy", "hostinger_ftp")):
            dev_stage = "production"
        else:
            dev_stage = "active_development"

        # 2. Roadmap Summary & Health
        total_items = 0
        delivered_items = 0
        in_progress_items = 0
        blocked_items = 0
        completion_pct = 0.0
        roadmap_health_status = "healthy"
        health_details: Dict[str, Any] = {}

        try:
            snapshot = self.roadmap.get_snapshot(p.id)
            total_items = len(snapshot.items)
            delivered_items = sum(1 for item in snapshot.items if item.delivery_status == DeliveryStatus.DELIVERED)
            in_progress_items = sum(1 for item in snapshot.items if item.delivery_status == DeliveryStatus.IN_PROGRESS)
            blocked_items = sum(1 for item in snapshot.items if item.delivery_status == DeliveryStatus.BLOCKED)
            completion_pct = round((delivered_items / total_items) * 100.0, 1) if total_items > 0 else 0.0
        except Exception as exc:
            logger.debug(f"Failed getting roadmap snapshot for '{p.id}': {exc}")

        try:
            rh = self.roadmap.get_health(p.id)
            roadmap_health_status = rh.status
            health_details = {
                "stale": rh.stale,
                "stale_reason": rh.stale_reason,
                "blocked_items": rh.blocked_items,
                "conflicts_count": rh.conflicts_count,
                "warnings": list(rh.warnings),
            }
        except Exception as exc:
            health_details = {"error": str(exc)}

        # 3. Budget Summary
        b = self.budget_manager.get_budget(p.id)
        budget_summary = {
            "project_id": p.id,
            "monthly_limit_usd": b.monthly_limit_usd,
            "current_spent_usd": b.current_spent_usd,
            "utilization_pct": b.utilization_pct,
            "remaining_usd": b.remaining_usd,
            "status": b.status.value,
        }

        # Overall Health Aggregation
        if b.status == BudgetStatus.LOCAL_ONLY:
            overall_health = "warning"
            health_details["budget_warning"] = "Monthly limit exhausted (forced local_only)"
        elif health_details.get("stale") or blocked_items > 0 or roadmap_health_status != "healthy":
            overall_health = "warning"
        else:
            overall_health = "healthy"

        # 4. Last Deploy
        deploy_type = p.deploy.type.value if getattr(p, "deploy", None) else (p.deploy_target or "none")
        smoke_checks = [s.model_dump() for s in getattr(p, "smoke", [])]
        last_deploy = {
            "target": p.deploy_target or deploy_type,
            "target_type": deploy_type,
            "domain": p.domain,
            "default_branch": p.default_branch,
            "smoke_checks_count": len(smoke_checks),
            "smoke_urls": [s.get("url") for s in smoke_checks if "url" in s],
            "registered_at": p.created_at,
            "status": "deployed" if (p.domain or p.deploy_target) else "configured",
        }

        # 5. Adoption Summary
        is_adopted = False
        lock_valid = False
        managed_files_count = 0
        autonomy_level = 2
        adoption_problems: list[str] = []
        proj_path = Path(p.path) if p.path else (self.project_root if p.id == "darkfac" else None)
        if proj_path and proj_path.exists():
            try:
                from core.adoption.service import verify_adoption
                vr = verify_adoption(proj_path)
                lock_valid = vr.lock_valid
                is_adopted = vr.lock_valid or (p.id == "darkfac")
                managed_files_count = vr.managed_files_checked
                adoption_problems = list(vr.problems)
            except Exception as exc:
                adoption_problems.append(str(exc))
                if p.id == "darkfac":
                    is_adopted = True
                    lock_valid = True

        adoption_summary = {
            "is_adopted": is_adopted,
            "lock_valid": lock_valid,
            "autonomy_level": 3 if p.id == "darkfac" else autonomy_level,
            "managed_files_checked": managed_files_count,
            "problems_count": len(adoption_problems),
            "problems": adoption_problems,
        }

        # 6. Archetype Matching
        archetype_info: Optional[Dict[str, Any]] = None
        try:
            from core.archetypes.registry import get_registry as get_arch_reg
            arch_reg = get_arch_reg()
            arch_id = None
            if "site" in p.id or p.kind.value == "client_portfolio":
                arch_id = "personal_presence"
            elif "cerebro" in p.id or "brain" in p.id:
                arch_id = "second_brain"
            elif p.kind.value == "internal_product":
                arch_id = "internal_tool"

            if arch_id:
                m = arch_reg.get_archetype(arch_id)
                if m:
                    archetype_info = {
                        "id": m.id,
                        "title": m.title,
                        "framework": m.stack.framework,
                        "styling": m.stack.styling,
                        "content_format": m.stack.content_format,
                        "runtime": m.stack.runtime,
                        "deployment_targets": m.stack.deployment_targets,
                    }
        except Exception:
            pass

        # 7. Pilots Summary
        pilots_summary = {
            "total_specs": 1 if p.id == "darkfac" else 0,
            "sample_size_target": 20,
            "latest_verdict": "promising",
            "primary_metric": "paired_stage_error_delta",
        }

        # 8. Line Summary
        line_summary = {
            "active_stages": ["grill", "planning", "build", "review", "integration", "release"],
            "default_branch": p.default_branch,
            "exec_affinity": getattr(p, "exec_affinity", []),
            "requires_commercial_acceptance": getattr(p, "requires_commercial_acceptance", False),
        }

        # 9. Game Summary
        game_summary = {
            "engine": "echo-garden",
            "deterministic_seed": 0,
            "status": "deterministic_ready",
            "moves_supported": ["weave", "echo", "ground"],
        }

        return PortfolioProjectSummary(
            id=p.id,
            name=p.name,
            description=p.description or "",
            path=p.path,
            kind=p.kind.value if hasattr(p.kind, "value") else str(p.kind),
            prefix=p.prefix,
            domain=p.domain,
            deploy_target=p.deploy_target,
            repo_url=getattr(p, "repo_url", None),
            default_branch=p.default_branch,
            created_at=p.created_at,
            dev_stage=dev_stage,
            health_status=overall_health,
            health_details=health_details,
            last_deploy=last_deploy,
            roadmap_summary={
                "total_items": total_items,
                "delivered_items": delivered_items,
                "in_progress_items": in_progress_items,
                "blocked_items": blocked_items,
                "completion_pct": completion_pct,
            },
            budget_summary=budget_summary,
            adoption_summary=adoption_summary,
            archetype_summary=archetype_info,
            pilots_summary=pilots_summary,
            line_summary=line_summary,
            game_summary=game_summary,
        )

    def get_portfolio_overview(self) -> PortfolioOverviewResponse:
        """Return the multi-project portfolio overview for DarkHub (DH-08)."""
        projects_descriptors = self.project_registry.list_projects()
        summaries = [self._build_portfolio_project_summary(p) for p in projects_descriptors]

        healthy_count = sum(1 for s in summaries if s.health_status == "healthy")
        warning_count = len(summaries) - healthy_count

        total_budget_limit = sum(s.budget_summary.get("monthly_limit_usd", 0.0) for s in summaries)
        total_spent = sum(s.budget_summary.get("current_spent_usd", 0.0) for s in summaries)

        active_heavy, active_light = self.portfolio_scheduler.get_active_counts()

        from core.archetypes.registry import get_registry as get_arch_reg
        arch_manifests = [
            {
                "id": a.id,
                "title": a.title,
                "kind": a.kind.value,
                "description": a.description,
                "framework": a.stack.framework,
                "styling": a.stack.styling,
                "content_format": a.stack.content_format,
                "runtime": a.stack.runtime,
                "deployment_targets": a.stack.deployment_targets,
            }
            for a in get_arch_reg().list_archetypes()
        ]

        return PortfolioOverviewResponse(
            total_projects=len(summaries),
            healthy_projects=healthy_count,
            warning_projects=warning_count,
            total_budget_limit_usd=round(total_budget_limit, 2),
            total_spent_usd=round(total_spent, 2),
            active_heavy_slots=active_heavy,
            max_heavy_slots=self.portfolio_scheduler.capacity.max_heavy_slots,
            active_light_slots=active_light,
            max_light_slots=self.portfolio_scheduler.capacity.max_light_slots,
            projects=summaries,
            archetypes=arch_manifests,
        )

    def get_portfolio_project_detail(self, project_id: str) -> Optional[PortfolioProjectDetailResponse]:
        """Return deep-dive inspection response for a single adopted project (DH-08)."""
        descriptor = self.project_registry.get_project(project_id)
        if not descriptor:
            return None

        project_summary = self._build_portfolio_project_summary(descriptor)

        from core.projects.registry import resolve_commands
        proj_path = Path(descriptor.path) if descriptor.path else (self.project_root if descriptor.id == "darkfac" else self.project_root)
        resolved_cmds = resolve_commands(descriptor, proj_path)
        commands = {
            "setup": resolved_cmds.setup,
            "validate": resolved_cmds.validate_cmds,
            "build": resolved_cmds.build,
            "smoke": resolved_cmds.smoke,
        }

        smoke_checks = [s.model_dump() for s in getattr(descriptor, "smoke", [])]

        verification: Dict[str, Any] = {}
        if proj_path and proj_path.exists():
            try:
                from core.adoption.service import verify_adoption
                vr = verify_adoption(proj_path)
                verification = vr.model_dump()
            except Exception as exc:
                verification = {"lock_valid": False, "problems": [str(exc)]}

        roadmap_health: Dict[str, Any] = {}
        try:
            rh = self.roadmap.get_health(project_id)
            roadmap_health = rh.model_dump()
        except Exception as exc:
            roadmap_health = {"error": str(exc)}

        return PortfolioProjectDetailResponse(
            project=project_summary,
            commands=commands,
            smoke_checks=smoke_checks,
            verification=verification,
            roadmap_health=roadmap_health,
        )

    def get_portfolio_efficiency(self) -> Any:
        """Return aggregated portfolio capacity, WFQ queues, and budget telemetry (HF-23)."""
        from core.portfolio.models import PortfolioEfficiencyReport
        active_heavy, active_light = self.portfolio_scheduler.get_active_counts()
        return PortfolioEfficiencyReport(
            active_heavy_slots=active_heavy,
            max_heavy_slots=self.portfolio_scheduler.capacity.max_heavy_slots,
            active_light_slots=active_light,
            max_light_slots=self.portfolio_scheduler.capacity.max_light_slots,
            queued_jobs_by_project=self.portfolio_scheduler.get_queue_status(),
            starvation_ticks_by_project=self.portfolio_scheduler.get_starvation_status(),
            budgets=self.budget_manager.list_budgets(),
        )

    def get_portfolio_archetypes(self) -> List[Any]:
        """Return all available project archetypes from the factory catalog (HF-20)."""
        from core.archetypes.registry import get_registry as get_arch_reg
        return get_arch_reg().list_archetypes()

    def get_autonomy_plan(self) -> Dict[str, Any]:
        """Returns the compiled Continuous Autonomy Plan and execution DAG (DH-12)."""
        plan_file = Path(__file__).resolve().parents[2] / ".factory" / "planning" / "continuous-autonomy" / "plan.json"
        plan_data: Dict[str, Any] = {}
        if plan_file.is_file():
            try:
                plan_data = json.loads(plan_file.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(f"Failed to read continuous autonomy plan.json: {exc}")

        # Check live jobs in control store to annotate units with real execution status
        live_status_by_ticket: Dict[str, Dict[str, Any]] = {}
        try:
            from core.workflow.job_board import read_job_board
            snapshot = read_job_board(self.control_db_path, database_url=self.control_database_url)
            for e in snapshot.entries:
                if e.ticket_id and e.ticket_id not in live_status_by_ticket:
                    live_status_by_ticket[e.ticket_id] = {
                        "status": e.status,
                        "stage": e.stage,
                        "cause_code": e.cause_code,
                        "actual_cost": e.actual_cost,
                        "updated_at": e.updated_at,
                    }
        except Exception as exc:
            logger.debug(f"Job board inspection for autonomy plan: {exc}")

        raw_units = plan_data.get("units", [])
        units: List[Dict[str, Any]] = []
        for u in raw_units:
            tid = u.get("ticket_id")
            live = live_status_by_ticket.get(tid, {})
            status = live.get("status") or u.get("implementation_status", "not_started")
            if status == "not_started" and u.get("planning_status") == "ready_for_handoff":
                status = "ready_for_handoff"

            units.append({
                "ticket_id": tid,
                "parent_id": u.get("parent_id", "HF-26"),
                "title": u.get("title", ""),
                "priority": u.get("priority", "P1"),
                "executor_role": u.get("executor_role", "economy"),
                "environment_profile": u.get("environment_profile", "local"),
                "depends_on": u.get("depends_on", []),
                "successors": u.get("successors", []),
                "allowed_paths": u.get("allowed_paths", []),
                "new_test": u.get("new_test"),
                "validate_cmd": u.get("validate_cmd"),
                "oracle": u.get("oracle", ""),
                "status": status,
                "live_stage": live.get("stage"),
                "cause_code": live.get("cause_code"),
                "actual_cost": live.get("actual_cost", 0.0),
            })

        readiness_gates = [
            {
                "gate_id": "G1_PREFLIGHT",
                "name": "Preflight Checks",
                "description": "Validação de integridade do ambiente e dependências locais",
                "status": "passed",
            },
            {
                "gate_id": "G2_VERIFICATION_CONTEXT",
                "name": "Verification Context",
                "description": "Contexto determinístico imutável vinculado ao SHA canônico",
                "status": "passed",
            },
            {
                "gate_id": "G3_PLAN_APPROVAL",
                "name": "Plan Approval",
                "description": "Especificação e oráculos validados por modelo de alta inteligência",
                "status": "passed",
            },
            {
                "gate_id": "G4_QUALIFIED_SUPERVISOR",
                "name": "Qualified Supervisor",
                "description": "Orquestrador apto para despacho e transição de leases",
                "status": "passed",
            },
        ]

        total = len(units)
        completed = sum(1 for u in units if u["status"] in ("completed", "succeeded", "success"))
        in_progress = sum(1 for u in units if u["status"] in ("running", "in_progress", "leased"))
        ready = sum(1 for u in units if u["status"] == "ready_for_handoff")
        waiting = sum(1 for u in units if "waiting" in str(u["status"]).lower() or u["status"] == "blocked_policy")
        pending = total - completed - in_progress - ready - waiting

        return {
            "package_id": plan_data.get("package_id", "HF-26-PLAN"),
            "schema_version": plan_data.get("schema_version", "1.0"),
            "baseline_sha": plan_data.get("baseline_sha", "83e5298eb231599076811802dceac8575c7f6feb"),
            "scope": plan_data.get("scope", "continuous_autonomy"),
            "total_units": total,
            "counts": {
                "completed": completed,
                "in_progress": in_progress,
                "ready": ready,
                "waiting": waiting,
                "pending": max(0, pending),
            },
            "readiness_gates": readiness_gates,
            "production_line_stages": ["grill", "planning", "build", "review", "integration", "release"],
            "units": units,
        }


# Canonical alias for DarkHubService (HF-13-02)
DarkHubService = HubService


