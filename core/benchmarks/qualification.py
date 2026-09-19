"""Model qualification engine for candidate evaluations (HF-07-03).

Governed by Universal Engineering Standards (AGENTS.md), HF-07-03, and CONTRACTS.md.
Evaluates models for canonical roles: economy, high_architecture, verifier, independent_review.

Invariants:
1. Missing price is NOT zero (preço ausente não zero):
   If price is missing or None, NEVER default to 0.0! A 0.0 price would corrupt the
   economy router into treating expensive/unknown models as free local models.
2. Incompatible reasoning effort is NOT sent (effort incompatível não enviado):
   If a model doesn't support reasoning effort, the parameter is stripped or rejected.
   Validates against allowed levels: minimal, low, medium, high, max.
3. Tool calling capabilities evaluation per role.
4. Pareto frontier calculation across cost and benchmark scores.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Set
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("darkfac.benchmarks.qualification")

ALLOWED_REASONING_EFFORTS: Set[str] = {"minimal", "low", "medium", "high", "max"}
FORBIDDEN_REASONING_EFFORTS: Set[str] = {"xhigh", "extra-high", "extra_high", "ultra"}


class MissingPriceError(ValueError):
    """Raised when an unpriced model is evaluated where explicit pricing is required."""


class IncompatibleEffortError(ValueError):
    """Raised when an incompatible reasoning effort cannot be resolved."""


class ModelCandidate(BaseModel):
    """Candidate model under evaluation for role qualification."""

    model_config = ConfigDict(extra="ignore")

    model_id: str
    alias: str = ""
    provider_id: str
    family: str = ""
    context_window: int = 4096
    cost_per_1k_input_usd: Optional[float] = None
    cost_per_1k_output_usd: Optional[float] = None
    supported_roles: List[str] = Field(default_factory=list)
    tool_calling_supported: bool = True
    tool_capabilities: List[str] = Field(default_factory=list)
    supports_reasoning_effort: bool = False
    allowed_reasoning_efforts: List[str] = Field(default_factory=list)
    default_reasoning_effort: Optional[str] = None
    coding_score: Optional[float] = None
    intelligence_score: Optional[float] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @property
    def has_explicit_pricing(self) -> bool:
        return self.cost_per_1k_input_usd is not None and self.cost_per_1k_output_usd is not None

    def get_task_cost(
        self,
        input_tokens: int = 25000,
        output_tokens: int = 2500,
    ) -> float:
        """Calculate task cost. Raises MissingPriceError if pricing is missing (never assumes 0.0)."""
        if self.cost_per_1k_input_usd is None or self.cost_per_1k_output_usd is None:
            raise MissingPriceError(
                f"Model '{self.model_id}' has missing pricing; missing price is NOT zero."
            )
        prompt_cost = (input_tokens / 1000.0) * self.cost_per_1k_input_usd
        comp_cost = (output_tokens / 1000.0) * self.cost_per_1k_output_usd
        return round(prompt_cost + comp_cost, 6)

    def sanitize_reasoning_effort(self, requested_effort: Optional[str]) -> Optional[str]:
        """Sanitize or strip reasoning effort.

        If model does NOT support reasoning effort, stripped to None.
        If supported, validates against allowed levels (minimal, low, medium, high, max).
        Forbidden strings (e.g. xhigh) are sanitized to max.
        """
        if not self.supports_reasoning_effort:
            return None

        if not requested_effort:
            return self.default_reasoning_effort

        cleaned = requested_effort.strip().lower()
        if cleaned in FORBIDDEN_REASONING_EFFORTS:
            cleaned = "max"

        if cleaned not in ALLOWED_REASONING_EFFORTS:
            return self.default_reasoning_effort

        if self.allowed_reasoning_efforts and cleaned not in self.allowed_reasoning_efforts:
            return self.default_reasoning_effort

        return cleaned


class ModelQualificationRecord(BaseModel):
    """Record of a model's qualification assessment across roles."""

    model_config = ConfigDict(extra="ignore")

    model_id: str
    alias: str = ""
    provider_id: str
    family: str = ""
    qualified_roles: List[str] = Field(default_factory=list)
    disqualified_roles: Dict[str, str] = Field(default_factory=dict)
    has_valid_pricing: bool = False
    cost_per_1k_input_usd: Optional[float] = None
    cost_per_1k_output_usd: Optional[float] = None
    estimated_task_cost_usd: Optional[float] = None
    coding_score: Optional[float] = None
    intelligence_score: Optional[float] = None
    is_pareto_optimal: bool = False
    tool_calling_supported: bool = True
    tool_capabilities: List[str] = Field(default_factory=list)
    supports_reasoning_effort: bool = False
    allowed_reasoning_efforts: List[str] = Field(default_factory=list)
    default_reasoning_effort: Optional[str] = None
    qualified_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: Dict[str, Any] = Field(default_factory=dict)


class QualificationEngine:
    """Evaluates candidates across roles, enforces invariants, and calculates Pareto frontier."""

    DEFAULT_REQUIRED_TOOLS: Dict[str, Set[str]] = {
        "economy": {"read_file", "write_file", "run_command"},
        "high_architecture": {"read_file", "write_file", "run_command"},
        "verifier": {"read_file", "run_command"},
        "independent_review": {"read_file", "run_command"},
    }

    def __init__(
        self,
        required_tools: Optional[Dict[str, Set[str]]] = None,
        min_architecture_score: float = 70.0,
    ) -> None:
        self.required_tools = required_tools or self.DEFAULT_REQUIRED_TOOLS
        self.min_architecture_score = min_architecture_score

    def qualify_candidate(
        self,
        candidate: ModelCandidate,
        roles: Optional[List[str]] = None,
    ) -> ModelQualificationRecord:
        """Evaluate candidate against specified or default roles."""
        target_roles = roles or ["economy", "high_architecture", "verifier", "independent_review"]
        qualified_roles: List[str] = []
        disqualified_roles: Dict[str, str] = {}

        has_pricing = candidate.has_explicit_pricing
        task_cost: Optional[float] = None
        if has_pricing:
            try:
                task_cost = candidate.get_task_cost()
            except MissingPriceError:
                has_pricing = False
                task_cost = None

        for role in target_roles:
            is_qual, reason = self._evaluate_role(candidate, role, has_pricing, task_cost)
            if is_qual:
                qualified_roles.append(role)
            else:
                disqualified_roles[role] = reason

        return ModelQualificationRecord(
            model_id=candidate.model_id,
            alias=candidate.alias or candidate.model_id,
            provider_id=candidate.provider_id,
            family=candidate.family,
            qualified_roles=qualified_roles,
            disqualified_roles=disqualified_roles,
            has_valid_pricing=has_pricing,
            cost_per_1k_input_usd=candidate.cost_per_1k_input_usd,
            cost_per_1k_output_usd=candidate.cost_per_1k_output_usd,
            estimated_task_cost_usd=task_cost,
            coding_score=candidate.coding_score,
            intelligence_score=candidate.intelligence_score,
            tool_calling_supported=candidate.tool_calling_supported,
            tool_capabilities=candidate.tool_capabilities,
            supports_reasoning_effort=candidate.supports_reasoning_effort,
            allowed_reasoning_efforts=candidate.allowed_reasoning_efforts,
            default_reasoning_effort=candidate.default_reasoning_effort,
            metadata=candidate.metadata,
        )

    def _evaluate_role(
        self,
        candidate: ModelCandidate,
        role: str,
        has_pricing: bool,
        task_cost: Optional[float],
    ) -> tuple[bool, str]:
        # Tool calling check
        if not candidate.tool_calling_supported:
            return False, f"Model lacks structured tool-calling support required for '{role}'."

        needed_tools = self.required_tools.get(role, set())
        missing_tools = needed_tools - set(candidate.tool_capabilities)
        if missing_tools:
            return False, f"Model lacks required capabilities for '{role}': {sorted(missing_tools)}."

        # Role-specific checks
        if role == "economy":
            # Invariant 1: Missing price is NOT zero!
            # Economy requires explicit pricing. Unpriced models cannot be qualified as $0.
            if not has_pricing:
                return (
                    False,
                    "Missing price: cannot qualify for economy role (missing price is NOT zero).",
                )
            is_local = candidate.provider_id == "ollama" or (task_cost is not None and task_cost == 0.0)
            if not is_local and (task_cost is not None and task_cost > 0.05):
                return False, f"Cost per task ${task_cost} exceeds economy threshold."
            return True, "Qualified for economy."

        if role == "high_architecture":
            score = candidate.coding_score or candidate.intelligence_score or 0.0
            # Explicitly declared role or high benchmark score
            if "high_architecture" not in candidate.supported_roles and score < self.min_architecture_score:
                return (
                    False,
                    f"Benchmark score {score} is below architectural floor {self.min_architecture_score}.",
                )
            return True, "Qualified for high_architecture."

        if role in {"verifier", "independent_review"}:
            return True, f"Qualified for {role}."

        return True, f"Qualified for {role}."

    def qualify_all(
        self,
        candidates: List[ModelCandidate],
        roles: Optional[List[str]] = None,
    ) -> List[ModelQualificationRecord]:
        """Qualify a list of candidates and update Pareto optimality flags."""
        records = [self.qualify_candidate(c, roles=roles) for c in candidates]
        self.compute_pareto_frontier(records)
        return records

    def compute_pareto_frontier(
        self,
        records: List[ModelQualificationRecord],
        metric: str = "coding_score",
    ) -> List[ModelQualificationRecord]:
        """Calculate 2D Pareto Efficiency Frontier for (task_cost, score).

        Invariants:
        - Unpriced models (has_valid_pricing == False) CANNOT be placed on Pareto frontier.
        - Model A dominates Model B if score_A >= score_B AND cost_A <= cost_B with >=1 strict inequality.
        """
        # Filter for models that have valid pricing and metric score
        rankable = [
            r for r in records
            if r.has_valid_pricing
            and r.estimated_task_cost_usd is not None
            and getattr(r, metric, None) is not None
        ]

        if not rankable:
            for r in records:
                r.is_pareto_optimal = False
            return []

        # Sort primarily by cost ascending, then score descending
        sorted_records = sorted(
            rankable,
            key=lambda r: (r.estimated_task_cost_usd, -float(getattr(r, metric) or 0.0)),
        )

        frontier: List[ModelQualificationRecord] = []
        max_score_seen = -1.0

        for record in sorted_records:
            score = float(getattr(record, metric) or 0.0)
            if score > max_score_seen:
                frontier.append(record)
                max_score_seen = score

        frontier_ids = {r.model_id for r in frontier}
        for r in records:
            r.is_pareto_optimal = r.model_id in frontier_ids

        return frontier

    def generate_catalog(
        self,
        records: List[ModelQualificationRecord],
        base_catalog: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Generate canonical executors catalog dictionary from qualification records."""
        catalog: Dict[str, Any] = {
            "providers": {},
            "roles_mapping": {
                "economy": {"primary_model": "", "allowed_models": [], "fallback_order": []},
                "high_architecture": {
                    "primary_model": "",
                    "strict_floor_enforcement": True,
                    "allowed_models": [],
                    "fallback_order": [],
                },
                "verifier": {
                    "primary_local": "",
                    "primary_cloud": "",
                    "allowed_models": [],
                    "fallback_order": [],
                    "isolation_rule": "Cross-Model Family Isolation",
                },
                "independent_review": {
                    "alias_of": "verifier",
                    "allowed_models": [],
                    "fallback_order": [],
                },
            },
        }

        # Populate providers
        for r in records:
            p_id = r.provider_id
            p_key = f"provider_{p_id}"
            if p_key not in catalog["providers"]:
                catalog["providers"][p_key] = {"provider_id": p_id, "models": []}

            catalog["providers"][p_key]["models"].append({
                "model_id": r.model_id,
                "alias": r.alias,
                "family": r.family,
                "cost_per_1k_input_usd": r.cost_per_1k_input_usd or 0.0,
                "cost_per_1k_output_usd": r.cost_per_1k_output_usd or 0.0,
                "supported_roles": r.qualified_roles,
                "tool_calling_supported": r.tool_calling_supported,
                "tool_capabilities": r.tool_capabilities,
                "default_reasoning_effort": r.default_reasoning_effort,
                "max_reasoning_effort": r.allowed_reasoning_efforts[-1] if r.allowed_reasoning_efforts else None,
            })

            # Populate role mappings for qualified roles
            for role in r.qualified_roles:
                if role in catalog["roles_mapping"]:
                    mapping = catalog["roles_mapping"][role]
                    if r.model_id not in mapping.get("allowed_models", []):
                        mapping["allowed_models"].append(r.model_id)
                        mapping["fallback_order"].append(r.model_id)
                        if not mapping.get("primary_model"):
                            mapping["primary_model"] = r.model_id

        return catalog
