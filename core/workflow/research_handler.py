"""Research stage handler and binding factory for the autonomous workflow (HF-10-02).

Governed by:
- Universal Engineering Standards (AGENTS.md)
- docs/handoffs/continuous-autonomy/HF-10-02.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/workflow/control_contracts.py
- core/workflow/handlers.py
- core/learning/research_dispatch.py

Key Invariants:
1. Conforms strictly to StageHandler protocol: handle(StageContext) -> StageResult.
2. stage="research", role="researcher".
3. Failsafe for stale or expired claim lease.
4. Canonical output_refs and evidence_refs tied to deterministic research digest.
5. Factory function create_research_bindings(...) integrates directly with build_handlers() and dispatch_stage().
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core.learning.research_dispatch import (
    BlockingResearchMissingDecisionError,
    BudgetExceededError,
    ContractChangeUnauthorizedError,
    ResearchDispatcher,
    ResearchRequest,
    SourceUnavailableError,
)
from core.research.models import ResearchTopicType
from core.workflow.control_contracts import (
    HandlerDescriptor,
    JobKey,
    StageContext,
    StageResult,
)
from core.workflow.handlers import StageHandler

logger = logging.getLogger("darkfac.workflow.research_handler")

STAGE: str = "research"
VERSION: str = "v1"


class ResearchStageHandler:
    """StageHandler implementing autonomous decision-linked research execution (HF-10-02)."""

    STAGE: str = STAGE
    VERSION: str = VERSION

    descriptor: HandlerDescriptor = HandlerDescriptor(
        stage=STAGE,
        version=VERSION,
        input_schema_ref="schema://contracts/ResearchQuery",
        output_schema_ref="schema://contracts/ResearchLedger",
        role="researcher",
        required_capabilities=["search_web", "read_primary_source", "persist_ledger"],
        timeout_seconds=1800,
        conflict_scope="job",
    )

    def __init__(
        self,
        dispatcher: ResearchDispatcher | None = None,
        version: str = VERSION,
    ) -> None:
        self.dispatcher = dispatcher or ResearchDispatcher()
        self.version = version

    def handle(self, context: StageContext) -> StageResult:
        """Execute research stage within given StageContext enforcing all safety invariants."""
        jk = context.claim.job_key

        # -------------------------------------------------------------
        # 1. Failsafe: Stale / Expired Lease Check
        # -------------------------------------------------------------
        if context.claim.expires_at:
            try:
                exp_clean = context.claim.expires_at.replace("Z", "+00:00")
                exp_dt = datetime.fromisoformat(exp_clean)
                if exp_dt < datetime.now(UTC):
                    logger.warning(
                        "Research stage rejected: stale lease %s expired at %s",
                        context.claim.lease_id,
                        context.claim.expires_at,
                    )
                    return StageResult(
                        outcome="failed",
                        cause_code="stale_lease",
                        output_refs=[],
                        evidence_refs=[],
                        actual_cost=0.0,
                    )
            except Exception as exc:
                logger.debug("Failed parsing expires_at (%s): %s", context.claim.expires_at, exc)

        # -------------------------------------------------------------
        # 2. Extract research parameters from context
        # -------------------------------------------------------------
        ticket_id = jk.ticket_id
        plan_version = jk.plan_version or self.version
        scope = getattr(jk, "project_id", None) or ticket_id

        # Determine question, decision_ref, and blocking intent from input_refs
        question = f"Research authoritative evidence and architectural decisions for {ticket_id}"
        decision_ref: str | None = None
        is_blocking = False
        proposes_contract_change = False
        sources_to_verify: list[dict[str, Any]] = []

        for ref in context.input_refs:
            if ref.startswith("query:"):
                question = ref.removeprefix("query:").strip()
            elif ref.startswith("decision:") or ref.startswith("ref://decision/"):
                raw_dec = ref.split(":")[-1].strip()
                if raw_dec:
                    decision_ref = raw_dec
                    is_blocking = True
            elif ref == "flag:blocking":
                is_blocking = True
            elif ref == "flag:contract_change":
                proposes_contract_change = True
            elif ref.startswith("source:"):
                # Format: source:http_url
                url = ref.removeprefix("source:").strip()
                sources_to_verify.append({"url": url, "snippet": f"Citation from {url}"})

        # If blocking is requested but decision_ref not provided in refs, link to ticket decision
        if is_blocking and not decision_ref:
            decision_ref = f"decision_{ticket_id}"

        role = context.identity or "researcher"

        request = ResearchRequest(
            question=question,
            version=plan_version,
            scope=scope,
            topic_type=ResearchTopicType.TOPIC_CONCEPT,
            is_blocking=is_blocking,
            decision_ref=decision_ref,
            proposes_contract_change=proposes_contract_change,
            requested_by_role=role,
            sources_to_verify=sources_to_verify,
        )

        # -------------------------------------------------------------
        # 3. Dispatch structured research
        # -------------------------------------------------------------
        try:
            res = self.dispatcher.dispatch(request)
        except BlockingResearchMissingDecisionError:
            logger.warning("Research blocked: missing explicit decision_ref for blocking query.")
            return StageResult(
                outcome="failed",
                cause_code="missing_decision_ref",
                output_refs=[],
                evidence_refs=[f"ref://evidence/research/error/{jk.canonical_key()}"],
                actual_cost=0.0,
            )
        except ContractChangeUnauthorizedError:
            logger.warning("Research contract change unauthorized for role: %s", role)
            return StageResult(
                outcome="failed",
                cause_code="contract_change_unauthorized",
                output_refs=[],
                evidence_refs=[f"ref://evidence/research/error/{jk.canonical_key()}"],
                actual_cost=0.0,
            )
        except BudgetExceededError:
            logger.warning("Research budget exceeded.")
            return StageResult(
                outcome="waiting_budget",
                cause_code="budget_exceeded",
                output_refs=[],
                evidence_refs=[f"ref://evidence/research/budget_exceeded/{jk.canonical_key()}"],
                actual_cost=0.0,
            )
        except Exception as exc:
            logger.error("Research dispatcher encountered unhandled failure: %s", exc)
            return StageResult(
                outcome="failed",
                cause_code=f"dispatch_error: {exc}",
                output_refs=[],
                evidence_refs=[],
                actual_cost=0.0,
            )

        # -------------------------------------------------------------
        # 4. Map dispatch result to StageResult
        # -------------------------------------------------------------
        digest = res.request_digest
        canonical_key = jk.canonical_key()

        if res.status == "unverified_source":
            return StageResult(
                outcome="failed",
                cause_code="unverified_source",
                output_refs=[],
                evidence_refs=res.evidence_refs or [f"ref://evidence/source_unavailable/{digest}"],
                actual_cost=res.actual_cost,
            )

        if res.status == "re_research_scheduled":
            return StageResult(
                outcome="retry",
                cause_code="re_research_scheduled",
                output_refs=res.output_refs,
                evidence_refs=res.evidence_refs,
                actual_cost=res.actual_cost,
            )

        # Success or cached hit
        ledger_id = res.ledger_id or f"res_{digest[:16]}"
        output_refs = [
            f"ref://research/ledger/{ledger_id}",
            f"ref://research/digest/{digest}",
        ]
        evidence_refs = [
            f"ref://evidence/research/{canonical_key}",
            f"ref://sha/{digest}",
        ]

        return StageResult(
            outcome="success",
            output_refs=output_refs,
            evidence_refs=evidence_refs,
            operation_refs=[],
            actual_cost=res.actual_cost,
        )


def create_research_bindings(
    dispatcher: ResearchDispatcher | None = None,
    version: str = "v1",
) -> dict[str | tuple[str, str], StageHandler]:
    """Factory function providing canonical research stage handlers for build_handlers()."""
    handler = ResearchStageHandler(dispatcher=dispatcher, version=version)
    return {
        STAGE: handler,
        (STAGE, version): handler,
    }


__all__ = [
    "ResearchStageHandler",
    "STAGE",
    "VERSION",
    "create_research_bindings",
]
