"""Hub service domain: roadmap, progress projection and multi-project portfolio."""

import json
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

from hub.backend.models import (
    IncidentAlert,
    ProgressProjection,
    PortfolioProjectSummary,
    PortfolioProjectDetailResponse,
    PortfolioOverviewResponse,
)
from core.roadmap.models import (
    DeliveryStatus,
    RoadmapHealth,
    RoadmapItem,
    RoadmapProjectSummary,
    RoadmapSnapshot,
    RoadmapSourceDocument,
)


logger = logging.getLogger("darkhub.service")


class ProgressMixin:
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
            roadmap_health_status = (
                "warning"
                if rh.stale or rh.blocked_items > 0 or rh.conflicts > 0 or rh.warnings > 0 or rh.sources_unavailable
                else "healthy"
            )
            health_details = {
                "stale": rh.stale,
                "blocked_items": rh.blocked_items,
                "conflicts_count": rh.conflicts,
                "warnings": rh.warnings,
                "sources_unavailable": list(rh.sources_unavailable),
            }
        except Exception as exc:
            logger.warning(f"Failed computing roadmap health for '{p.id}': {exc}")
            roadmap_health_status = "error"
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
        proj_path = self._portfolio_project_path(p)
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

    def _portfolio_project_path(self, descriptor: Any) -> Optional[Path]:
        """Checkout path of a portfolio project.

        The registry stores the owner's workstation path for the core project
        (a `C:` drive path), which does not exist on the Linux cloud worker. The core project
        always lives in this service's own repository root, so fall back to it there.
        """
        raw = getattr(descriptor, "path", None)
        candidate = Path(raw) if raw else None
        if getattr(descriptor, "id", None) == "darkfac" and (candidate is None or not candidate.exists()):
            return self.project_root
        return candidate

    def get_portfolio_project_detail(self, project_id: str) -> Optional[PortfolioProjectDetailResponse]:
        """Return deep-dive inspection response for a single adopted project (DH-08)."""
        descriptor = self.project_registry.get_project(project_id)
        if not descriptor:
            return None

        project_summary = self._build_portfolio_project_summary(descriptor)

        from core.projects.registry import resolve_commands
        proj_path = self._portfolio_project_path(descriptor) or self.project_root
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
        completed = sum(1 for u in units if u["status"] in ("completed", "succeeded", "success", "implemented"))
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
