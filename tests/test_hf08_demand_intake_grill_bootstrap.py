"""Deterministic tests for HF-08: Demand Intake, Grill, Specification, and Project Bootstrap.

Covers:
1. Ingestion of demands and registration of durable RunRecord in WorkflowRuntime (HF-05).
2. Scenario G1: Unambiguous demands pass Grill without redundant questions (ready_for_spec=True).
3. Scenario G1: Ambiguous demands formulate 1-3 questions with recommended options and suspend in WAITING_HUMAN.
4. Scenario G1: User responses finalize GrillRecord and resume only the affected run/jobs.
5. Scenario G2: Autonomous resolution of dependencies via equivalent alternatives (AlternativeAttempt).
6. Scenario G2: Irreplaceable manual dependencies with safe steps, final probe, and gate enforcement.
7. Greenfield project bootstrap with git, governance, lockfile, and runtime registration (Skill 07).
8. Brownfield project adoption with stack inspection, namespaced runtime, and lockfile.
9. Compilation of WorkflowHandoff and verification via ReadinessGate.
10. Audio intake flow (Skill 09) converting transcript to specified demand.
11. Headless CLI commands: intake, grill, plan, bootstrap.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.demands.contracts_adapter import (
    build_environment_manifest,
    build_grill_record,
    build_manual_dependency,
    build_workflow_handoff,
    create_testing_verification_context,
)
from core.demands.integrated_service import IntegratedIntakeService
from core.demands.models import DemandInput, UserTicket
from core.demands.service import DemandsService
from core.demands.store import DemandsStore
from core.workflow.contracts import (
    AlternativeAttempt,
    EnvironmentKind,
    EvidenceRequirement,
    EvidenceResult,
    GrillDecision,
    GrillRecord,
    ManualDependency,
    ManualDependencyStatus,
    PlannerTier,
    ReadinessReport,
    ReadinessState,
    SanitizedIdentity,
    WorkflowHandoff,
    WorkflowState,
)
from core.workflow.readiness import ReadinessGate
from core.workflow.runtime import (
    EventStatus,
    JobSpec,
    WorkflowRuntime,
)
from core.workflow.verification import (
    EvidenceReceipt,
    PlanApproval,
    ValidationMode,
    VerificationContext,
)


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def hf08_env(tmp_path: Path):
    """Provides isolated demands store, workflow runtime, and integrated intake service."""
    store_file = tmp_path / "demands.json"
    runtime_db = tmp_path / "workflow.sqlite3"
    clock = FakeClock()

    store = DemandsStore(store_file)
    runtime = WorkflowRuntime(runtime_db, clock=clock)
    intake = IntegratedIntakeService(store=store)
    service = DemandsService(store=store, intake_service=intake)

    yield {
        "tmp_path": tmp_path,
        "store": store,
        "runtime": runtime,
        "intake": intake,
        "service": service,
        "clock": clock,
    }
    runtime.close()


def test_intake_demand_and_runtime_run_registration(hf08_env):
    """Verify that receiving a demand registers the ticket and durable run with an outbox event."""
    intake: IntegratedIntakeService = hf08_env["intake"]
    runtime: WorkflowRuntime = hf08_env["runtime"]

    demand = DemandInput(
        project_id="test_proj",
        title="Motor de Notificações Assíncronas",
        problem_statement="Gargalo em envio síncrono de e-mails bloqueando requisições HTTP do cliente",
        core_journey="Usuário dispara ação; job entra em fila background e worker despacha sem travar resposta",
        non_goals=["Não criar interface gráfica; focar exclusivamente no despachante headless"],
        acceptance_criteria=["Suíte de testes de integração com pytest aprovando despacho de 50 mensagens"],
    )

    result = intake.receive_demand(demand, runtime=runtime, force_heuristic=True)

    ticket: UserTicket = result["ticket"]
    assert ticket.id.startswith("USR-")
    assert ticket.title == demand.title

    run_record = result["run_record"]
    assert run_record is not None
    assert run_record.run_id == f"run_{ticket.id.lower().replace('-', '_')}"
    assert run_record.project_id == "test_proj"
    assert run_record.state == WorkflowState.PLANNING_HIGH

    # Verify initial job in runtime
    job_id = f"job_grill_{ticket.id.lower().replace('-', '_')}"
    job = runtime.get_job(job_id)
    assert job.stage == "grill"
    assert job.run_id == run_record.run_id


def test_scenario_g1_clear_demand_passes_without_redundant_questions(hf08_env):
    """Scenario G1 Part 1: Clear, self-contained demand passes Grill without artificial questions."""
    intake: IntegratedIntakeService = hf08_env["intake"]
    runtime: WorkflowRuntime = hf08_env["runtime"]

    demand = DemandInput(
        project_id="darkfac",
        title="Serviço de Exportação de Relatórios em Parquet",
        problem_statement="Exportação de relatórios consome excesso de memória RAM ao processar arquivos CSV gigantes",
        core_journey="Serviço lê stream de dados e grava batches comprimidos em formato Apache Parquet",
        non_goals=[
            "Não implementar leitor de outros formatos além de Parquet",
            "Não alterar a camada visual do DarkHub neste ciclo",
        ],
        acceptance_criteria=[
            "Verificação de integridade com pyarrow comprovando redução de 70% de espaço",
            "Teste de integração headless com assertivas determinísticas",
        ],
    )

    result = intake.receive_demand(demand, runtime=runtime, force_heuristic=True)

    grill: GrillRecord = result["grill_record"]
    assert grill.ready_for_spec is True
    assert len(grill.pending_questions) == 0
    assert result["grill_session"] is None
    assert result["status"] == "ready_for_spec"

    # Invariant: Run is NOT suspended in WAITING_HUMAN
    run = runtime.get_run(result["run_id"])
    assert run.state == WorkflowState.PLANNING_HIGH


def test_scenario_g1_ambiguous_demand_suspends_and_resumes_affected_jobs(hf08_env):
    """Scenario G1 Part 2: Ambiguous demand asks 1-3 questions, suspends in WAITING_HUMAN, and resumes only affected jobs."""
    intake: IntegratedIntakeService = hf08_env["intake"]
    runtime: WorkflowRuntime = hf08_env["runtime"]

    # Ambiguous demand: short problem statement, TODO marker, empty non-goals
    ambiguous_demand = DemandInput(
        project_id="darkfac",
        title="Novo Módulo de Métricas",
        problem_statement="Preciso de métricas, TODO ver banco",
        non_goals=[],
        acceptance_criteria=[],
    )

    # 1. Intake triggers ambiguity evaluation and suspends
    intake_res = intake.receive_demand(ambiguous_demand, runtime=runtime, force_heuristic=True)
    assert intake_res["status"] == "waiting_human"

    grill: GrillRecord = intake_res["grill_record"]
    assert grill.ready_for_spec is False
    assert 1 <= len(grill.pending_questions) <= 3
    assert intake_res["grill_session"] is not None

    run = runtime.get_run(intake_res["run_id"])
    assert run.state == WorkflowState.WAITING_HUMAN

    # Verify outbox event recorded
    events = runtime.pending_events()
    assert any(ev.event_type == "run.transitioned" and ev.payload.get("to_state") == "waiting_human" for ev in events)

    # 2. User submits answers
    session = intake_res["grill_session"]
    answers = {
        session.questions[0].id: session.questions[0].options[0].label,
    }
    resume_res = intake.submit_grill_answers(
        ticket_id=intake_res["ticket"].id,
        answers=answers,
        session=session,
        runtime=runtime,
    )

    assert resume_res["status"] == "ready_for_spec"
    refined_grill: GrillRecord = resume_res["grill_record"]
    assert refined_grill.ready_for_spec is True
    assert len(refined_grill.pending_questions) == 0
    assert len(refined_grill.decisions) >= 1

    # Verify run resumed from WAITING_HUMAN -> PLANNING_HIGH
    resumed_run = runtime.get_run(intake_res["run_id"])
    assert resumed_run.state == WorkflowState.PLANNING_HIGH


def test_scenario_g2_autonomous_resolution_via_equivalent_alternative(hf08_env):
    """Scenario G2 Part 1: Missing dependency with acceptable equivalent alternative resolves autonomously."""
    intake: IntegratedIntakeService = hf08_env["intake"]
    runtime: WorkflowRuntime = hf08_env["runtime"]

    demand = DemandInput(
        project_id="darkfac",
        title="Cache em Memória com Fallback Local",
        problem_statement="Necessidade de cache distribuído sem incorrer em custo de Redis em nuvem",
        non_goals=["Não provisionar cluster Redis externo"],
        acceptance_criteria=["Cache local em SQLite/memória opera de forma transparente"],
    )
    res = intake.receive_demand(demand, runtime=runtime, force_heuristic=True)

    # Dependency with acceptable local equivalent alternative
    alt = AlternativeAttempt(
        alternative_id="sqlite_in_memory",
        description="SQLite em memória para lab local com custo zero ($0.00)",
        tested=True,
        equivalent=True,
    )
    manual_dep = build_manual_dependency(
        dependency_id="dep_cache_backend",
        ticket_ids=[res["ticket"].id],
        reason="Falta de cluster Redis externo",
        configuration_location="LOCAL_CACHE",
        steps=[("Configurar SQLite local", "SQLite ativo")],
        final_probe="python -c 'import sqlite3'",
        resume_criteria="SQLite operacional",
        alternatives_attempted=[alt],
    )

    plan_res = intake.plan_and_resolve_dependencies(
        res["ticket"].id,
        res["grill_record"],
        runtime=runtime,
        manual_dependencies=[manual_dep],
    )

    handoff: WorkflowHandoff = plan_res["handoff"]
    # Because alternative was equivalent, no human manual dependency blocks the handoff
    assert len(handoff.manual_dependencies) == 0

    # ReadinessGate allows ready_for_handoff
    report: ReadinessReport = plan_res["readiness_report"]
    assert report.eligible is True
    assert report.state == WorkflowState.READY_FOR_HANDOFF


def test_scenario_g2_irreplaceable_manual_dependency_blocks_until_probed(hf08_env):
    """Scenario G2 Part 2: Irreplaceable manual dependency generates ManualDependency and enforces gate probe."""
    intake: IntegratedIntakeService = hf08_env["intake"]
    runtime: WorkflowRuntime = hf08_env["runtime"]

    demand = DemandInput(
        project_id="darkfac",
        title="Integração de Pagamento de Produção",
        problem_statement="Processamento de cartão de crédito de clientes pagantes em produção",
        non_goals=["Não utilizar sandbox de testes em ambiente de produção"],
        acceptance_criteria=["Transação confirmada via gateway oficial"],
    )
    res = intake.receive_demand(demand, runtime=runtime, force_heuristic=True)

    # Irreplaceable dependency: production API key of external provider
    failed_alt = AlternativeAttempt(
        alternative_id="mock_gateway",
        description="Mock de testes locais",
        tested=True,
        equivalent=False,
        reason_unusable="Mock não é permitido para cobrança real de cliente",
    )
    manual_dep = build_manual_dependency(
        dependency_id="dep_stripe_live_key",
        ticket_ids=[res["ticket"].id],
        reason="Chave de produção exclusiva do owner exigida para cobrança",
        configuration_location="STRIPE_LIVE_API_KEY",
        steps=[
            ("Acessar o painel Stripe na conta oficial", "Dashboard aberto"),
            ("Copiar a chave restrita de produção para o cofre seguro", "Chave inserida"),
        ],
        final_probe="probe_stripe_connection",
        resume_criteria="Probe de conexão retorna status 200",
        alternatives_attempted=[failed_alt],
        blocked_stages=[WorkflowState.READY_FOR_HANDOFF, WorkflowState.DELIVERED],
        status=ManualDependencyStatus.WAITING,
    )

    plan_res = intake.plan_and_resolve_dependencies(
        res["ticket"].id,
        res["grill_record"],
        runtime=runtime,
        manual_dependencies=[manual_dep],
    )

    report: ReadinessReport = plan_res["readiness_report"]
    # The dependency is waiting, so readiness gate BLOCKS READY_FOR_HANDOFF
    assert report.eligible is False
    assert "dep_stripe_live_key" in report.blocking_dependency_ids


def test_greenfield_project_bootstrap(hf08_env):
    """Verify that bootstrap_new_project creates clean greenfield structure with git, lockfile, and runtime run."""
    intake: IntegratedIntakeService = hf08_env["intake"]
    runtime: WorkflowRuntime = hf08_env["runtime"]
    tmp_path: Path = hf08_env["tmp_path"]

    target_dir = tmp_path / "NovoApp"
    res = intake.bootstrap_new_project(
        project_name="NovoApp",
        target_dir=target_dir,
        kind="greenfield",
        runtime=runtime,
    )

    assert target_dir.exists()
    assert (target_dir / "MISSION.md").exists()
    assert (target_dir / "FACTORY_RULES.md").exists()
    assert (target_dir / "AGENTS.md").exists()
    assert (target_dir / ".factory" / "darkfac.lock.json").exists()

    manifest = res["environment_manifest"]
    assert any(t.name == "python" for t in manifest.tools)

    run_record = res["run_record"]
    assert run_record is not None
    assert run_record.project_id == "novoapp"
    assert run_record.state == WorkflowState.PLANNING_HIGH


def test_brownfield_project_bootstrap(hf08_env):
    """Verify that bootstrap_new_project adopts existing repository with lockfile and runtime registration."""
    intake: IntegratedIntakeService = hf08_env["intake"]
    runtime: WorkflowRuntime = hf08_env["runtime"]
    tmp_path: Path = hf08_env["tmp_path"]

    existing_repo = tmp_path / "ExistingRepo"
    existing_repo.mkdir()
    subprocess.run(["git", "init"], cwd=existing_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=existing_repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=existing_repo, check=True, capture_output=True)

    (existing_repo / "README.md").write_text("# Existing Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=existing_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=existing_repo, check=True, capture_output=True)

    res = intake.bootstrap_new_project(
        project_name="ExistingRepo",
        target_dir=existing_repo,
        kind="brownfield",
        runtime=runtime,
    )

    assert (existing_repo / ".factory" / "darkfac.lock.json").exists()
    assert res["run_record"] is not None
    assert res["run_record"].state == WorkflowState.PLANNING_HIGH


def test_audio_demand_intake_flow(hf08_env):
    """Verify that audio intake correctly extracts transcription and enters the Grill loop."""
    intake: IntegratedIntakeService = hf08_env["intake"]
    runtime: WorkflowRuntime = hf08_env["runtime"]
    tmp_path: Path = hf08_env["tmp_path"]

    fake_audio = tmp_path / "meeting_notes.mp3"
    fake_audio.write_bytes(b"FAKE_AUDIO_BYTES")

    res = intake.receive_audio_demand(
        audio_path=fake_audio,
        project_id="darkfac",
        title="Refatorar Loop de Eventos",
        runtime=runtime,
        mock_transcript="Alinhamos na reunião que o loop de eventos precisa de timeout de 30 segundos e fallback local.",
        force_heuristic=True,
    )

    assert res["ticket"].title == "Refatorar Loop de Eventos"
    assert "reunião" in res["ticket"].problem_statement.lower()
    assert res["grill_record"] is not None
    assert res["run_record"] is not None


def test_cli_headless_hf08_commands(tmp_path: Path):
    """Verify that CLI subcommands (intake, grill, plan, bootstrap) execute cleanly via headless JSON on Windows."""
    repo_root = Path(__file__).resolve().parents[1]

    # 1. CLI intake (Scenario G1 clear demand)
    proc_intake = subprocess.run(
        [
            sys.executable,
            "-m",
            "core.demands.cli",
            "intake",
            "--title",
            "CLI Pipeline de Testes",
            "--problem",
            "Problema bem especificado para validação de comandos headless",
            "--non-goals",
            "Não criar frontends",
            "--criteria",
            "Passar no pytest com exit code 0",
            "--force-heuristic",
            "--json",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    data_intake = json.loads(proc_intake.stdout)
    assert data_intake["ready_for_spec"] is True
    ticket_id = data_intake["ticket"]["id"]

    # 2. CLI plan
    proc_plan = subprocess.run(
        [
            sys.executable,
            "-m",
            "core.demands.cli",
            "plan",
            ticket_id,
            "--json",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    data_plan = json.loads(proc_plan.stdout)
    assert data_plan["ticket_id"] == ticket_id
    assert data_plan["state"] == "ready_for_handoff"
    assert data_plan["planner_tier"] == "high"

    # 3. CLI bootstrap
    boot_target = tmp_path / "CliBootProject"
    proc_boot = subprocess.run(
        [
            sys.executable,
            "-m",
            "core.demands.cli",
            "bootstrap",
            "CliBootProject",
            str(boot_target),
            "--kind",
            "greenfield",
            "--json",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    data_boot = json.loads(proc_boot.stdout)
    assert data_boot["project"] == "CliBootProject"
    assert Path(data_boot["lock_path"]).exists()
