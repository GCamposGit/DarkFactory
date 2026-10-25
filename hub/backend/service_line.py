"""Hub service domain: autonomous line, task dashboard, demands/grill and test workers."""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

from hub.backend.models import (
    TaskDashboardEvidence,
    TaskDashboardItem,
    TaskDashboardReport,
    LineStatusResponse,
)
from core.paths import state_root
from core.audio.engine import TranscriptionResult
from core.roadmap.models import (
    DeliveryStatus,
)
from core.demands.models import (
    DemandInput,
    DemandSpecificationGuidance,
    GrillRefinementResult,
    GrillSession,
    UserTicket,
)
from core.harness.test_subagent import (
    DistilledTestReport,
    TestExecutionInstruction,
    TestSubagentEngine,
)
from core.workflow.job_board import GHOST_OPEN_JOB_CAUSE, JobBoardEntry, is_ghost_open_job, read_job_board




logger = logging.getLogger("darkhub.service")


class LineDemandsMixin:

    def submit_ticket_to_line(self, ticket_id: str) -> Any:
        """Push an existing demands.json ticket into the autonomous production line."""
        from core.line.owner_intake import submit_ticket_to_line

        try:
            store = self._line_control_store()
        except Exception as exc:
            from core.line.owner_intake import LineSubmission

            return LineSubmission(ok=False, ticket_id=ticket_id, message=f"Store da linha indisponivel: {exc}")
        return submit_ticket_to_line(ticket_id, demands_store=self.demands_store, store=store)


    def cancel_line_run(self, target: str, user_id: int = 0, reason: str = "owner_cancelled") -> Any:
        """Cancel an in-flight production line run by ticket_id or run_id (USR-123)."""
        from core.line.owner_intake import CancelResult, cancel_line_run

        try:
            store = self._line_control_store()
        except Exception as exc:
            return CancelResult(ok=False, target=target, message=f"Store da linha indisponivel: {exc}")
        return cancel_line_run(
            target,
            demands_store=self.demands_store,
            store=store,
            user_id=user_id,
            reason=reason,
        )


    def set_autonomous_intake_service(self, service: Any) -> None:
        """Inject an AutonomousIntakeService instance (HF-08-02)."""
        self._autonomous_intake_service = service


    def get_autonomous_intake_service(self) -> Any:
        """Get or initialize the autonomous intake service with ControlStore (HF-08-02)."""
        if self._autonomous_intake_service is None:
            from core.demands.autonomous_intake import AutonomousIntakeService
            from core.workflow.control_contracts import StoreUnavailableError

            try:
                store = self._line_control_store()
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
        return self._dispatch_test_run(self.test_subagent_engine, instruction)


    @staticmethod
    def _dispatch_test_run(engine: TestSubagentEngine, instruction: TestExecutionInstruction) -> DistilledTestReport:
        """USR-218: remote Hub dispatches to an online worker; no worker -> explicit 503."""
        from fastapi import HTTPException

        from hub.backend.test_dispatch import NoTestWorkerAvailable, dispatch_test_run

        try:
            return dispatch_test_run(engine, instruction)
        except NoTestWorkerAvailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc


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


    def get_line_status(self) -> LineStatusResponse:
        """Real-time status of the autonomous production line (USR-93)."""
        from core.line.canary import REPORTS_DIR, green_streak, load_reports
        from core.line.acceptance import ACCEPTANCE_JSON

        streak = 0
        last_canary: dict[str, Any] | None = None
        live_criteria: dict[str, Any] = {}
        try:
            # USR-215: the streak and V1/V2 come from the control store (same source as the live
            # board); the canary report files only exist on the host that ran the canary.
            from core.line.acceptance import verify_v1, verify_v2

            line_store = self._line_control_store()
            v2 = verify_v2(store=line_store)
            streak = int(v2.details.get("streak", 0) or 0)
            if v2.details.get("last_date"):
                last_canary = {"date": v2.details["last_date"], "source": "control_store"}
            live_criteria["V2"] = v2.model_dump(mode="json")
            live_criteria["V1"] = verify_v1(store=line_store).model_dump(mode="json")
        except Exception as exc:
            logger.warning("Failed to load canary streak from control store for line status: %s", exc)
        if not live_criteria:
            try:
                reports = load_reports(REPORTS_DIR, limit=10)
                streak = green_streak(reports)
                if reports:
                    last_canary = reports[-1].model_dump(mode="json")
            except Exception as exc:
                logger.warning("Failed to load canary streak for line status: %s", exc)

        active_runs: list[dict[str, Any]] = []
        try:
            store = self._line_control_store()
            conn = getattr(store, "_connect", None)
            if conn is not None:
                with conn() as c:
                    rows = c.execute(
                        "SELECT run_id, demand_id, status, current_stage, created_at, updated_at "
                        "FROM runs WHERE status IN ('active', 'running', 'leased', 'in_progress', 'waiting_human')"
                    ).fetchall()
                    for r in rows:
                        active_runs.append({
                            "run_id": r[0],
                            "demand_id": r[1],
                            "status": r[2],
                            "current_stage": r[3],
                            "created_at": r[4],
                            "updated_at": r[5],
                        })
        except Exception as exc:
            logger.warning("Failed to query active runs for line status: %s", exc)

        nodes: list[dict[str, Any]] = []
        try:
            cards_report = self.get_infra_cards_report(probe_liveness=False)
            nodes = [card.model_dump(mode="json") for card in cards_report.cards]
        except Exception as exc:
            logger.warning("Failed to load topology nodes for line status: %s", exc)

        acceptance_data: dict[str, Any] | None = None
        try:
            if ACCEPTANCE_JSON.exists():
                acceptance_data = json.loads(ACCEPTANCE_JSON.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to load acceptance report for line status: %s", exc)
        if live_criteria:
            acceptance_data = dict(acceptance_data or {})
            merged = {str(c.get("id")): c for c in acceptance_data.get("criteria", []) if isinstance(c, dict)}
            merged.update(live_criteria)
            acceptance_data["criteria"] = [merged[key] for key in sorted(merged)]

        return LineStatusResponse(
            canary_streak=streak,
            last_canary_report=last_canary,
            active_runs_count=len(active_runs),
            active_runs=active_runs,
            nodes=nodes,
            acceptance=acceptance_data,
        )


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
        ghost = is_ghost_open_job(entry.run_status, entry.status)
        if entry.needs_attention and not ghost:
            reason = entry.cause_code or "sem cause_code registrado"
            exceptions.append(f"{entry.status}: {reason} (tentativa {entry.retry_count}/{entry.max_retries})")
        status = (entry.run_status if ghost else entry.status).upper()
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
            "cause_code": GHOST_OPEN_JOB_CAUSE if ghost else entry.cause_code,
            "diagnostic": (
                "Job aberto em run ja encerrado; nao exige decisao do owner"
                if ghost
                else entry.diagnostic
            ),
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
        
        # Disparo automático de notificação no Telegram quando entrar em estado de Grill pendente (USR-58)
        tags_lower = [t.lower() for t in saved.tags]
        if "grill-pending" in tags_lower or "needs-grill" in tags_lower or "grill" in tags_lower:
            try:
                self.notify_pending_grill(saved.id)
            except Exception as exc:
                logger.warning("Failed auto-dispatching grill notification for %s: %s", saved.id, exc)

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


    async def transcribe_audio_request(
        self,
        request: Any,
        language: Optional[str] = "pt",
    ) -> TranscriptionResult:
        """Transcribe uploaded audio data through the hybrid audio engine without multipart requirement (USR-60)."""
        import uuid
        from core.audio.engine import AudioTranscriptionEngine, get_audio_config

        cfg = get_audio_config()
        cfg.audio_upload_dir.mkdir(parents=True, exist_ok=True)
        tmp_id = uuid.uuid4().hex[:8]

        content_type = request.headers.get("content-type", "").lower()
        body_bytes = b""

        # 1. Try reading multipart if sent as form and multipart parser available
        if "multipart/form-data" in content_type:
            try:
                form = await request.form()
                for key in ("file", "audio"):
                    field = form.get(key)
                    if field and hasattr(field, "read"):
                        body_bytes = await field.read()
                        break
            except Exception as exc:
                logger.debug("Multipart parsing skipped/failed (%s); reading raw body.", exc)

        # 2. Fallback to raw binary body
        if not body_bytes:
            body_bytes = await request.body()

        ext = ".ogg" if "ogg" in content_type else (".mp3" if "mp3" in content_type else ".wav")
        save_path = cfg.audio_upload_dir / f"hub_upload_{tmp_id}{ext}"
        try:
            save_path.write_bytes(body_bytes)
            engine = AudioTranscriptionEngine(config=cfg)
            return engine.transcribe(save_path, language=language)
        finally:
            save_path.unlink(missing_ok=True)


    # ---------------------------------------------------------------------------
    # Test Worker & Harness Subagent Service Methods (USR-16)
    # ---------------------------------------------------------------------------

    def get_test_workers_status(self) -> List[Dict[str, Any]]:
        """Probes known test worker nodes via the shared liveness service (USR-217)."""
        from core.infra.node_liveness import get_node_liveness, worker_nodes

        meta = [
            ("grokbox-test-worker", "grokbox-test-worker", "Remote Test Worker (grokbox, Linux)", "remote_test_worker",
             "Official gate worker (first in DARKFAC_TEST_WORKERS), Linux VM via Tailscale DERP relay; gate in ~143 s"),
            ("onprem-z97-server", "onprem-z97-server", "Dedicated Test Worker (desktop-g45ipem)", "onprem_worker",
             "Dedicated i7-4790K / 16GB / 3TB E: on-premises validation node via Tailscale"),
            ("predator-neo-16", "local-notebook", "Local Workstation Runner", "local_worker",
             "Interactive developer laptop local test engine via Tailscale"),
        ]
        nodes = worker_nodes()
        results = []
        for node_id, worker_id, name, role, description in meta:
            spec = nodes.get(node_id, {})
            url = spec.get("url", "")
            if node_id == "predator-neo-16":
                url = os.environ.get("LOCAL_NOTEBOOK_WORKER_URL", url)
            live = get_node_liveness(node_id, url=url or None)
            results.append({
                "id": worker_id,
                "node_id": node_id,
                "name": name,
                "url": live.url,
                "ip": spec.get("ip"),
                "port": spec.get("port"),
                "role": role,
                "description": description,
                "status": live.status,
                "healthy": live.is_live,
                "busy": live.busy,
                "latency_ms": live.latency_ms,
                "last_contact_at": live.last_contact_at.isoformat() if live.last_contact_at else None,
                "details": live.details,
            })
        return results


    def execute_test_run(self, instruction: TestExecutionInstruction) -> DistilledTestReport:
        """Executes a test run via TestSubagentEngine with automatic failover."""
        engine = TestSubagentEngine(project_root=self.project_root, log_dir=state_root() / "test_logs")
        return self._dispatch_test_run(engine, instruction)
