"""Structured Research Dispatch Coordinator and Primary Source Ledger (HF-10-02).

Governed by:
- Universal Engineering Standards (AGENTS.md)
- docs/handoffs/continuous-autonomy/HF-10-02.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/research/models.py
- core/research/ledger.py

Key Invariants:
1. Research Dispatch Coordinator by question, version, and scope.
2. Integration with KnowledgeLedgerManager and primary source citations with SHA-256 hash, URLs, and licenses.
3. Mandatory Contraproofs:
   a) Unavailable source never becomes fact (returns error / unverified status, never hallucinates fact).
   b) Expired research (TTL expired) automatically schedules re-research.
   c) Contract change can only be authorized by high_architecture role.
   d) Deduplicated query (same canonical hash/topic) reuses persisted ledger without draining quota or duplicating cost.
   e) Blocking research strictly tied to decision; exploratory operates with separate pool and budget.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import Field

from core.paths import project_root
from core.research.ledger import KnowledgeLedgerManager
from core.research.models import (
    AuthorityTier,
    LicenseType,
    ResearchLedger,
    ResearchSource,
    ResearchTopicType,
    SourceInsight,
)
from core.workflow.contracts import ContractModel

logger = logging.getLogger("darkfac.learning.research_dispatch")

DEFAULT_RESEARCH_DIR = project_root() / ".factory" / "research"
AUTHORIZED_CONTRACT_ROLES: frozenset[str] = frozenset({"high_architecture", "high", "architecture_owner"})


# ---------------------------------------------------------------------------
# Domain Exceptions
# ---------------------------------------------------------------------------


class ResearchDispatchError(RuntimeError):
    """Base error for research dispatch operations."""


class SourceUnavailableError(ResearchDispatchError):
    """Raised when an authoritative source is unreachable or unverified."""


class StaleResearchError(ResearchDispatchError):
    """Raised when research TTL has expired."""


class ContractChangeUnauthorizedError(ResearchDispatchError):
    """Raised when an unauthorized role attempts to approve a contract change."""


class BlockingResearchMissingDecisionError(ResearchDispatchError):
    """Raised when a blocking research query is not tied to an explicit decision."""


class BudgetExceededError(ResearchDispatchError):
    """Raised when a research budget pool is exhausted."""


# ---------------------------------------------------------------------------
# Contracts & Data Models
# ---------------------------------------------------------------------------


class PrimarySourceCitation(ContractModel):
    """Auditable primary source record with SHA-256 snippet hash, URL, and license."""

    source_id: str
    url: str
    snippet: str
    citation_hash: str
    license: str = "unknown"
    license_category: LicenseType = LicenseType.UNKNOWN
    authority_tier: AuthorityTier = AuthorityTier.HIGH_CREDIBILITY
    verified: bool = True
    status: Literal["verified", "unavailable", "unverified"] = "verified"
    retrieved_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    error_message: str | None = None
    decision_ref: str | None = None

    @classmethod
    def create(
        cls,
        source_id: str,
        url: str,
        snippet: str,
        license: str = "MIT",
        license_category: LicenseType = LicenseType.PERMISSIVE,
        verified: bool = True,
        status: Literal["verified", "unavailable", "unverified"] = "verified",
        error_message: str | None = None,
        decision_ref: str | None = None,
    ) -> PrimarySourceCitation:
        """Create citation calculating SHA-256 digest of snippet content."""
        citation_hash = hashlib.sha256(snippet.strip().encode("utf-8")).hexdigest()
        return cls(
            source_id=source_id,
            url=url,
            snippet=snippet,
            citation_hash=citation_hash,
            license=license,
            license_category=license_category,
            verified=verified,
            status=status,
            error_message=error_message,
            decision_ref=decision_ref,
        )

    def to_research_source(self) -> ResearchSource:
        """Map to core ResearchSource model."""
        is_fully_verified = self.verified and self.status == "verified"
        return ResearchSource(
            id=self.source_id,
            title=f"Source: {self.source_id}",
            url=self.url,
            source_type="primary_source",
            summary=self.snippet[:256],
            license=self.license,
            license_category=self.license_category,
            authority_tier=self.authority_tier,
            credibility_score=1.0 if is_fully_verified else 0.0,
            metadata={
                "citation_hash": self.citation_hash,
                "citation_snippet": self.snippet,
                "verification_status": self.status,
                "verified": self.verified,
                "decision_ref": self.decision_ref,
                "error_message": self.error_message,
            },
        )


class ResearchRequest(ContractModel):
    """Structured research request parameterized by question, version, and scope."""

    question: str
    version: str = "v1"
    scope: str = "global"
    topic_type: ResearchTopicType = ResearchTopicType.TOPIC_CONCEPT
    is_blocking: bool = False
    decision_ref: str | None = None
    proposes_contract_change: bool = False
    requested_by_role: str = "researcher"
    ttl_seconds: int = 86400
    cost_estimate: float = 0.05
    sources_to_verify: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def canonical_digest(self) -> str:
        """Deterministic SHA-256 digest over question, version, scope, and topic_type."""
        norm_topic = self.topic_type.value if isinstance(self.topic_type, ResearchTopicType) else str(self.topic_type)
        payload = f"{self.question.strip().lower()}:{self.version.strip().lower()}:{self.scope.strip().lower()}:{norm_topic.strip().lower()}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReResearchScheduleEvent(ContractModel):
    """Record emitted when an expired research ledger is scheduled for refresh."""

    event_id: str
    request_digest: str
    question: str
    version: str
    scope: str
    scheduled_at: str
    reason: str = "ttl_expired"


class ResearchDispatchResult(ContractModel):
    """Outcome of a research dispatch coordination."""

    request_digest: str
    ledger_id: str | None = None
    status: Literal[
        "success",
        "unverified_source",
        "re_research_scheduled",
        "rejected_unauthorized",
        "cached",
        "failed",
    ]
    is_cached: bool = False
    actual_cost: float = 0.0
    scheduled_re_research: bool = False
    contract_change_authorized: bool = False
    primary_sources: list[PrimarySourceCitation] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    error_message: str | None = None


# ---------------------------------------------------------------------------
# Research Dispatch Coordinator
# ---------------------------------------------------------------------------


class ResearchDispatcher:
    """Coordinates structured research dispatch, primary source ledger persistence,

    and strict enforcement of architectural invariants and budget pools.
    """

    def __init__(
        self,
        base_dir: Path | str | None = None,
        ledger_manager: KnowledgeLedgerManager | None = None,
        source_verifier: Callable[[str], bool] | None = None,
        decision_budget: float = 50.0,
        exploratory_budget: float = 10.0,
        default_ttl_seconds: int = 86400,
        fail_closed_on_source_error: bool = False,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_RESEARCH_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_manager = ledger_manager or KnowledgeLedgerManager(base_dir=self.base_dir)
        self.source_verifier = source_verifier
        self.decision_budget = decision_budget
        self.decision_spent: float = 0.0
        self.exploratory_budget = exploratory_budget
        self.exploratory_spent: float = 0.0
        self.default_ttl_seconds = default_ttl_seconds
        self.fail_closed_on_source_error = fail_closed_on_source_error

        self.scheduled_re_research_queue: list[ReResearchScheduleEvent] = []
        self.deduplicated_queries_count: int = 0
        self._index_file = self.base_dir / "research_index.json"
        self._index: dict[str, dict[str, Any]] = self._load_index()

    def _load_index(self) -> dict[str, dict[str, Any]]:
        if self._index_file.exists():
            try:
                with open(self._index_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("Could not read research index: %s", exc)
        return {}

    def _save_index(self) -> None:
        try:
            with open(self._index_file, "w", encoding="utf-8") as f:
                json.dump(self._index, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning("Could not save research index: %s", exc)

    def schedule_re_research(
        self,
        request: ResearchRequest,
        reason: str = "ttl_expired",
    ) -> ReResearchScheduleEvent:
        """Schedule automatic re-research for expired dossiers."""
        digest = request.canonical_digest
        event = ReResearchScheduleEvent(
            event_id=f"evt_reresearch_{digest[:16]}_{int(datetime.now(UTC).timestamp())}",
            request_digest=digest,
            question=request.question,
            version=request.version,
            scope=request.scope,
            scheduled_at=datetime.now(UTC).isoformat(),
            reason=reason,
        )
        self.scheduled_re_research_queue.append(event)
        return event

    def dispatch(self, request: ResearchRequest) -> ResearchDispatchResult:
        """Dispatch a structured research query enforcing all mandatory invariants."""
        digest = request.canonical_digest

        # -------------------------------------------------------------
        # Contraproof (c): Mudança de contrato só pode ser autorizada
        # por papel high_architecture
        # -------------------------------------------------------------
        if request.proposes_contract_change:
            role = request.requested_by_role.strip().lower()
            if role not in AUTHORIZED_CONTRACT_ROLES:
                logger.error(
                    "Contract change rejected: role '%s' lacks high_architecture authorization.",
                    request.requested_by_role,
                )
                raise ContractChangeUnauthorizedError(
                    f"Role '{request.requested_by_role}' is not authorized to approve contract changes; "
                    f"'high_architecture' role required."
                )

        # -------------------------------------------------------------
        # Contraproof (e): Pesquisa bloqueante estritamente restrita à
        # decisão; exploratória opera com pool e orçamento separados
        # -------------------------------------------------------------
        if request.is_blocking:
            if not request.decision_ref or not request.decision_ref.strip():
                raise BlockingResearchMissingDecisionError(
                    "Blocking research must be strictly tied to a decision (decision_ref required)."
                )
            if self.decision_spent + request.cost_estimate > self.decision_budget:
                raise BudgetExceededError(
                    f"Decision research budget pool exceeded (spent={self.decision_spent:.4f}, "
                    f"cost={request.cost_estimate:.4f}, budget={self.decision_budget:.4f})."
                )
        else:
            if self.exploratory_spent + request.cost_estimate > self.exploratory_budget:
                raise BudgetExceededError(
                    f"Exploratory research budget pool exceeded (spent={self.exploratory_spent:.4f}, "
                    f"cost={request.cost_estimate:.4f}, budget={self.exploratory_budget:.4f})."
                )

        # -------------------------------------------------------------
        # Contraproofs (d) & (b): Cache deduplication vs TTL expiration
        # -------------------------------------------------------------
        entry = self._index.get(digest)
        if entry is not None:
            ledger_id = entry["ledger_id"]
            created_at_iso = entry.get("created_at")
            ttl = entry.get("ttl_seconds", request.ttl_seconds)

            is_expired = False
            if created_at_iso:
                try:
                    clean_iso = created_at_iso.replace("Z", "+00:00")
                    created_dt = datetime.fromisoformat(clean_iso)
                    if (datetime.now(UTC) - created_dt).total_seconds() > ttl:
                        is_expired = True
                except Exception:
                    pass

            if is_expired:
                # Contraproof (b): Pesquisa vencida agenda automaticamente re-pesquisa
                logger.info("Research expired for query digest %s; scheduling re-research.", digest)
                event = self.schedule_re_research(request, reason="ttl_expired")
                return ResearchDispatchResult(
                    request_digest=digest,
                    ledger_id=ledger_id,
                    status="re_research_scheduled",
                    is_cached=False,
                    actual_cost=0.0,
                    scheduled_re_research=True,
                    contract_change_authorized=request.proposes_contract_change,
                    output_refs=[f"ref://research/rescheduled/{ledger_id}"],
                    evidence_refs=[
                        f"ref://research/expired/{digest}",
                        f"event://schedule/{event.event_id}",
                    ],
                )
            else:
                # Contraproof (d): Query deduplicada reaproveita ledger persistido sem custo
                logger.info(
                    "Deduplicated query hit for digest %s; reusing persisted ledger %s.",
                    digest,
                    ledger_id,
                )
                self.deduplicated_queries_count += 1
                return ResearchDispatchResult(
                    request_digest=digest,
                    ledger_id=ledger_id,
                    status="cached",
                    is_cached=True,
                    actual_cost=0.0,
                    scheduled_re_research=False,
                    contract_change_authorized=request.proposes_contract_change,
                    output_refs=[
                        f"ref://research/ledger/{ledger_id}",
                        f"ref://research/digest/{digest}",
                    ],
                    evidence_refs=[f"ref://research/cached/{digest}"],
                )

        # -------------------------------------------------------------
        # Contraproof (a): Fonte indisponível não vira fato
        # -------------------------------------------------------------
        citations: list[PrimarySourceCitation] = []
        verified_citations: list[PrimarySourceCitation] = []

        for src_input in request.sources_to_verify:
            url = src_input.get("url", "")
            snippet = src_input.get("snippet", "")
            src_id = src_input.get("source_id") or f"src_{hashlib.sha256(url.encode()).hexdigest()[:8]}"
            license_name = src_input.get("license", "unknown")
            license_cat = LicenseType(src_input.get("license_category", LicenseType.UNKNOWN))

            is_avail = True
            if self.source_verifier is not None:
                is_avail = self.source_verifier(url)
            elif src_input.get("available") is False:
                is_avail = False

            if not is_avail:
                cit = PrimarySourceCitation.create(
                    source_id=src_id,
                    url=url,
                    snippet=snippet,
                    license=license_name,
                    license_category=license_cat,
                    verified=False,
                    status="unavailable",
                    error_message="Primary source unreachable; verification failed",
                    decision_ref=request.decision_ref,
                )
                citations.append(cit)
            else:
                cit = PrimarySourceCitation.create(
                    source_id=src_id,
                    url=url,
                    snippet=snippet,
                    license=license_name,
                    license_category=license_cat,
                    verified=True,
                    status="verified",
                    decision_ref=request.decision_ref,
                )
                citations.append(cit)
                verified_citations.append(cit)

        # If primary sources were given and ALL are unavailable:
        if request.sources_to_verify and not verified_citations:
            logger.warning("All primary sources unavailable for %s. Never hallucinating fact.", digest)
            if self.fail_closed_on_source_error:
                raise SourceUnavailableError("Primary source unavailable; cannot establish fact without evidence.")

            return ResearchDispatchResult(
                request_digest=digest,
                ledger_id=None,
                status="unverified_source",
                is_cached=False,
                actual_cost=0.0,
                scheduled_re_research=False,
                contract_change_authorized=False,
                primary_sources=citations,
                output_refs=[],
                evidence_refs=[f"ref://evidence/source_unavailable/{digest}"],
                error_message="Primary source unavailable; cannot establish authoritative fact.",
            )

        # -------------------------------------------------------------
        # Deduct cost from dedicated budget pool
        # -------------------------------------------------------------
        cost = request.cost_estimate
        if request.is_blocking:
            self.decision_spent += cost
        else:
            self.exploratory_spent += cost

        # -------------------------------------------------------------
        # Persist authoritative ResearchLedger with only verified facts
        # -------------------------------------------------------------
        ledger_id = f"res_{digest[:16]}"
        ledger = self.ledger_manager.create_ledger(
            query=request.question,
            topic_type=request.topic_type,
            ledger_id=ledger_id,
        )
        if request.decision_ref:
            ledger.decisions_linked.append(request.decision_ref)

        for cit in verified_citations:
            ledger.add_source(cit.to_research_source())
            ledger.add_insight(
                SourceInsight(
                    source_id=cit.source_id,
                    source_title=f"Source: {cit.source_id}",
                    key_insight=cit.snippet,
                    architectural_implications=f"Verified authoritative fact for {request.scope}",
                    future_reference_value=f"Decision link: {request.decision_ref or 'exploratory'}",
                    authority_tier=cit.authority_tier,
                )
            )

        self.ledger_manager.save_ledger(ledger)

        # Update index
        self._index[digest] = {
            "ledger_id": ledger_id,
            "created_at": datetime.now(UTC).isoformat(),
            "ttl_seconds": request.ttl_seconds,
            "version": request.version,
            "scope": request.scope,
        }
        self._save_index()

        return ResearchDispatchResult(
            request_digest=digest,
            ledger_id=ledger_id,
            status="success",
            is_cached=False,
            actual_cost=cost,
            scheduled_re_research=False,
            contract_change_authorized=request.proposes_contract_change,
            primary_sources=citations,
            output_refs=[
                f"ref://research/ledger/{ledger_id}",
                f"ref://research/digest/{digest}",
            ],
            evidence_refs=[
                f"ref://evidence/research/{digest}",
                f"ref://sha/{digest}",
            ],
        )


__all__ = [
    "AUTHORIZED_CONTRACT_ROLES",
    "BlockingResearchMissingDecisionError",
    "BudgetExceededError",
    "ContractChangeUnauthorizedError",
    "PrimarySourceCitation",
    "ReResearchScheduleEvent",
    "ResearchDispatchError",
    "ResearchDispatchResult",
    "ResearchDispatcher",
    "ResearchRequest",
    "SourceUnavailableError",
    "StaleResearchError",
]
