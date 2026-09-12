"""Deterministic test suite for HF-10: Persistent Memory, Self-Learning, Research Ledger & Learning Pack.

Validates:
1. Selective context assembly and survival across complete cold restarts.
2. Invariant fail-closed: unpromoted or failed rules NEVER activate or enter context.
3. Strict project isolation: project A rules/preferences never leak into project B.
4. Scenario G7: canonical research sources resolve to links, insights link decisions and tickets, and persist across restarts.
5. Owner Learning Pack: multi-tier Feynman, active recall cards, 2-3 discussion topics, non-blocking invariants.
"""

import os
import shutil
import tempfile
from pathlib import Path
import pytest

from core.learning.models import (
    PolicyOrigin,
    PolicyStatus,
    PreferenceCategory,
    UserPreference,
)
from core.learning.promotion import PromotionDeniedError
from core.learning.service import PersistentMemoryService
from core.learning_pack.models import (
    ConceptCategory,
    ExplanationTier,
    DefenseQA,
    TradeOffOption,
    LearningConcept,
)
from core.learning_pack.renderer import LearningPackRenderer
from core.execution.agent_executor import TaskSpec
from core.research.models import (
    AuthorityTier,
    LicenseType,
    ResearchSource,
    ResearchTopicType,
    SourceInsight,
)


@pytest.fixture
def temp_factory_dir():
    """Create an isolated temporary .factory directory for persistent memory testing."""
    tmp_path = Path(tempfile.mkdtemp(prefix="darkfac_test_hf10_"))
    yield tmp_path
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_persistent_memory_restart_survival(temp_factory_dir):
    """Retomada de contexto e dados sobrevive a restart a frio completo do processo."""
    service = PersistentMemoryService(base_dir=temp_factory_dir, session_id="test_sess_01")

    # 1. Register candidate and promote
    candidate = service.register_candidate(
        rule_id="RULE_PERSIST_01",
        origin=PolicyOrigin.RCA,
        scope="core/engine/router.py",
        rule_content="Always enforce bounded retry queues before dispatching.",
        supporting_runs=["run_101", "run_102"],
        eval_version="eval_v1.0",
        project_id="proj_core",
    )
    assert candidate.status == PolicyStatus.PROPOSED

    promoted = service.evaluate_and_promote(
        rule_id="RULE_PERSIST_01",
        eval_version="eval_v1.0",
        passed=True,
        test_output="All 10 bounded queue assertions passed",
    )
    assert promoted.status == PolicyStatus.ACTIVE

    # 2. Record research dossier
    src = ResearchSource(
        id="src_arxiv_01",
        title="Bounded Queues in High Concurrency Systems",
        url="https://arxiv.org/abs/2401.99999",
        source_type="paper",
        authority_tier=AuthorityTier.HIGH_CREDIBILITY,
    )
    ins = SourceInsight(
        source_id="src_arxiv_01",
        source_title="Bounded Queues in High Concurrency Systems",
        key_insight="Backpressure prevents cascading memory exhaustion",
        architectural_implications="Queue capacity must be bounded to 1024 slots",
        future_reference_value="Core router dispatch",
    )
    ledger = service.record_research(
        query="bounded queue latency backpressure",
        topic_type=ResearchTopicType.TOPIC_CONCEPT,
        sources=[src],
        insights=[ins],
        summary_executive="Queue boundedness preserves determinism.",
        decisions_linked=["ADR-010: Bounded queues in router"],
        related_tickets=["HF-10"],
        ledger_id="ledger_bounded_queues",
    )
    assert ledger.ledger_id == "ledger_bounded_queues"

    # 3. Generate Owner Learning Pack
    pack = service.generate_owner_learning_pack(
        title="Bounded Memory Mechanics",
        files_analyzed=["core/engine/router.py"],
        discussion_topics=[
            "What happens if queue fills up during traffic bursts?",
            "Should we configure dead-letter queues?",
        ],
    )
    assert pack.reading_is_optional is True
    assert pack.blocks_production is False
    assert len(pack.discussion_topics) == 2

    # 4. Cold restart simulation: instantiate a new service pointing to the same folder
    cold_service = PersistentMemoryService(base_dir=temp_factory_dir, session_id="test_sess_02")

    # Verify rule persisted as ACTIVE
    active_rules = cold_service.list_active_rules(project_id="proj_core")
    assert any(r.rule_id == "RULE_PERSIST_01" and r.status == PolicyStatus.ACTIVE for r in active_rules)

    # Verify context assembly contains the persisted rule
    task_spec = TaskSpec(
        task_id="T-99",
        objective="Refactor router dispatch mechanism",
        allowed_paths=["core/engine/router.py"],
    )
    context = cold_service.get_selective_context(task_spec, project_id="proj_core")
    assert any("RULE_PERSIST_01" in r for r in context.active_rules)

    # Verify research ledger survived
    loaded_ledger = cold_service.load_research("ledger_bounded_queues")
    assert loaded_ledger is not None
    assert loaded_ledger.query == "bounded queue latency backpressure"
    assert loaded_ledger.decisions_linked == ["ADR-010: Bounded queues in router"]
    assert loaded_ledger.related_tickets == ["HF-10"]
    assert len(loaded_ledger.sources) == 1
    assert loaded_ledger.sources[0].url == "https://arxiv.org/abs/2401.99999"

    # Verify learning pack survived
    loaded_pack = cold_service.load_learning_pack(pack.pack_id)
    assert loaded_pack is not None
    assert loaded_pack.title == "Bounded Memory Mechanics"
    assert loaded_pack.reading_is_optional is True
    assert loaded_pack.blocks_production is False
    assert len(loaded_pack.discussion_topics) == 2


def test_fail_closed_unpromoted_rules_never_activate(temp_factory_dir):
    """Regra reprovada ou não promovida NUNCA é ativada nem injetada no contexto."""
    service = PersistentMemoryService(base_dir=temp_factory_dir)

    # Register candidate
    service.register_candidate(
        rule_id="RULE_UNPROVEN",
        origin=PolicyOrigin.RCA,
        scope="core/auth/tokens.py",
        rule_content="Always bypass JWT check in dev mode.",
        supporting_runs=["run_dummy"],
        eval_version="eval_v1",
    )

    task_spec = TaskSpec(
        task_id="T-AUTH",
        objective="Update core auth tokens parser",
        allowed_paths=["core/auth/tokens.py"],
    )

    # 1. In PROPOSED status, must NOT be present in context
    ctx = service.get_selective_context(task_spec)
    assert not any("RULE_UNPROVEN" in r for r in ctx.active_rules)

    # 2. Evaluation fails
    failed_cand = service.evaluate_and_promote(
        rule_id="RULE_UNPROVEN",
        eval_version="eval_v1",
        passed=False,
        error_message="Security violation: dev bypass rejected",
    )
    assert failed_cand.status != PolicyStatus.ACTIVE

    # Must NOT be present in context after failure
    ctx2 = service.get_selective_context(task_spec)
    assert not any("RULE_UNPROVEN" in r for r in ctx2.active_rules)

    # 3. Attempting to promote candidate without supporting runs raises error
    service.register_candidate(
        rule_id="RULE_NO_RUNS",
        origin=PolicyOrigin.RCA,
        scope="core/auth/tokens.py",
        eval_version="eval_v1",
        supporting_runs=[],
    )
    with pytest.raises(PromotionDeniedError) as exc_info:
        service.evaluate_and_promote(rule_id="RULE_NO_RUNS", eval_version="eval_v1", passed=True)
    assert "supporting run" in str(exc_info.value).lower()


def test_strict_project_isolation(temp_factory_dir):
    """Regras e preferências de um projeto nunca vazam para outro projeto."""
    service = PersistentMemoryService(base_dir=temp_factory_dir)

    # Project Alpha Rule
    service.register_candidate(
        rule_id="RULE_ALPHA",
        origin=PolicyOrigin.EXPLICIT_PREFERENCE,
        scope="core/billing",
        rule_content="Project Alpha: Use Stripe idempotency keys.",
        supporting_runs=["run_alpha_1"],
        eval_version="v1",
        project_id="project-alpha",
    )
    service.evaluate_and_promote("RULE_ALPHA", "v1", passed=True)

    # Project Beta Rule
    service.register_candidate(
        rule_id="RULE_BETA",
        origin=PolicyOrigin.EXPLICIT_PREFERENCE,
        scope="core/billing",
        rule_content="Project Beta: Use PayPal sandbox headers.",
        supporting_runs=["run_beta_1"],
        eval_version="v1",
        project_id="project-beta",
    )
    service.evaluate_and_promote("RULE_BETA", "v1", passed=True)

    # Global Rule
    service.register_candidate(
        rule_id="RULE_GLOBAL",
        origin=PolicyOrigin.OBSERVATION,
        scope="core/billing",
        rule_content="Universal: Always log transaction IDs.",
        supporting_runs=["run_global_1"],
        eval_version="v1",
        project_id="global",
    )
    service.evaluate_and_promote("RULE_GLOBAL", "v1", passed=True)

    task_spec = TaskSpec(
        task_id="T-BILLING",
        objective="Update billing gateway dispatcher",
        allowed_paths=["core/billing/gateway.py"],
    )

    # Query for project-alpha
    ctx_alpha = service.get_selective_context(task_spec, project_id="project-alpha")
    alpha_rules = " ".join(ctx_alpha.active_rules)
    assert "RULE_ALPHA" in alpha_rules
    assert "RULE_GLOBAL" in alpha_rules
    assert "RULE_BETA" not in alpha_rules, "Leak detected: Project Beta rule leaked into Alpha context"

    # Query for project-beta
    ctx_beta = service.get_selective_context(task_spec, project_id="project-beta")
    beta_rules = " ".join(ctx_beta.active_rules)
    assert "RULE_BETA" in beta_rules
    assert "RULE_GLOBAL" in beta_rules
    assert "RULE_ALPHA" not in beta_rules, "Leak detected: Project Alpha rule leaked into Beta context"


def test_scenario_g7_research_sources_decisions_and_restart(temp_factory_dir):
    """Cenário G7: fontes de pesquisa resolvem a links, insights vinculam decisões/tickets e persistem."""
    service = PersistentMemoryService(base_dir=temp_factory_dir)

    src_high = ResearchSource(
        id="src_raft_paper",
        title="In Search of an Understandable Consensus Algorithm (Raft)",
        url="https://raft.github.io/raft.pdf",
        source_type="paper",
        license="CC-BY-4.0",
        authority_tier=AuthorityTier.HIGH_CREDIBILITY,
        credibility_score=0.98,
    )
    src_trend = ResearchSource(
        id="src_tech_blog",
        title="Discussion on Consensus in Edge Nodes",
        url="https://news.ycombinator.com/item?id=9999999",
        source_type="expert_trend",
        authority_tier=AuthorityTier.TREND_SIGNAL,
        credibility_score=0.75,
        metadata={"points": 250, "comments": 80},
    )

    insight_1 = SourceInsight(
        source_id="src_raft_paper",
        source_title="In Search of an Understandable Consensus Algorithm",
        key_insight="Leader election and log replication are decomposed for comprehensible safety",
        architectural_implications="Maintain strict monotonic terms; discard uncommitted entries upon new term",
        future_reference_value="Foundational consensus across multi-worker clusters",
        authority_tier=AuthorityTier.HIGH_CREDIBILITY,
        code_patterns_or_algorithms=["TermIncrement", "HeartbeatTimer", "AppendEntriesRPC"],
    )

    ledger = service.record_research(
        query="distributed consensus and lease management",
        topic_type=ResearchTopicType.TOPIC_CONCEPT,
        sources=[src_high, src_trend],
        insights=[insight_1],
        summary_executive="Consensus protocol ensures zero-split-brain under network partitions.",
        decisions_linked=[
            "ADR-031: Lease management via monotonic term heartbeats",
            "ADR-032: Two-phase commit protocol holding gate",
        ],
        related_tickets=["HF-05", "HF-09", "HF-10"],
        ledger_id="dossier_consensus_g7",
    )

    # Validate in-memory model
    assert ledger.ledger_id == "dossier_consensus_g7"
    assert len(ledger.sources) == 2
    assert ledger.sources[0].url == "https://raft.github.io/raft.pdf"
    assert ledger.sources[1].url == "https://news.ycombinator.com/item?id=9999999"
    assert len(ledger.decisions_linked) == 2
    assert len(ledger.related_tickets) == 3

    # Validate Markdown rendering contains links and decision sections
    md_content = ledger.to_markdown()
    assert "[https://raft.github.io/raft.pdf](https://raft.github.io/raft.pdf)" in md_content
    assert "[https://news.ycombinator.com/item?id=9999999](https://news.ycombinator.com/item?id=9999999)" in md_content
    assert "Decisões Arquiteturais Vinculadas" in md_content
    assert "ADR-031: Lease management" in md_content
    assert "Tickets Relacionados" in md_content
    assert "`HF-10`" in md_content

    # Validate physical files on disk
    ledger_json = temp_factory_dir / "research" / "dossier_consensus_g7" / "ledger.json"
    ledger_md = temp_factory_dir / "research" / "dossier_consensus_g7" / "INSIGHTS.md"
    assert ledger_json.exists()
    assert ledger_md.exists()

    # Restart and reload
    service.reload()
    reloaded = service.load_research("dossier_consensus_g7")
    assert reloaded is not None
    assert reloaded.sources[0].id == "src_raft_paper"
    assert reloaded.sources[0].url == "https://raft.github.io/raft.pdf"
    assert reloaded.decisions_linked == ledger.decisions_linked
    assert reloaded.related_tickets == ledger.related_tickets


def test_owner_learning_pack_feynman_and_non_blocking(temp_factory_dir):
    """Learning pack do owner com múltiplos níveis Feynman, 2-3 tópicos e leitura opcional não-bloqueante."""
    service = PersistentMemoryService(base_dir=temp_factory_dir)

    tier = ExplanationTier(
        pitch_30s="A digital guardrail that prevents bad code from ever touching live systems.",
        staff_architect="Zero-trust deterministic verification gate with isolated subprocess sandboxing.",
        under_the_hood="Spawns isolated child process with strict flags, intercepting return codes and stdout markers.",
    )
    concept = LearningConcept(
        concept_id="c_verification_gate",
        name="Deterministic Verification Gate",
        category=ConceptCategory.RELIABILITY,
        mental_anchor="An automated airport security scanner that inspects every byte before boarding.",
        tiers=tier,
        defense=[
            DefenseQA(
                question="Doesn't running full test suites slow down our deployment loops?",
                bulletproof_answer="Quick gates execute in under 3 seconds, saving hours of manual rollback triage.",
            )
        ],
        trade_offs=[
            TradeOffOption(
                option="Deterministic Gate",
                pros="100% reproducible confidence",
                cons="Slight test execution overhead",
                why_chosen="Eliminates silent flakiness",
            )
        ],
    )

    pack = service.generate_owner_learning_pack(
        title="Reliability Architecture Pack",
        custom_concepts=[concept],
        discussion_topics=[
            "How should we tune timeouts for long-running end-to-end holdouts?",
            "What metrics should we display on the executive dashboard?",
        ],
    )

    # 1. Non-blocking Invariant checks
    assert pack.reading_is_optional is True
    assert pack.blocks_production is False
    assert len(pack.discussion_topics) == 2

    # 2. Multi-tier Feynman checks
    assert pack.concepts[0].tiers.pitch_30s.startswith("A digital guardrail")
    assert pack.concepts[0].tiers.staff_architect.startswith("Zero-trust")
    assert pack.concepts[0].tiers.under_the_hood.startswith("Spawns isolated child")
    assert pack.concepts[0].mental_anchor.startswith("An automated airport")

    # 3. Flashcards generation
    assert len(pack.flashcards) >= 1
    assert any("airport" in f.why_it_matters or "stakeholders" in f.why_it_matters.lower() or "Staff+" in f.why_it_matters for f in pack.flashcards)

    # 4. Renderers output validation
    md = LearningPackRenderer.render_markdown(pack)
    assert "Tópicos para Alinhamento & Discussão do Owner (Opcional)" in md
    assert "A leitura e discussão destes tópicos é opcional e NÃO bloqueia a produção" in md
    assert "How should we tune timeouts" in md

    html_out = LearningPackRenderer.render_html(pack)
    assert "Tópicos de Alinhamento & Discussão (Opcionais)" in html_out
    assert "A leitura é 100% opcional e não bloqueia produção" in html_out
    assert "How should we tune timeouts" in html_out

    tsv_out = LearningPackRenderer.render_anki_tsv(pack)
    assert len(tsv_out.strip().splitlines()) >= 1


def test_default_discussion_topics_generated_if_omitted(temp_factory_dir):
    """Se nenhum tópico de discussão for informado, o gerador sintetiza 2 a 3 tópicos técnicos."""
    service = PersistentMemoryService(base_dir=temp_factory_dir)
    pack = service.generate_owner_learning_pack(title="Default Topics Pack")

    assert pack.reading_is_optional is True
    assert pack.blocks_production is False
    assert 2 <= len(pack.discussion_topics) <= 3
    assert all(isinstance(t, str) and len(t) > 10 for t in pack.discussion_topics)


def test_candidate_rollback_removes_from_context(temp_factory_dir):
    """Rollback de candidato ativo transiciona para RETIRED e remove da montagem de contexto."""
    service = PersistentMemoryService(base_dir=temp_factory_dir)

    service.register_candidate(
        rule_id="RULE_ROLLBACK_TEST",
        origin=PolicyOrigin.RCA,
        scope="core/cache",
        rule_content="Use aggressive write-through cache.",
        supporting_runs=["run_01"],
        eval_version="v1",
    )
    service.evaluate_and_promote("RULE_ROLLBACK_TEST", "v1", passed=True)

    task_spec = TaskSpec(
        task_id="T-CACHE",
        objective="Refactor cache invalidation",
        allowed_paths=["core/cache/client.py"],
    )

    # In active state, must be present
    ctx = service.get_selective_context(task_spec)
    assert any("RULE_ROLLBACK_TEST" in r for r in ctx.active_rules)

    # Roll back candidate
    service.promotion_engine.rollback_candidate(
        rule_id="RULE_ROLLBACK_TEST",
        reason="Performance degradation under high write concurrency",
    )

    # Must NOT be present in context
    ctx_after = service.get_selective_context(task_spec)
    assert not any("RULE_ROLLBACK_TEST" in r for r in ctx_after.active_rules)

    # After cold restart, remains RETIRED and absent from context
    service.reload()
    ctx_reloaded = service.get_selective_context(task_spec)
    assert not any("RULE_ROLLBACK_TEST" in r for r in ctx_reloaded.active_rules)


def test_user_preferences_isolation_and_inactivity(temp_factory_dir):
    """Preferências do usuário no tracker ledger respeitam isolamento de projeto e status ativo."""
    service = PersistentMemoryService(base_dir=temp_factory_dir)

    # Add active preference for project X
    pref_x = UserPreference(
        preference_id="PREF_X",
        category=PreferenceCategory.ARCHITECTURE,
        rule="Prefer asynchronous event queues for long operations.",
        context_or_example="Task dispatch",
        confidence=1.0,
        created_at="2026-09-11T20:00:00Z",
        status=PolicyStatus.ACTIVE,
    )
    # Store project_id in metadata
    pref_x.metadata = {"project_id": "project-x"}
    service.tracker.ledger.preferences.append(pref_x)

    # Add active preference for project Y
    pref_y = UserPreference(
        preference_id="PREF_Y",
        category=PreferenceCategory.ARCHITECTURE,
        rule="Prefer synchronous RPCs for high reliability.",
        context_or_example="Task dispatch",
        confidence=1.0,
        created_at="2026-09-11T20:00:00Z",
        status=PolicyStatus.ACTIVE,
    )
    pref_y.metadata = {"project_id": "project-y"}
    service.tracker.ledger.preferences.append(pref_y)

    # Add inactive preference
    pref_inactive = UserPreference(
        preference_id="PREF_INACTIVE",
        category=PreferenceCategory.ARCHITECTURE,
        rule="Avoid thread pools.",
        context_or_example="Task dispatch",
        confidence=1.0,
        created_at="2026-09-11T20:00:00Z",
        status=PolicyStatus.RETIRED,
        active=False,
    )
    service.tracker.ledger.preferences.append(pref_inactive)
    service.tracker.save()

    task_spec = TaskSpec(
        task_id="T-ARCH",
        objective="Design architecture of dispatch module",
        allowed_paths=["core/arch/dispatch.py"],
    )

    # Query for project-x
    ctx_x = service.get_selective_context(task_spec, project_id="project-x")
    rules_x = " ".join(ctx_x.active_rules)
    assert "PREF_X" in rules_x
    assert "PREF_Y" not in rules_x
    assert "PREF_INACTIVE" not in rules_x

    # Query for project-y
    ctx_y = service.get_selective_context(task_spec, project_id="project-y")
    rules_y = " ".join(ctx_y.active_rules)
    assert "PREF_Y" in rules_y
    assert "PREF_X" not in rules_y
    assert "PREF_INACTIVE" not in rules_y


def test_learning_pack_and_research_models_roundtrip():
    """Valida que to_dict e from_dict preservam 100% dos novos campos de HF-10."""
    from core.learning_pack.models import SessionLearningPack
    from core.research.models import ResearchLedger

    # 1. SessionLearningPack roundtrip
    pack = SessionLearningPack(
        pack_id="p1",
        session_id="s1",
        timestamp="2026-09-11T21:00:00Z",
        title="Test Pack",
        executive_summary="Summary",
        discussion_topics=["Topic 1", "Topic 2", "Topic 3"],
        reading_is_optional=True,
        blocks_production=False,
    )
    pack_dict = pack.to_dict()
    assert pack_dict["discussion_topics"] == ["Topic 1", "Topic 2", "Topic 3"]
    assert pack_dict["reading_is_optional"] is True
    assert pack_dict["blocks_production"] is False

    reconstructed_pack = SessionLearningPack.from_dict(pack_dict)
    assert reconstructed_pack.discussion_topics == pack.discussion_topics
    assert reconstructed_pack.reading_is_optional is True
    assert reconstructed_pack.blocks_production is False

    # 2. ResearchLedger roundtrip
    ledger = ResearchLedger(
        ledger_id="led1",
        query="test query",
        topic_type=ResearchTopicType.TOPIC_CONCEPT,
        decisions_linked=["ADR-1", "ADR-2"],
        related_tickets=["HF-09", "HF-10"],
    )
    ledger_dict = ledger.to_dict()
    assert ledger_dict["decisions_linked"] == ["ADR-1", "ADR-2"]
    assert ledger_dict["related_tickets"] == ["HF-09", "HF-10"]

    reconstructed_ledger = ResearchLedger.from_dict(ledger_dict)
    assert reconstructed_ledger.decisions_linked == ["ADR-1", "ADR-2"]
    assert reconstructed_ledger.related_tickets == ["HF-09", "HF-10"]

