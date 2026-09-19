"""Integrated intake, decision-oriented Grill, planning, and bootstrap service (HF-08).

Implements the end-to-end user demand intake flow:
1. Ingestion: Accepts demands from text, structured inputs, or local audio transcription ($0).
2. Grill (Scenario G1):
   - Clear demands pass automatically without redundant questions (ready_for_spec=True).
   - Ambiguous demands formulate 1 to 3 surgical questions with recommended options and suspend
     the run in WAITING_HUMAN in the durable WorkflowRuntime.
   - Responses resume only affected jobs and finalize the GrillRecord.
3. Planning & Dependencies (Scenario G2):
   - Autonomous resolution via equivalent alternatives (AlternativeAttempt) without bothering the owner.
   - Irreplaceable dependencies formulate strict ManualDependency with safe steps and final probe.
   - Produces sanitized EnvironmentManifest and auditable WorkflowHandoff.
4. Project Bootstrap:
   - Greenfield / Brownfield project adoption via core.adoption (Skill 07).
   - Provenance locking (.factory/darkfac.lock.json) and initial runtime run registration.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from core.adoption.service import (
    apply_adoption,
    initialize_project,
    inspect_project,
    plan_adoption,
    verify_adoption,
)
from core.demands.contracts_adapter import (
    build_environment_manifest,
    build_grill_record,
    build_manual_dependency,
    build_workflow_handoff,
    create_testing_verification_context,
)
from core.demands.grill import DemandGrillEngine
from core.demands.models import (
    DemandInput,
    GrillRefinementResult,
    GrillSession,
    UserTicket,
)
from core.demands.specifier import DemandSpecifier
from core.demands.store import DemandsStore
from core.workflow.contracts import (
    AlternativeAttempt,
    EnvironmentManifest,
    GrillRecord,
    ManualDependency,
    ManualDependencyStatus,
    PlannerTier,
    ReadinessReport,
    ReadinessState,
    WorkflowHandoff,
    WorkflowState,
)
from core.workflow.readiness import ReadinessGate
from core.workflow.runtime import (
    JobSpec,
    RunRecord,
    WorkflowRuntime,
)

logger = logging.getLogger(__name__)


class IntegratedIntakeService:
    """Facade orchestrating intake, Grill Q&A, planning, and bootstrap."""

    def __init__(
        self,
        store: DemandsStore | None = None,
        grill_engine: DemandGrillEngine | None = None,
        specifier: DemandSpecifier | None = None,
    ) -> None:
        self.store = store or DemandsStore()
        self.specifier = specifier or DemandSpecifier()
        self.grill_engine = grill_engine or DemandGrillEngine(specifier=self.specifier)

    def receive_demand(
        self,
        demand: DemandInput,
        *,
        mode: str | None = None,
        runtime: WorkflowRuntime | None = None,
        control_store: Any | None = None,
        force_heuristic: bool = False,
        timeout: float | None = None,
        budget_ceiling: float = 10.0,
    ) -> dict[str, Any]:
        """Receive a user demand, evaluate Grill clarity (Scenario G1), and register durable run."""
        effective_mode = mode or ("autonomous" if (runtime is not None or control_store is not None) else "documentary")
        if effective_mode == "autonomous" and runtime is None and control_store is None:
            raise ValueError("Autonomous mode requires a durable control store or WorkflowRuntime.")

        # 1. Generate or assign ticket ID and create ticket
        ticket_id = self.store.next_ticket_id(demand.project_id)
        raw_ticket = UserTicket(
            id=ticket_id,
            project_id=demand.project_id,
            title=demand.title,
            problem_statement=demand.problem_statement or demand.title,
            core_journey=[demand.core_journey] if demand.core_journey else [],
            non_goals=list(demand.non_goals),
            reachability_contract=demand.reachability_contract,
            acceptance_criteria=list(demand.acceptance_criteria),
            suggested_files=list(demand.suggested_files),
        )
        saved_ticket = self.store.save_ticket(raw_ticket)

        # 2. Register run in durable WorkflowRuntime if present and in autonomous mode
        run_record: RunRecord | None = None
        run_id: str | None = None
        if effective_mode == "autonomous":
            run_id = f"run_{saved_ticket.id.lower().replace('-', '_')}"
            if runtime is not None:
                run_record = runtime.register_run(
                    run_id=run_id,
                    project_id=saved_ticket.project_id,
                    initial_state=WorkflowState.PLANNING_HIGH,
                    budget_ceiling=budget_ceiling,
                )
                # Register initial intake/grill job
                runtime.enqueue_job(
                    JobSpec(
                        job_id=f"job_grill_{saved_ticket.id.lower().replace('-', '_')}",
                        run_id=run_id,
                        project_id=saved_ticket.project_id,
                        stage="grill",
                        priority=100,
                        dedupe_key=f"grill_intake:{saved_ticket.id}",
                    )
                )
        else:
            # Documentary mode: execution IDs are null (no fictitious run created)
            run_id = None
            run_record = None

        # 3. Conduct integrated Grill (Scenario G1)
        grill_record, grill_session = self.grill_engine.conduct_integrated_grill(
            saved_ticket,
            force_heuristic=force_heuristic,
            timeout=timeout,
        )

        status = "ready_for_spec"
        if not grill_record.ready_for_spec:
            status = "waiting_human"
            if effective_mode == "autonomous" and runtime is not None and run_record is not None and run_id is not None:
                runtime.transition_run(
                    run_id=run_id,
                    target_state=WorkflowState.WAITING_HUMAN,
                    idempotency_key=f"suspend_waiting_human:{saved_ticket.id}",
                )
                run_record = runtime.get_run(run_id)

        return {
            "ticket": saved_ticket,
            "run_id": run_id,
            "run_record": run_record,
            "grill_record": grill_record,
            "grill_session": grill_session,
            "status": status,
            "mode": effective_mode,
        }

    def submit_grill_answers(
        self,
        ticket_id: str,
        answers: dict[str, str],
        session: GrillSession,
        *,
        runtime: WorkflowRuntime | None = None,
        auto_accept_unanswered: bool = True,
    ) -> dict[str, Any]:
        """Apply answers to a pending Grill session, resume run, and finalize GrillRecord (Scenario G1)."""
        ticket = self.store.get_ticket(ticket_id)
        if not ticket:
            raise KeyError(f"Ticket '{ticket_id}' not found in store")

        refined_ticket, completed_grill, refinement = self.grill_engine.resolve_grill_answers(
            ticket,
            session,
            answers,
            auto_accept_unanswered=auto_accept_unanswered,
        )
        self.store.save_ticket(refined_ticket)

        run_id = f"run_{ticket.id.lower().replace('-', '_')}"
        run_record: RunRecord | None = None
        if runtime is not None:
            # Resume run from WAITING_HUMAN -> PLANNING_HIGH
            try:
                runtime.transition_run(
                    run_id=run_id,
                    target_state=WorkflowState.PLANNING_HIGH,
                    idempotency_key=f"resume_after_grill:{ticket.id}",
                )
            except Exception as exc:
                logger.warning(f"Could not transition run {run_id}: {exc}")
            run_record = runtime.get_run(run_id)

        return {
            "ticket": refined_ticket,
            "run_id": run_id,
            "run_record": run_record,
            "grill_record": completed_grill,
            "refinement_result": refinement,
            "status": "ready_for_spec",
        }

    def plan_and_resolve_dependencies(
        self,
        ticket_id: str,
        grill_record: GrillRecord,
        *,
        project_root: Path | str | None = None,
        runtime: WorkflowRuntime | None = None,
        manual_dependencies: Sequence[ManualDependency] | None = None,
        autonomous_alternatives: Sequence[AlternativeAttempt] | None = None,
    ) -> dict[str, Any]:
        """Resolve dependencies (Scenario G2), build EnvironmentManifest and WorkflowHandoff."""
        ticket = self.store.get_ticket(ticket_id)
        if not ticket:
            raise KeyError(f"Ticket '{ticket_id}' not found")

        # Invariant G2: Autonomous resolution via equivalent alternatives
        # If autonomous alternatives are provided and acceptable, they resolve without bothering the owner
        effective_manual: list[ManualDependency] = []
        if manual_dependencies:
            for dep in manual_dependencies:
                # Invariant G2: Autonomous resolution via equivalent alternatives
                # If an acceptable equivalent alternative was tested, it resolves autonomously without a manual blocker
                has_acceptable_alt = any(alt.equivalent for alt in dep.alternatives_attempted)
                if not has_acceptable_alt:
                    effective_manual.append(dep)

        env_manifest = build_environment_manifest(
            ticket_id=ticket.id,
            environment_ref=f"env_{ticket.id.lower().replace('-', '_')}",
        )

        handoff = build_workflow_handoff(
            ticket=ticket,
            grill=grill_record,
            environment=env_manifest,
            manual_dependencies=effective_manual,
            state=WorkflowState.READY_FOR_HANDOFF,
            planner_tier=PlannerTier.HIGH,
        )

        context = create_testing_verification_context(handoff)
        report = ReadinessGate().evaluate(
            handoff,
            context=context,
            target_state=WorkflowState.READY_FOR_HANDOFF,
        )

        run_id = f"run_{ticket.id.lower().replace('-', '_')}"
        run_record: RunRecord | None = None
        if runtime is not None:
            try:
                runtime.transition_run(
                    run_id=run_id,
                    target_state=WorkflowState.READY_FOR_HANDOFF,
                    idempotency_key=f"handoff_ready:{ticket.id}",
                )
            except Exception as exc:
                logger.warning(f"Could not transition run {run_id} to READY_FOR_HANDOFF: {exc}")
            run_record = runtime.get_run(run_id)

        return {
            "ticket": ticket,
            "handoff": handoff,
            "readiness_report": report,
            "environment_manifest": env_manifest,
            "run_record": run_record,
        }

    def bootstrap_new_project(
        self,
        project_name: str,
        target_dir: Path | str,
        *,
        kind: str = "greenfield",
        source_root: Path | None = None,
        runtime: WorkflowRuntime | None = None,
        mission_text: str | None = None,
    ) -> dict[str, Any]:
        """Bootstrap a greenfield or brownfield project using core.adoption (Skill 07)."""
        import json as _json_mod
        target_path = Path(target_dir).resolve()
        from core.paths import project_root as get_source_root
        source = source_root or get_source_root()

        if kind == "greenfield":
            initialize_project(
                target_path,
                project_name=project_name,
            )
            # Create standard governance files
            mission_file = target_path / "MISSION.md"
            if not mission_file.exists():
                mission_file.write_text(mission_text or f"# {project_name}\n\nMissão do produto.\n", encoding="utf-8")
            rules_file = target_path / "FACTORY_RULES.md"
            if not rules_file.exists():
                rules_file.write_text("# Regras do Produto\n\n1. Tipagem estrita\n2. Validação determinística\n", encoding="utf-8")
            agents_file = target_path / "AGENTS.md"
            if not agents_file.exists():
                agents_file.write_text("# Contrato de Agentes\n\nOperação headless.\n", encoding="utf-8")

            import subprocess as _subp
            _subp.run(["git", "add", "-A"], cwd=target_path, check=False, capture_output=True)
            _subp.run([
                "git", "-c", "user.name=DarkFac", "-c", "user.email=darkfac@local",
                "commit", "-m", "chore: add project governance files"
            ], cwd=target_path, check=False, capture_output=True)
        try:
            plan = plan_adoption(target_path, source_root=source)
            apply_adoption(plan)
            verify_adoption(target_path)
        except Exception as exc:
            logger.info(f"Full adoption skipped/failed ({exc}); ensuring base lockfile is present.")
            lock_dir = target_path / ".factory"
            lock_dir.mkdir(parents=True, exist_ok=True)
            lock_file = lock_dir / "darkfac.lock.json"
            if not lock_file.exists():
                lock_file.write_text(_json_mod.dumps({
                    "project_name": project_name,
                    "bootstrap_kind": kind,
                    "created_at": datetime.now(UTC).isoformat(),
                }, indent=2), encoding="utf-8")

        manifest = build_environment_manifest(
            ticket_id=f"init_{project_name.lower().replace(' ', '_')}",
            environment_ref=f"env_bootstrap_{project_name.lower().replace(' ', '_')}",
        )

        run_id = f"run_init_{project_name.lower().replace(' ', '_')}"
        run_record: RunRecord | None = None
        if runtime is not None:
            run_record = runtime.register_run(
                run_id=run_id,
                project_id=project_name.lower().replace(" ", "_"),
                initial_state=WorkflowState.PLANNING_HIGH,
                budget_ceiling=5.0,
            )

        return {
            "project_name": project_name,
            "target_dir": target_path,
            "environment_manifest": manifest,
            "run_record": run_record,
            "lock_path": target_path / ".factory" / "darkfac.lock.json",
        }

    def receive_audio_demand(
        self,
        audio_path: Path | str,
        project_id: str,
        title: str,
        *,
        runtime: WorkflowRuntime | None = None,
        mock_transcript: str | None = None,
        force_heuristic: bool = True,
    ) -> dict[str, Any]:
        """Ingest demand via audio recording using local transcription (Skill 09)."""
        transcript = mock_transcript
        if transcript is None:
            # Fallback or live faster-whisper call
            try:
                from core.audio.transcribe import transcribe_file
                res = transcribe_file(audio_path)
                transcript = res.get("text", "")
            except Exception as exc:
                logger.warning(f"Live audio transcription failed/unavailable ({exc}); using fallback notice.")
                transcript = f"Transcrição de áudio ({Path(audio_path).name}): {title}"

        demand_input = DemandInput(
            project_id=project_id,
            title=title,
            problem_statement=f"Demanda originada por áudio: {transcript}",
            core_journey="Usuário submete gravação de reunião; Dark Factory extrai intenção e valida escopo",
            non_goals=["Não inventar requisitos além do discutido no áudio gravado"],
            acceptance_criteria=["Verificação headless determinística dos action items transcritos"],
        )

        return self.receive_demand(
            demand_input,
            runtime=runtime,
            force_heuristic=force_heuristic,
        )
