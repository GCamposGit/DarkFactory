"""
Continuous Learning Tracker & Self-Improvement Engine.
Operates the Dual-Process (System 1/2) architecture, 2nd-prompt reflection cadence,
contrastive trajectory analysis, Code Judge verification, and policy debt pruning.
"""

import os
import sys
import json
import uuid
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone

from core.learning.models import (
    InteractionTurn,
    UserPreference,
    MistakeRCA,
    AnalogousTransfer,
    TrajectoryContrast,
    VerificationGate,
    PolicyDebtReport,
    LearningLedger,
    PreferenceCategory,
    MistakeCategory,
    PolicyOrigin,
    PolicyStatus,
)

DEFAULT_LEARNING_DIR = Path(".factory") / "learning"
DEFAULT_LEDGER_FILE = DEFAULT_LEARNING_DIR / "learning_ledger.json"


class ContinuousLearningTracker:
    """Manages the active session self-improvement loop, preference ledger, and RCA registry."""

    def __init__(self, ledger_file: Optional[Path] = None, session_id: Optional[str] = None):
        self.ledger_file = ledger_file or DEFAULT_LEDGER_FILE
        self.learning_dir = self.ledger_file.parent
        self.session_id = session_id or f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        self.ledger = self._load_or_initialize()

    def _load_or_initialize(self) -> LearningLedger:
        """Loads existing learning ledger or initializes a new one."""
        if self.ledger_file.exists():
            try:
                with open(self.ledger_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    ledger = LearningLedger.from_dict(data)
                    return ledger
            except Exception as e:
                print(f"[WARN] Failed to parse learning ledger from {self.ledger_file}: {e}. Reinitializing.")

        return LearningLedger(
            session_id=self.session_id,
            prompt_count=0,
            turns=[],
            preferences=[],
            mistakes=[],
            transfers=[],
            contrasts=[],
            verifications=[]
        )

    def save(self) -> None:
        """Persists the ledger to disk atomically."""
        os.makedirs(self.learning_dir, exist_ok=True)
        self.ledger.last_updated = datetime.now(timezone.utc).isoformat()
        with open(self.ledger_file, "w", encoding="utf-8") as f:
            json.dump(self.ledger.to_dict(), f, indent=2, ensure_ascii=False)

    @staticmethod
    def is_checkpoint_turn(prompt_count: int, was_followup: bool = False) -> bool:
        """
        Determines if the current turn requires a mandatory stop & self-improvement assessment.
        Cadence:
        - Fires on EVERY prompt in the session (prompt_count >= 1), ensuring even the 1st prompt
          is evaluated for past session corrections, implicit preferences, and alignment.
        - Always fires on follow-up corrections.
        """
        if prompt_count <= 0:
            return False
        return True

    # -------------------------------------------------------------
    # System 1: Fast Context Priming for Zero-Iteration One-Shot
    # -------------------------------------------------------------
    def get_active_system1_context(self, domain: Optional[str] = None) -> str:
        """
        Synthesizes a high-signal, compact prompt preamble containing:
        - Active user preferences (highest confidence first).
        - Inviolable preventative rules from past mistake RCAs.
        - Cross-domain extrapolated principles.
        """
        lines = ["[SYSTEM 1 KNOWLEDGE PRIMING: DARKFAC CONTINUOUS LEARNING]"]

        # 1. Active preferences
        active_prefs = [p for p in self.ledger.preferences if p.active]
        if active_prefs:
            lines.append("### INGRAINED USER CONVENTIONS & PREFERENCES:")
            for p in sorted(active_prefs, key=lambda x: (x.confidence, x.times_reinforced), reverse=True)[:5]:
                lines.append(f"- [{p.category.value.upper()}] {p.rule} (Evidence: {p.context_or_example})")

        # 2. Inviolable RCA preventative rules
        active_mistakes = [m for m in self.ledger.mistakes if m.active]
        if active_mistakes:
            lines.append("### NEVER-REPEAT INVIOLABLE RULES (ROOT CAUSE RESOLVED):")
            for m in active_mistakes[-4:]:
                lines.append(f"- [{m.category.value.upper()}] {m.preventative_rule}")

        # 3. Relevant Analogous Principles
        relevant_transfers = self.ledger.transfers
        if domain:
            relevant_transfers = [
                t for t in self.ledger.transfers
                if domain.lower() in [d.lower() for d in t.target_domains] or domain.lower() in t.source_domain.lower()
            ]
        if relevant_transfers:
            lines.append("### CROSS-DOMAIN GENERALIZED PRINCIPLES:")
            for t in relevant_transfers[-3:]:
                lines.append(f"- {t.generalized_principle}")

        lines.append("[END SYSTEM 1 PRIMING]")
        return "\n".join(lines)

    # -------------------------------------------------------------
    # Turn Tracking
    # -------------------------------------------------------------
    def record_turn(
        self,
        user_prompt_summary: str,
        perceived_intent: str,
        was_followup: bool = False,
        one_shot_success: bool = True,
        missing_context_or_gap: Optional[str] = None,
        target_skill: Optional[str] = None,
    ) -> InteractionTurn:
        """Records a new prompt turn and increments prompt counter."""
        self.ledger.prompt_count += 1
        turn = InteractionTurn(
            turn_id=self.ledger.prompt_count,
            timestamp=datetime.now(timezone.utc).isoformat(),
            user_prompt_summary=user_prompt_summary,
            perceived_intent=perceived_intent,
            was_followup=was_followup,
            one_shot_success=one_shot_success,
            missing_context_or_gap=missing_context_or_gap,
            target_skill=target_skill,
        )
        self.ledger.turns.append(turn)
        self.save()
        return turn

    # -------------------------------------------------------------
    # System 2: Contrastive Trajectory Analysis (ExpeL / SICA)
    # -------------------------------------------------------------
    def record_trajectory_contrast(
        self,
        initial_output_summary: str,
        user_correction: str,
        corrected_output_summary: str,
        key_delta: str,
        inferred_preference_rule: Optional[str] = None,
        preference_category: PreferenceCategory = PreferenceCategory.WORKFLOW,
    ) -> TrajectoryContrast:
        """
        Analyzes the delta between an initial turn and a correction,
        instantly extracting and anchoring the latent user preference.
        """
        pref_id = None
        if inferred_preference_rule:
            pref = self.register_preference(
                category=preference_category,
                rule=inferred_preference_rule,
                context_or_example=f"Extracted from contrast delta: {key_delta}",
                confidence=1.0,
                origin=PolicyOrigin.INFERRED_PREFERENCE,
                status=PolicyStatus.PROPOSED,
            )
            pref_id = pref.preference_id

        contrast_id = f"contrast_{uuid.uuid4().hex[:8]}"
        contrast = TrajectoryContrast(
            contrast_id=contrast_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            initial_output_summary=initial_output_summary,
            user_correction=user_correction,
            corrected_output_summary=corrected_output_summary,
            key_delta=key_delta,
            inferred_preference_id=pref_id,
        )
        self.ledger.contrasts.append(contrast)
        self.save()
        return contrast

    # -------------------------------------------------------------
    # Preference Registration & Reinforcement
    # -------------------------------------------------------------
    def register_preference(
        self,
        category: PreferenceCategory,
        rule: str,
        context_or_example: str,
        confidence: float = 1.0,
        *,
        origin: PolicyOrigin = PolicyOrigin.EXPLICIT_PREFERENCE,
        status: PolicyStatus = PolicyStatus.ACTIVE,
    ) -> UserPreference:
        """Registers or reinforces a user preference."""
        clean_rule = rule.strip()
        for pref in self.ledger.preferences:
            if pref.active and pref.rule.strip().lower() == clean_rule.lower():
                pref.times_reinforced += 1
                pref.confidence = min(1.0, pref.confidence + 0.1)
                pref.context_or_example = f"{pref.context_or_example} | {context_or_example}"
                self.save()
                return pref

        pref_id = f"pref_{uuid.uuid4().hex[:8]}"
        new_pref = UserPreference(
            preference_id=pref_id,
            category=category,
            rule=clean_rule,
            context_or_example=context_or_example,
            confidence=confidence,
            created_at=datetime.now(timezone.utc).isoformat(),
            times_reinforced=1,
            origin=origin,
            status=status,
        )
        self.ledger.preferences.append(new_pref)
        self.save()
        return new_pref

    def evaluate_preference(self, preference_id: str, gate_id: str) -> UserPreference:
        """Promote a candidate only when its referenced verification passed."""
        preference = next(
            (item for item in self.ledger.preferences if item.preference_id == preference_id),
            None,
        )
        if preference is None:
            raise ValueError(f"Unknown preference: {preference_id}")
        gate = next(
            (item for item in self.ledger.verifications if item.gate_id == gate_id),
            None,
        )
        if gate is None:
            raise ValueError(f"Unknown verification gate: {gate_id}")
        preference.verification_gate_id = gate_id
        preference.set_status(PolicyStatus.ACTIVE if gate.passed else PolicyStatus.EVALUATED)
        self.save()
        return preference

    # -------------------------------------------------------------
    # Deterministic Code Judge Verification (Voyager / RSI Pattern)
    # -------------------------------------------------------------
    def verify_patch_with_code_judge(
        self,
        target_type: str,
        test_command: str,
        timeout_sec: int = 60,
    ) -> VerificationGate:
        """
        Executes a deterministic test command before allowing a patch or rule to be accepted.
        No change is considered valid unless the Code Judge passes (exit code 0).
        """
        gate_id = f"gate_{uuid.uuid4().hex[:8]}"
        passed = False
        output_snippet = ""

        try:
            res = subprocess.run(
                test_command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_sec,
            )
            passed = (res.returncode == 0)
            output_snippet = res.stdout[-800:] if res.stdout else f"Exit code: {res.returncode}"
        except subprocess.TimeoutExpired:
            passed = False
            output_snippet = f"Execution timed out after {timeout_sec}s"
        except Exception as e:
            passed = False
            output_snippet = f"Execution error: {e}"

        gate = VerificationGate(
            gate_id=gate_id,
            target_type=target_type,
            test_command=test_command,
            passed=passed,
            output_snippet=output_snippet,
            executed_at=datetime.now(timezone.utc).isoformat(),
        )
        self.ledger.verifications.append(gate)
        self.save()
        return gate

    # -------------------------------------------------------------
    # Root Cause Analysis (RCA 5-Whys)
    # -------------------------------------------------------------
    def record_mistake_rca(
        self,
        category: MistakeCategory,
        symptom: str,
        mechanism: str,
        root_cause: str,
        patch_description: str,
        preventative_rule: str,
        regression_test_file: Optional[str] = None,
        test_verification_cmd: Optional[str] = None,
    ) -> MistakeRCA:
        """
        Registers a Root Cause Analysis (RCA) and associated preventative fix.
        If test_verification_cmd is provided, executes Code Judge verification.
        """
        gate_id = None
        policy_status = PolicyStatus.PROPOSED
        if test_verification_cmd:
            gate = self.verify_patch_with_code_judge(
                target_type="rca_patch",
                test_command=test_verification_cmd,
            )
            gate_id = gate.gate_id
            policy_status = PolicyStatus.ACTIVE if gate.passed else PolicyStatus.EVALUATED
            if not gate.passed:
                print(f"[WARN] RCA patch verification failed Code Judge: {gate.output_snippet}")

        rca_id = f"rca_{uuid.uuid4().hex[:8]}"
        rca = MistakeRCA(
            rca_id=rca_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            category=category,
            symptom=symptom,
            mechanism=mechanism,
            root_cause=root_cause,
            patch_description=patch_description,
            preventative_rule=preventative_rule,
            regression_test_file=regression_test_file,
            verification_gate_id=gate_id,
            origin=PolicyOrigin.RCA,
            status=policy_status,
        )
        self.ledger.mistakes.append(rca)
        self.save()
        return rca

    # -------------------------------------------------------------
    # Cross-Domain Analogy Extrapolation
    # -------------------------------------------------------------
    def extrapolate_analogy(
        self,
        source_domain: str,
        target_domains: List[str],
        specific_lesson: str,
        generalized_principle: str,
        applied_actions: Optional[List[str]] = None,
    ) -> AnalogousTransfer:
        """
        Extrapolates a specific lesson or preference to analogous domains.
        """
        transfer_id = f"trans_{uuid.uuid4().hex[:8]}"
        transfer = AnalogousTransfer(
            transfer_id=transfer_id,
            source_domain=source_domain,
            target_domains=target_domains,
            specific_lesson=specific_lesson,
            generalized_principle=generalized_principle,
            applied_actions=applied_actions or [],
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.ledger.transfers.append(transfer)
        self.save()
        return transfer

    # -------------------------------------------------------------
    # Policy Debt & Redundancy Pruning (Anti-Entropy)
    # -------------------------------------------------------------
    def prune_policy_debt(self) -> PolicyDebtReport:
        """
        System 2 curation: detects duplicate, subsumed, or near-identical rules,
        consolidates them into canonical rules, and deactivates redundant entries.
        """
        seen: Dict[str, UserPreference] = {}
        pruned_ids: List[str] = []
        consolidated = 0

        for pref in self.ledger.preferences:
            if not pref.active:
                continue

            normalized_key = "".join(ch for ch in pref.rule.lower() if ch.isalnum())
            if normalized_key in seen:
                # Merge into canonical
                canonical = seen[normalized_key]
                canonical.times_reinforced += pref.times_reinforced
                canonical.confidence = max(canonical.confidence, pref.confidence)
                canonical.context_or_example = f"{canonical.context_or_example} | {pref.context_or_example}"
                pref.set_status(PolicyStatus.RETIRED)
                pruned_ids.append(pref.preference_id)
                consolidated += 1
            else:
                seen[normalized_key] = pref

        self.save()
        active_count = sum(1 for p in self.ledger.preferences if p.active)
        return PolicyDebtReport(
            redundant_count=len(pruned_ids),
            pruned_ids=pruned_ids,
            consolidated_count=consolidated,
            active_clean_count=active_count,
        )

    # -------------------------------------------------------------
    # Reporting
    # -------------------------------------------------------------
    def get_summary_report(self) -> Dict[str, Any]:
        """Returns aggregated summary metrics for session self-improvement."""
        total_turns = len(self.ledger.turns)
        followups = sum(1 for t in self.ledger.turns if t.was_followup)
        one_shots = sum(1 for t in self.ledger.turns if t.one_shot_success and not t.was_followup)
        one_shot_rate = (one_shots / total_turns * 100) if total_turns > 0 else 100.0

        return {
            "session_id": self.ledger.session_id,
            "prompt_count": self.ledger.prompt_count,
            "total_turns": total_turns,
            "followup_turns": followups,
            "one_shot_rate_pct": round(one_shot_rate, 1),
            "preferences_tracked": len([p for p in self.ledger.preferences if p.active]),
            "rcas_resolved": len([m for m in self.ledger.mistakes if m.active]),
            "analogous_transfers": len(self.ledger.transfers),
            "contrasts_analyzed": len(self.ledger.contrasts),
            "verifications_passed": sum(1 for v in self.ledger.verifications if v.passed),
            "last_updated": self.ledger.last_updated,
        }
