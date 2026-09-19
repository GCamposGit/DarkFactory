"""Persistent Memory and Continuous Learning Service (HF-10).

Provides a unified facade coordinating:
- Context selection with strict project isolation and fail-closed active rule gating.
- Continuous learning candidate evaluation and promotion with empirical supporting runs.
- Persistent research dossier recording with source integrity, linked decisions, and related tickets (Scenario G7).
- Non-blocking Owner Learning Pack generation with optional discussion topics.
- Cold-restart reloads to verify durable persistence across process lifecycles.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from core.learning.models import (
    LearningLedger,
    PolicyOrigin,
    PolicyStatus,
    UserPreference,
    MistakeRCA,
    PreferenceCategory,
)
from core.learning.promotion import (
    LearningCandidate,
    LearningPromotionEngine,
    PromotionDeniedError,
)
from core.learning.tracker import ContinuousLearningTracker
from core.learning_pack.generator import LearningPackGenerator
from core.learning_pack.models import LearningConcept, SessionLearningPack
from core.learning_pack.storage import LearningPackStore
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.orchestrator.context import ContextSelector, TaskContext
from core.execution.agent_executor import TaskSpec
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


class PersistentMemoryService:
    """Unified service for persistent memory, self-learning, research ledger, and owner learning packs."""

    def __init__(
        self,
        base_dir: Path | str | None = None,
        session_id: Optional[str] = None,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir else (project_root() / ".factory")
        self.learning_dir = self.base_dir / "learning"
        self.research_dir = self.base_dir / "research"
        self.learning_packs_dir = self.base_dir / "learning_packs"
        self.session_id = session_id or f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        self.learning_dir.mkdir(parents=True, exist_ok=True)
        self.research_dir.mkdir(parents=True, exist_ok=True)
        self.learning_packs_dir.mkdir(parents=True, exist_ok=True)

        self.reload()

    def reload(self) -> None:
        """Re-initializes all sub-components from disk to simulate a cold restart."""
        self.tracker = ContinuousLearningTracker(
            ledger_file=self.learning_dir / "learning_ledger.json",
            session_id=self.session_id,
        )
        self.promotion_engine = LearningPromotionEngine(
            storage_path=self.learning_dir / "candidates.json"
        )
        from core.orchestrator.context import ContextSelector

        self.context_selector = ContextSelector(promotion_engine=self.promotion_engine)
        self.research_manager = KnowledgeLedgerManager(base_dir=self.research_dir)
        self.pack_generator = LearningPackGenerator()
        self.pack_store = LearningPackStore(storage_dir=self.learning_packs_dir)

    # -------------------------------------------------------------
    # Context Assembly & Project Isolation (Fail-Closed)
    # -------------------------------------------------------------
    def get_selective_context(
        self,
        task_spec: TaskSpec,
        project_id: Optional[str] = None,
        checkpoint: Optional[dict[str, Any]] = None,
        max_summary_tokens: int = 500,
    ) -> TaskContext:
        """Synthesize bounded, selective task context with strict project isolation.

        Enforces:
        - Only rules with status == ACTIVE are included.
        - Rules matching another project_id are excluded.
        - Global/general rules are included if scope is relevant.
        """
        return self.context_selector.assemble_context(
            task_spec=task_spec,
            project_id=project_id,
            tracker=self.tracker,
            promotion_engine=self.promotion_engine,
            checkpoint=checkpoint,
            max_summary_tokens=max_summary_tokens,
        )

    # -------------------------------------------------------------
    # Candidate Registration, Evaluation & Promotion
    # -------------------------------------------------------------
    def register_candidate(
        self,
        rule_id: str,
        origin: PolicyOrigin | str,
        scope: str,
        rule_content: str = "",
        supporting_runs: Optional[list[str]] = None,
        eval_version: str = "",
        project_id: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> LearningCandidate:
        """Register a new candidate in PROPOSED status with optional project affiliation."""
        meta = dict(metadata or {})
        if project_id:
            meta["project_id"] = project_id

        return self.promotion_engine.register_candidate(
            rule_id=rule_id,
            origin=origin,
            scope=scope,
            supporting_runs=supporting_runs,
            eval_version=eval_version,
            rule_content=rule_content,
            metadata=meta,
        )

    def evaluate_and_promote(
        self,
        rule_id: str,
        eval_version: str,
        passed: bool,
        supporting_runs: Optional[list[str]] = None,
        test_output: str = "",
        error_message: str = "",
    ) -> LearningCandidate:
        """Evaluate a candidate and promote to ACTIVE if passed; otherwise record failure."""
        candidate = self.promotion_engine.get_candidate(rule_id)
        if supporting_runs:
            for run in supporting_runs:
                if run not in candidate.supporting_runs:
                    candidate.supporting_runs.append(run)
            self.promotion_engine.save()

        if passed:
            self.promotion_engine.record_eval_pass(
                rule_id=rule_id,
                eval_version=eval_version,
                test_output=test_output,
            )
            return self.promotion_engine.promote_candidate(
                rule_id=rule_id,
                verified_eval_version=eval_version,
            )
        else:
            return self.promotion_engine.record_eval_failure(
                rule_id=rule_id,
                eval_version=eval_version,
                error_message=error_message,
            )

    def list_active_rules(self, project_id: Optional[str] = None) -> list[LearningCandidate]:
        """Return all active rules, optionally isolated to a specific project or global."""
        active = self.promotion_engine.get_active_candidates()
        if not project_id:
            return active

        norm_target = project_id.strip().lower()
        matched: list[LearningCandidate] = []
        for c in active:
            cand_proj = (c.metadata.get("project_id") or "").strip().lower()
            if not cand_proj or cand_proj in ("global", "general", "*") or cand_proj == norm_target:
                matched.append(c)
        return matched

    def compute_memory_version(self, project_id: Optional[str] = None) -> str:
        """Compute a deterministic hash representing the active memory rules state."""
        active_rules = self.list_active_rules(project_id=project_id)
        if not active_rules:
            scope_tag = (project_id or "global").strip().lower()
            digest = hashlib.sha256(f"empty:{scope_tag}".encode("utf-8")).hexdigest()
            return f"mem-v1:{digest[:16]}"

        sorted_rules = sorted(active_rules, key=lambda r: r.rule_id)
        tokens: list[str] = []
        for r in sorted_rules:
            tokens.append(f"{r.rule_id}:{r.eval_version}:{r.scope}:{r.rule_content}")
        payload = "|".join(tokens)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"mem-v1:{digest[:16]}"

    # -------------------------------------------------------------
    # Research Dossier Persistence (Scenario G7)
    # -------------------------------------------------------------
    def record_research(
        self,
        query: str,
        topic_type: ResearchTopicType | str,
        sources: list[ResearchSource],
        insights: list[SourceInsight],
        summary_executive: str = "",
        decisions_linked: Optional[list[str]] = None,
        related_tickets: Optional[list[str]] = None,
        ledger_id: Optional[str] = None,
    ) -> ResearchLedger:
        """Persist an authoritative research dossier with canonical source URLs, insights, and linked decisions."""
        norm_type = ResearchTopicType(topic_type) if isinstance(topic_type, str) else topic_type
        ledger = self.research_manager.create_ledger(
            query=query,
            topic_type=norm_type,
            ledger_id=ledger_id,
        )

        for src in sources:
            ledger.add_source(src)

        for ins in insights:
            ledger.add_insight(ins)

        ledger.summary_executive = summary_executive
        ledger.decisions_linked = list(decisions_linked or [])
        ledger.related_tickets = list(related_tickets or [])

        self.research_manager.save_ledger(ledger)
        return ledger

    def load_research(self, ledger_id: str) -> Optional[ResearchLedger]:
        """Load an authoritative research ledger from storage."""
        return self.research_manager.load_ledger(ledger_id)

    # -------------------------------------------------------------
    # Owner Learning Pack (Feynman Multi-Tier, Non-blocking)
    # -------------------------------------------------------------
    def generate_owner_learning_pack(
        self,
        title: Optional[str] = None,
        session_id: Optional[str] = None,
        files_analyzed: Optional[list[str]] = None,
        custom_concepts: Optional[list[LearningConcept]] = None,
        discussion_topics: Optional[list[str]] = None,
        save_to_disk: bool = True,
    ) -> SessionLearningPack:
        """Generate a structured owner learning pack with 2-3 discussion topics.

        Guarantees:
        - reading_is_optional == True
        - blocks_production == False
        """
        pack = self.pack_generator.generate_pack(
            title=title,
            session_id=session_id or self.session_id,
            files_analyzed=files_analyzed,
            custom_concepts=custom_concepts,
            discussion_topics=discussion_topics,
        )

        # Enforce non-blocking invariants
        pack.reading_is_optional = True
        pack.blocks_production = False

        if save_to_disk:
            self.pack_store.save_pack(pack)

        return pack

    def load_learning_pack(self, pack_id: str) -> Optional[SessionLearningPack]:
        """Retrieve a learning pack by ID or 'latest'."""
        return self.pack_store.load_pack(pack_id)


__all__ = [
    "PersistentMemoryService",
]
