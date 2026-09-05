"""
Session Learning Pack Generator & Cognitive Uplift Synthesizer.
Transforms detected patterns, session deltas, and architectural concepts
into high-bandwidth multi-tier learning packs with active recall cards.
"""

import uuid
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pathlib import Path

from core.learning_pack.models import (
    ConceptCategory,
    ExplanationTier,
    DefenseQA,
    TradeOffOption,
    LearningConcept,
    ActiveRecallCard,
    SessionLearningPack,
)
from core.learning_pack.analyzer import CodebaseAnalyzer


class LearningPackGenerator:
    """Generates complete, structured SessionLearningPack instances."""

    def __init__(self, analyzer: Optional[CodebaseAnalyzer] = None):
        self.analyzer = analyzer or CodebaseAnalyzer()

    def generate_pack(
        self,
        title: Optional[str] = None,
        session_id: Optional[str] = None,
        files_analyzed: Optional[List[str]] = None,
        custom_concepts: Optional[List[LearningConcept]] = None,
    ) -> SessionLearningPack:
        """Synthesizes a full learning pack from session context and detected code patterns."""
        now_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        pack_id = f"pack_{now_str}_{uuid.uuid4().hex[:6]}"
        active_session_id = session_id or f"session_{now_str}"

        target_files = files_analyzed or self.analyzer.get_modified_files()
        detected_patterns = self.analyzer.detect_patterns(target_files)

        concepts: List[LearningConcept] = []
        if custom_concepts:
            concepts.extend(custom_concepts)

        # Build concepts from detected patterns
        for idx, pat in enumerate(detected_patterns[:4]):  # Keep it high-yield (max 4 concepts)
            concept_id = f"concept_{pat['id']}"

            # Ensure uniqueness if custom concepts were passed
            if any(c.concept_id == concept_id for c in concepts):
                continue

            category = ConceptCategory.ARCHITECTURE
            try:
                category = ConceptCategory(pat.get("category", ConceptCategory.ARCHITECTURE.value))
            except ValueError:
                pass

            tiers = ExplanationTier(
                pitch_30s=pat.get("pitch_30s", "Clean architectural pattern implemented to boost system efficiency."),
                staff_architect=pat.get("staff_architect", "Architectural separation with deterministic guarantees."),
                under_the_hood=pat.get("under_the_hood", "Algorithms and data structures optimized for latency."),
            )

            defense_list = []
            if "defense_q" in pat and "defense_a" in pat:
                defense_list.append(
                    DefenseQA(
                        question=pat["defense_q"],
                        bulletproof_answer=pat["defense_a"],
                        context=pat.get("code_anchor"),
                    )
                )

            trade_offs = [
                TradeOffOption(
                    option=pat["name"],
                    pros="Deterministic safety, explicit guarantees, and zero-trust verification.",
                    cons="Requires additional domain models and initial boilerplate.",
                    why_chosen="Eliminates silent failures and enables 100% headless testing.",
                )
            ]

            concept = LearningConcept(
                concept_id=concept_id,
                name=pat["name"],
                category=category,
                mental_anchor=pat.get("default_metaphor", "A structured mechanical assembly where every gear is visible and tested."),
                tiers=tiers,
                trade_offs=trade_offs,
                defense=defense_list,
                code_anchor=pat.get("code_anchor"),
            )
            concepts.append(concept)

        # Generate active recall flashcards from concepts
        flashcards: List[ActiveRecallCard] = []
        for c in concepts:
            # Card 1: The 30s Pitch / Core Value
            flashcards.append(
                ActiveRecallCard(
                    card_id=f"card_{uuid.uuid4().hex[:8]}",
                    concept_id=c.concept_id,
                    front_prompt=f"How would you explain '{c.name}' in 30 seconds to a non-technical client or executive?",
                    back_solution=c.tiers.pitch_30s,
                    why_it_matters="Communicating technical value clearly to stakeholders builds trust and prevents misalignment.",
                    tags=[c.category.value, "Pitch", "Feynman"],
                )
            )

            # Card 2: The Defense QA (if available)
            if c.defense:
                d = c.defense[0]
                flashcards.append(
                    ActiveRecallCard(
                        card_id=f"card_{uuid.uuid4().hex[:8]}",
                        concept_id=c.concept_id,
                        front_prompt=f"Defense Shield: {d.question}",
                        back_solution=d.bulletproof_answer,
                        why_it_matters="Staff+ engineers must defend design decisions against valid skepticism with facts and trade-offs.",
                        tags=[c.category.value, "Architecture", "Defense"],
                    )
                )

        # Executive summary
        concept_names = [c.name for c in concepts]
        summary_text = (
            f"This session reinforced {len(concepts)} state-of-the-art engineering concepts: "
            f"{', '.join(concept_names)}. All implementations adhere to Universal Engineering Standards "
            f"with strict typing, headless reachability, and deterministic validation gates."
        )

        pack_title = title or f"Technical Mastery Pack: {concept_names[0] if concept_names else 'Dark Factory Engineering'}"

        metrics = {
            "concepts_count": len(concepts),
            "flashcards_count": len(flashcards),
            "files_analyzed_count": len(target_files),
            "estimated_read_time_min": max(2, len(concepts) * 2),
            "retention_score_potential": "High (Active Recall & Multi-Tier Grounding)",
        }

        return SessionLearningPack(
            pack_id=pack_id,
            session_id=active_session_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            title=pack_title,
            executive_summary=summary_text,
            concepts=concepts,
            flashcards=flashcards,
            files_analyzed=target_files,
            metrics=metrics,
        )
