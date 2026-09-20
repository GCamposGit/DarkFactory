"""Deterministic test suite for HF-10-02: Pesquisa ligada às decisões.

Normative verification of:
- docs/handoffs/continuous-autonomy/HF-10-02.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/learning/research_dispatch.py
- core/workflow/research_handler.py
- core/workflow/handlers.py
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest

from core.learning.research_dispatch import (
    AUTHORIZED_CONTRACT_ROLES,
    BlockingResearchMissingDecisionError,
    BudgetExceededError,
    ContractChangeUnauthorizedError,
    PrimarySourceCitation,
    ReResearchScheduleEvent,
    ResearchDispatchResult,
    ResearchDispatcher,
    ResearchRequest,
    SourceUnavailableError,
    StaleResearchError,
)
from core.research.models import AuthorityTier, LicenseType, ResearchTopicType
from core.workflow.control_contracts import (
    Claim,
    JobKey,
    StageContext,
    StageResult,
)
from core.workflow.handlers import (
    StageHandler,
    build_handlers,
    dispatch_stage,
)
from core.workflow.research_handler import (
    ResearchStageHandler,
    create_research_bindings,
)


def _make_context(
    stage: str = "research",
    ticket_id: str = "HF-10-02",
    run_id: str = "run-res-01",
    iteration: int = 0,
    expires_at: str | None = None,
    identity: str = "researcher",
    input_refs: list[str] | None = None,
) -> StageContext:
    jk = JobKey(
        run_id=run_id,
        ticket_id=ticket_id,
        plan_version="1.0",
        stage=stage,
        iteration=iteration,
    )
    exp = expires_at or (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    claim = Claim(
        job_key=jk,
        lease_id=f"lease-{stage}-{iteration}",
        owner="worker-res",
        fencing_token=1,
        expires_at=exp,
    )
    return StageContext(
        claim=claim,
        plan_ref="plan-res-1",
        plan_digest="sha256:" + "b" * 64,
        config_version="1.0",
        environment_ref="env-local",
        identity=identity,
        route_ref="route-local",
        memory_version="mem-v1:initial",
        input_refs=input_refs or [f"query: authoritative architecture patterns for {ticket_id}"],
    )


# ===========================================================================
# 1. Contraprova A: Fonte indisponível não vira fato
# ===========================================================================


class TestContraproofAUnavailableSource:
    def test_unavailable_source_never_becomes_fact(self, tmp_path: Path) -> None:
        """Unavailable source returns error / unverified status, credibility 0.0, never hallucinating facts."""
        dispatcher = ResearchDispatcher(
            base_dir=tmp_path / "research",
            source_verifier=lambda url: "live.example.com" in url,
        )

        # Submit query with unavailable source
        req = ResearchRequest(
            question="Analyze Byzantine fault tolerance in decentralized storage",
            version="v1",
            scope="architecture",
            sources_to_verify=[
                {
                    "url": "https://offline.example.com/paper.pdf",
                    "snippet": "Claims that consensus can be achieved in 1 round without quorum.",
                    "available": False,
                }
            ],
        )

        res = dispatcher.dispatch(req)
        assert res.status == "unverified_source"
        assert res.ledger_id is None
        assert len(res.primary_sources) == 1
        citation = res.primary_sources[0]
        assert citation.status == "unavailable"
        assert citation.verified is False
        assert citation.error_message is not None
        # ResearchSource credibility score must be 0.0
        rs = citation.to_research_source()
        assert rs.credibility_score == 0.0
        # No output refs emitted for unverified source
        assert res.output_refs == []

    def test_fail_closed_mode_raises_source_unavailable_error(self, tmp_path: Path) -> None:
        """When fail-closed flag is active, unavailable primary source raises SourceUnavailableError."""
        dispatcher = ResearchDispatcher(
            base_dir=tmp_path / "research",
            source_verifier=lambda url: False,
            fail_closed_on_source_error=True,
        )
        req = ResearchRequest(
            question="Zero-knowledge rollup benchmarks",
            sources_to_verify=[{"url": "https://unreachable.org/doc", "snippet": "zk-proof spec"}],
        )
        with pytest.raises(SourceUnavailableError, match="cannot establish fact without evidence"):
            dispatcher.dispatch(req)


# ===========================================================================
# 2. Contraprova B: Pesquisa vencida agenda automaticamente re-pesquisa
# ===========================================================================


class TestContraproofBExpiredResearchSchedules:
    def test_expired_research_ttl_schedules_re_research(self, tmp_path: Path) -> None:
        """When a cached research dossier exceeds its TTL, dispatcher automatically schedules re-research."""
        dispatcher = ResearchDispatcher(
            base_dir=tmp_path / "research",
            default_ttl_seconds=1,  # 1 second TTL for test
        )

        req = ResearchRequest(
            question="Distributed vector indexing with HNSW",
            version="v1",
            scope="vector_db",
            ttl_seconds=1,
            sources_to_verify=[{"url": "https://valid.org/hnsw", "snippet": "Hierarchical navigable small world graphs"}],
        )

        # Initial successful dispatch
        res1 = dispatcher.dispatch(req)
        assert res1.status == "success"
        assert res1.is_cached is False
        assert res1.ledger_id is not None
        initial_ledger_id = res1.ledger_id

        # Artificially age the entry in index
        digest = req.canonical_digest
        dispatcher._index[digest]["created_at"] = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
        dispatcher._save_index()

        # Query again: TTL is expired
        res2 = dispatcher.dispatch(req)
        assert res2.status == "re_research_scheduled"
        assert res2.scheduled_re_research is True
        assert res2.ledger_id == initial_ledger_id
        assert len(dispatcher.scheduled_re_research_queue) == 1

        queued_event = dispatcher.scheduled_re_research_queue[0]
        assert isinstance(queued_event, ReResearchScheduleEvent)
        assert queued_event.request_digest == digest
        assert queued_event.reason == "ttl_expired"
        assert queued_event.question == req.question


# ===========================================================================
# 3. Contraprova C: Mudança de contrato só por high_architecture
# ===========================================================================


class TestContraproofCContractChangeAuthorization:
    def test_contract_change_rejected_for_unauthorized_roles(self, tmp_path: Path) -> None:
        """Roles like economy, developer, or researcher cannot approve contract changes."""
        dispatcher = ResearchDispatcher(base_dir=tmp_path / "research")

        unauthorized_roles = ["economy", "developer", "researcher", "intern", "qa"]
        for role in unauthorized_roles:
            req = ResearchRequest(
                question="Propose deprecation of REST v1 in favor of gRPC",
                proposes_contract_change=True,
                requested_by_role=role,
            )
            with pytest.raises(ContractChangeUnauthorizedError) as exc_info:
                dispatcher.dispatch(req)
            assert "high_architecture" in str(exc_info.value)

    def test_contract_change_authorized_for_high_architecture(self, tmp_path: Path) -> None:
        """high_architecture or high role is authorized to propose/approve contract changes."""
        dispatcher = ResearchDispatcher(base_dir=tmp_path / "research")

        for role in ("high_architecture", "high", "architecture_owner"):
            req = ResearchRequest(
                question=f"Contract boundary update for {role}",
                version="v1",
                scope=f"scope_{role}",
                proposes_contract_change=True,
                requested_by_role=role,
            )
            res = dispatcher.dispatch(req)
            assert res.status == "success"
            assert res.contract_change_authorized is True


# ===========================================================================
# 4. Contraprova D: Query deduplicada não esgota cota nem gera custo
# ===========================================================================


class TestContraproofDDeduplicationNoQuotaDrain:
    def test_deduplicated_query_reuses_ledger_at_zero_cost(self, tmp_path: Path) -> None:
        """Identical query with same topic/version/scope reuses persisted ledger without draining quota."""
        dispatcher = ResearchDispatcher(
            base_dir=tmp_path / "research",
            exploratory_budget=10.0,
        )

        req = ResearchRequest(
            question="Optimized Raft consensus log compaction",
            version="v1",
            scope="consensus",
            cost_estimate=0.50,
            is_blocking=False,
            sources_to_verify=[{"url": "https://raft.github.io/raft.pdf", "snippet": "Log compaction mechanisms"}],
        )

        # First run: consumes budget
        res1 = dispatcher.dispatch(req)
        assert res1.status == "success"
        assert res1.is_cached is False
        assert res1.actual_cost == 0.50
        assert dispatcher.exploratory_spent == 0.50
        assert dispatcher.deduplicated_queries_count == 0

        # Second run: identical query within TTL
        res2 = dispatcher.dispatch(req)
        assert res2.status == "cached"
        assert res2.is_cached is True
        assert res2.actual_cost == 0.0
        # Crucial check: spent budget DID NOT increase!
        assert dispatcher.exploratory_spent == 0.50
        assert dispatcher.deduplicated_queries_count == 1
        assert res2.ledger_id == res1.ledger_id
        assert res2.output_refs[0] == res1.output_refs[0]


# ===========================================================================
# 5. Contraprova E: Pesquisa bloqueante estrita à decisão e pools isolados
# ===========================================================================


class TestContraproofEBlockingDecisionAndIsolatedPools:
    def test_blocking_research_requires_decision_ref(self, tmp_path: Path) -> None:
        """Blocking research without an explicit decision_ref raises BlockingResearchMissingDecisionError."""
        dispatcher = ResearchDispatcher(base_dir=tmp_path / "research")

        req = ResearchRequest(
            question="Select cryptographic cipher suite",
            is_blocking=True,
            decision_ref=None,  # Missing decision link
        )
        with pytest.raises(BlockingResearchMissingDecisionError, match="strictly tied to a decision"):
            dispatcher.dispatch(req)

        req_empty_dec = ResearchRequest(
            question="Select cryptographic cipher suite",
            is_blocking=True,
            decision_ref="   ",
        )
        with pytest.raises(BlockingResearchMissingDecisionError):
            dispatcher.dispatch(req_empty_dec)

    def test_blocking_research_with_decision_ref_succeeds(self, tmp_path: Path) -> None:
        """Blocking research with a valid decision_ref succeeds and links to decision."""
        dispatcher = ResearchDispatcher(base_dir=tmp_path / "research")

        req = ResearchRequest(
            question="Evaluate SQLite WAL mode concurrency limits",
            is_blocking=True,
            decision_ref="ADR-0042-SQLITE-WAL",
            cost_estimate=0.10,
        )
        res = dispatcher.dispatch(req)
        assert res.status == "success"
        assert dispatcher.decision_spent == 0.10
        assert dispatcher.exploratory_spent == 0.0

        # Verify ledger has decision linked
        assert res.ledger_id is not None
        ledger = dispatcher.ledger_manager.load_ledger(res.ledger_id)
        assert ledger is not None
        assert "ADR-0042-SQLITE-WAL" in ledger.decisions_linked

    def test_exhausting_exploratory_budget_does_not_block_decision_research(self, tmp_path: Path) -> None:
        """Exploratory and decision pools are fully isolated; exploratory exhaustion does not starve decision pool."""
        dispatcher = ResearchDispatcher(
            base_dir=tmp_path / "research",
            decision_budget=10.0,
            exploratory_budget=0.20,  # Tight exploratory pool
        )

        # 1. Exhaust exploratory pool
        req_exp1 = ResearchRequest(
            question="Exploratory query 1: modern CSS layouts",
            is_blocking=False,
            cost_estimate=0.15,
        )
        dispatcher.dispatch(req_exp1)
        assert dispatcher.exploratory_spent == 0.15

        # Attempt next exploratory query that exceeds the budget
        req_exp2 = ResearchRequest(
            question="Exploratory query 2: animations",
            is_blocking=False,
            cost_estimate=0.10,  # 0.15 + 0.10 > 0.20
        )
        with pytest.raises(BudgetExceededError, match="Exploratory research budget pool exceeded"):
            dispatcher.dispatch(req_exp2)

        # 2. Blocking decision research MUST STILL SUCCEED because decision pool is separate
        req_dec = ResearchRequest(
            question="Critical DB replication schema choice",
            is_blocking=True,
            decision_ref="DEC-CRITICAL-01",
            cost_estimate=1.00,
        )
        res_dec = dispatcher.dispatch(req_dec)
        assert res_dec.status == "success"
        assert dispatcher.decision_spent == 1.00


# ===========================================================================
# 6. PrimarySourceCitation & Ledger Integrity
# ===========================================================================


class TestPrimarySourceLedgerIntegrity:
    def test_primary_source_citation_sha256_hash_and_conversion(self) -> None:
        """PrimarySourceCitation deterministically computes snippet SHA-256 and maps to ResearchSource."""
        snippet = "PostgreSQL multi-version concurrency control documentation extract."
        expected_hash = hashlib.sha256(snippet.strip().encode("utf-8")).hexdigest()

        citation = PrimarySourceCitation.create(
            source_id="pg_mvcc",
            url="https://postgresql.org/docs/mvcc",
            snippet=snippet,
            license="PostgreSQL",
            license_category=LicenseType.PERMISSIVE,
            decision_ref="DEC-DB-01",
        )

        assert citation.citation_hash == expected_hash
        assert citation.status == "verified"
        assert citation.verified is True

        rs = citation.to_research_source()
        assert rs.id == "pg_mvcc"
        assert rs.credibility_score == 1.0
        assert rs.metadata["citation_hash"] == expected_hash
        assert rs.metadata["decision_ref"] == "DEC-DB-01"


# ===========================================================================
# 7. Protocol Compliance & Failsafes for ResearchStageHandler
# ===========================================================================


class TestResearchStageHandler:
    def test_handler_protocol_conformance(self, tmp_path: Path) -> None:
        """ResearchStageHandler conforms to StageHandler protocol."""
        dispatcher = ResearchDispatcher(base_dir=tmp_path / "research")
        handler = ResearchStageHandler(dispatcher=dispatcher)

        assert isinstance(handler, StageHandler)
        assert handler.STAGE == "research"
        assert handler.VERSION == "v1"
        assert handler.descriptor.stage == "research"
        assert handler.descriptor.role == "researcher"

    def test_stale_lease_failsafe(self, tmp_path: Path) -> None:
        """Handler immediately fails with stale_lease if the claim lease has expired."""
        dispatcher = ResearchDispatcher(base_dir=tmp_path / "research")
        handler = ResearchStageHandler(dispatcher=dispatcher)

        # Context with expired timestamp
        expired_ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        ctx = _make_context(stage="research", expires_at=expired_ts)

        result = handler.handle(ctx)
        assert isinstance(result, StageResult)
        assert result.outcome == "failed"
        assert result.cause_code == "stale_lease"
        assert result.output_refs == []

    def test_successful_execution_emits_canonical_refs(self, tmp_path: Path) -> None:
        """Successful handler run emits non-empty canonical output_refs and evidence_refs."""
        dispatcher = ResearchDispatcher(base_dir=tmp_path / "research")
        handler = ResearchStageHandler(dispatcher=dispatcher)

        ctx = _make_context(
            stage="research",
            ticket_id="HF-10-02",
            input_refs=[
                "query: Memory and learning coordination patterns",
                "decision: DEC-HF-10-02",
            ],
        )

        result = handler.handle(ctx)
        assert result.outcome == "success"
        assert len(result.output_refs) >= 2
        assert any(ref.startswith("ref://research/ledger/") for ref in result.output_refs)
        assert any(ref.startswith("ref://research/digest/") for ref in result.output_refs)
        assert len(result.evidence_refs) >= 2
        assert any(ref.startswith("ref://evidence/research/") for ref in result.evidence_refs)


# ===========================================================================
# 8. Integration with build_handlers() and dispatch_stage()
# ===========================================================================


class TestBuildHandlersAndDispatchStageIntegration:
    def test_create_research_bindings_integrates_with_workflow_registry(self, tmp_path: Path) -> None:
        """create_research_bindings integrates directly with build_handlers() and dispatch_stage()."""
        dispatcher = ResearchDispatcher(base_dir=tmp_path / "research")
        bindings = create_research_bindings(dispatcher=dispatcher)

        # Build workflow handler registry with custom research binding
        registry = build_handlers(bindings=bindings)
        assert ("research", "v1") in registry
        registered_handler = registry.get_handler("research", "v1")
        assert isinstance(registered_handler, ResearchStageHandler)

        ctx = _make_context(stage="research", ticket_id="PRJ-AUTONOMY-10")
        result = dispatch_stage(registry, ctx, version="v1")

        assert isinstance(result, StageResult)
        assert result.outcome == "success"
        assert len(result.output_refs) > 0
        assert any("research" in ref for ref in result.output_refs)
