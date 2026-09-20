"""Qualified routes and fallback engine (HF-07-02).

Governed by Universal Engineering Standards (AGENTS.md) and Continuous Autonomy Plan.
Implements canonical route selection:
    select_route(job, catalog, quota_report, budget_manager) -> RouteDecision

Enforces:
1. Strict Local-First preference for economy ($0 via Ollama).
2. Anti-degradation: high_architecture strictly blocks in WAITING_RESOURCE
   when quotas are exhausted and NEVER degrades to economy or local $0.
3. Cross-Model Family Isolation for verifier and independent_review roles.
4. Reselection without double budget reservation when quotas shift before claim.
5. Real tool-calling capability verification.
6. Reasoning effort sanitization per 2026 specs.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("darkfac.workflow.qualified_routes")

# Default path to canonical executors catalog
DEFAULT_CATALOG_PATH = Path(".factory/planning/continuous-autonomy/bindings/executors.json")

# Reasoning effort constraints
ALLOWED_REASONING_EFFORTS: Set[str] = {"minimal", "low", "medium", "high", "max"}
FORBIDDEN_REASONING_EFFORTS: Set[str] = {"xhigh", "extra-high", "extra_high", "ultra"}

# Model family normalization dictionary
FAMILY_SYNONYMS: Dict[str, str] = {
    # Anthropic / Claude
    "claude": "claude",
    "anthropic": "claude",
    "anthropic/claude-opus-5": "claude",
    "claude-opus-5": "claude",
    "opus-5.1": "claude",
    # Qwen
    "qwen": "qwen",
    "alibaba": "qwen",
    "qwen-fast": "qwen",
    "qwen-deep": "qwen",
    "qwen-code-fast:latest": "qwen",
    "qwen-code-deep:latest": "qwen",
    # GPT / OpenAI / GPT-OSS
    "gpt-oss": "gpt-oss",
    "gpt-review": "gpt-oss",
    "gpt-review:latest": "gpt-oss",
    "gpt-oss-clean:latest": "gpt-oss",
    "openai": "gpt-oss",
    "gpt": "gpt-oss",
    "luna-xhigh": "gpt-oss",
    "gpt-6-astra": "gpt-oss",
    # Google / Gemini
    "gemini": "gemini",
    "google": "gemini",
    "gemini-flash": "gemini",
    "gemini-pro": "gemini",
    "gemini-3.8-flash": "gemini",
    "google/gemini-3.8-flash": "gemini",
    "google/gemini-3.1-pro": "gemini",
    # DeepSeek
    "deepseek": "deepseek",
    "deepseek-v4-pro": "deepseek",
    "deepseek-v4.1-flash": "deepseek",
    "deepseek/deepseek-v4-pro": "deepseek",
    "deepseek/deepseek-v4.1-flash": "deepseek",
    # xAI / Grok
    "grok": "grok",
    "xai": "grok",
    "grok-4.6": "grok",
    "xai/grok-4.6": "grok",
}


def normalize_family(identifier: Optional[str]) -> str:
    """Normalize a model name, alias, or provider to a canonical family key."""
    if not identifier:
        return "unknown"
    cleaned = identifier.strip().lower()
    if cleaned in FAMILY_SYNONYMS:
        return FAMILY_SYNONYMS[cleaned]
    # Check partial matches
    for key, canonical in FAMILY_SYNONYMS.items():
        if key in cleaned:
            return canonical
    return cleaned


def sanitize_reasoning_effort(effort: Optional[str], complexity: str = "medium") -> str:
    """Enforce HF-07 / Section 4 reasoning effort constraints.

    Allowed: 'high' by default for planning; 'max' when complexity justifies and supported.
    Never 'xhigh', 'extra-high', or 'ultra' (sanitized to 'max' without sending invalid parameters).
    """
    if not effort:
        return "max" if complexity in {"critical", "high"} else "high"
    normalized = effort.strip().lower()
    if normalized in FORBIDDEN_REASONING_EFFORTS:
        return "max"
    if normalized in ALLOWED_REASONING_EFFORTS:
        return normalized
    return "high"


# ==============================================================================
# Domain Contracts
# ==============================================================================


class ModelCapabilitySpec(BaseModel):
    """Normalized technical capability and cost profile of an executor model."""

    model_config = ConfigDict(extra="ignore")

    model_id: str
    alias: str
    provider_id: str
    family: str
    context_window: int = 4096
    cost_per_1k_input_usd: float = 0.0
    cost_per_1k_output_usd: float = 0.0
    cost_per_1k_cache_read_usd: float = 0.0
    supported_roles: List[str] = Field(default_factory=list)
    tool_calling_supported: bool = True
    tool_capabilities: List[str] = Field(default_factory=list)
    default_reasoning_effort: Optional[str] = None
    max_reasoning_effort: Optional[str] = None


class RouteDecision(BaseModel):
    """Canonical routing decision contract (HF-07-02)."""

    model_config = ConfigDict(extra="ignore")

    selected_model: Optional[str] = None
    selected_provider: Optional[str] = None
    status: str = "DISQUALIFIED"  # "QUALIFIED", "WAITING_RESOURCE", "DISQUALIFIED"
    reason: str = ""
    estimated_cost_usd: float = 0.0
    reservation_required: bool = False
    wakeup_at: Optional[str] = None

    # Extended telemetry & handoff attributes
    route_ref: Optional[str] = None
    model_family: Optional[str] = None
    reasoning_effort: Optional[str] = None
    reservation_id: Optional[str] = None
    fallback_model: Optional[str] = None


# Embedded catalog fallback matching executors.json
_EMBEDDED_DEFAULT_CATALOG: Dict[str, Any] = {
    "providers": {
        "local_ollama": {
            "provider_id": "ollama",
            "models": [
                {
                    "model_id": "qwen-code-fast:latest",
                    "alias": "qwen-fast",
                    "family": "qwen",
                    "context_window": 4096,
                    "cost_per_1k_input_usd": 0.0,
                    "cost_per_1k_output_usd": 0.0,
                    "supported_roles": ["economy"],
                    "tool_calling_supported": True,
                    "tool_capabilities": ["read_file", "write_file", "run_command", "git_ops"],
                },
                {
                    "model_id": "qwen-code-deep:latest",
                    "alias": "qwen-deep",
                    "family": "qwen",
                    "context_window": 16384,
                    "cost_per_1k_input_usd": 0.0,
                    "cost_per_1k_output_usd": 0.0,
                    "supported_roles": ["economy"],
                    "tool_calling_supported": True,
                    "tool_capabilities": ["read_file", "write_file", "run_command", "ast_analysis"],
                },
                {
                    "model_id": "gpt-review:latest",
                    "alias": "gpt-review",
                    "family": "gpt-oss",
                    "context_window": 16384,
                    "cost_per_1k_input_usd": 0.0,
                    "cost_per_1k_output_usd": 0.0,
                    "supported_roles": ["verifier", "independent_review"],
                    "tool_calling_supported": True,
                    "tool_capabilities": ["read_file", "git_diff", "run_command", "linter"],
                },
            ],
        },
        "cloud_antigravity_google": {
            "provider_id": "google",
            "models": [
                {
                    "model_id": "google/gemini-3.8-flash",
                    "alias": "gemini-flash",
                    "family": "gemini",
                    "context_window": 1048576,
                    "cost_per_1k_input_usd": 0.00015,
                    "cost_per_1k_output_usd": 0.0006,
                    "supported_roles": ["economy", "orchestrator"],
                    "tool_calling_supported": True,
                    "tool_capabilities": ["read_file", "write_file", "replace_file_content", "run_command"],
                },
                {
                    "model_id": "google/gemini-3.1-pro",
                    "alias": "gemini-pro",
                    "family": "gemini",
                    "context_window": 1048576,
                    "cost_per_1k_input_usd": 0.00125,
                    "cost_per_1k_output_usd": 0.005,
                    "supported_roles": ["high_architecture"],
                    "tool_calling_supported": True,
                    "tool_capabilities": ["read_file", "write_file", "run_command"],
                },
            ],
        },
        "cloud_anthropic": {
            "provider_id": "anthropic",
            "models": [
                {
                    "model_id": "anthropic/claude-opus-5",
                    "alias": "claude-opus-5",
                    "family": "claude",
                    "context_window": 200000,
                    "cost_per_1k_input_usd": 0.005,
                    "cost_per_1k_output_usd": 0.025,
                    "supported_roles": ["high_architecture"],
                    "tool_calling_supported": True,
                    "tool_capabilities": ["read_file", "write_file", "replace_file_content", "run_command"],
                },
            ],
        },
        "cloud_openrouter": {
            "provider_id": "openrouter",
            "models": [
                {
                    "model_id": "deepseek/deepseek-v4-pro",
                    "alias": "deepseek-v4-pro",
                    "family": "deepseek",
                    "context_window": 256000,
                    "cost_per_1k_input_usd": 0.00045,
                    "cost_per_1k_output_usd": 0.0018,
                    "supported_roles": ["high_architecture", "coding"],
                    "tool_calling_supported": True,
                    "tool_capabilities": ["read_file", "write_file", "run_command"],
                },
                {
                    "model_id": "deepseek/deepseek-v4.1-flash",
                    "alias": "deepseek-v4.1-flash",
                    "family": "deepseek",
                    "context_window": 1048576,
                    "cost_per_1k_input_usd": 0.00015,
                    "cost_per_1k_output_usd": 0.0006,
                    "supported_roles": ["verifier", "independent_review"],
                    "tool_calling_supported": True,
                    "tool_capabilities": ["read_file", "run_command"],
                },
            ],
        },
        "cloud_xai": {
            "provider_id": "xai",
            "models": [
                {
                    "model_id": "xai/grok-4.6",
                    "alias": "grok-4.6",
                    "family": "grok",
                    "context_window": 131072,
                    "cost_per_1k_input_usd": 0.002,
                    "cost_per_1k_output_usd": 0.01,
                    "supported_roles": ["researcher", "verifier"],
                    "tool_calling_supported": True,
                    "tool_capabilities": ["web_search", "read_url", "run_command"],
                },
            ],
        },
    },
    "roles_mapping": {
        "economy": {
            "primary_model": "qwen-code-fast:latest",
            "allowed_models": [
                "qwen-code-fast:latest",
                "qwen-code-deep:latest",
                "google/gemini-3.8-flash",
            ],
            "fallback_order": [
                "qwen-code-fast:latest",
                "qwen-code-deep:latest",
                "google/gemini-3.8-flash",
            ],
        },
        "high_architecture": {
            "primary_model": "anthropic/claude-opus-5",
            "strict_floor_enforcement": True,
            "allowed_models": [
                "anthropic/claude-opus-5",
                "deepseek/deepseek-v4-pro",
                "google/gemini-3.1-pro",
            ],
            "fallback_order": [
                "anthropic/claude-opus-5",
                "deepseek/deepseek-v4-pro",
                "google/gemini-3.1-pro",
            ],
        },
        "verifier": {
            "primary_local": "gpt-review:latest",
            "primary_cloud": "deepseek/deepseek-v4.1-flash",
            "allowed_models": [
                "gpt-review:latest",
                "deepseek/deepseek-v4.1-flash",
                "xai/grok-4.6",
            ],
            "fallback_order": [
                "gpt-review:latest",
                "deepseek/deepseek-v4.1-flash",
                "xai/grok-4.6",
            ],
            "isolation_rule": "Cross-Model Family Isolation",
        },
        "independent_review": {
            "alias_of": "verifier",
            "allowed_models": [
                "gpt-review:latest",
                "deepseek/deepseek-v4.1-flash",
                "xai/grok-4.6",
            ],
            "fallback_order": [
                "gpt-review:latest",
                "deepseek/deepseek-v4.1-flash",
                "xai/grok-4.6",
            ],
        },
    },
}


class CatalogRegistry:
    """Indexes executors, models and role fallback hierarchies from catalog JSON/dict."""

    def __init__(self, raw_catalog: Optional[Dict[str, Any]] = None) -> None:
        self.raw = raw_catalog or self._load_default_catalog()
        self.models_by_id: Dict[str, ModelCapabilitySpec] = {}
        self.models_by_alias: Dict[str, ModelCapabilitySpec] = {}
        self.roles_mapping: Dict[str, Dict[str, Any]] = self.raw.get("roles_mapping", {})
        self._index()

    @staticmethod
    def _load_default_catalog() -> Dict[str, Any]:
        if DEFAULT_CATALOG_PATH.is_file():
            try:
                return json.loads(DEFAULT_CATALOG_PATH.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("Failed loading %s: %s; using embedded catalog", DEFAULT_CATALOG_PATH, exc)
        return _EMBEDDED_DEFAULT_CATALOG

    def _index(self) -> None:
        providers = self.raw.get("providers", {})
        for p_key, p_info in providers.items():
            provider_id = p_info.get("provider_id", p_key)
            for m in p_info.get("models", []):
                spec = ModelCapabilitySpec(
                    model_id=m["model_id"],
                    alias=m.get("alias", m["model_id"]),
                    provider_id=provider_id,
                    family=m.get("family", normalize_family(m["model_id"])),
                    context_window=m.get("context_window", 4096),
                    cost_per_1k_input_usd=m.get("cost_per_1k_input_usd", 0.0),
                    cost_per_1k_output_usd=m.get("cost_per_1k_output_usd", 0.0),
                    cost_per_1k_cache_read_usd=m.get("cost_per_1k_cache_read_usd", 0.0),
                    supported_roles=m.get("supported_roles", []),
                    tool_calling_supported=m.get("tool_calling_supported", True),
                    tool_capabilities=m.get("tool_capabilities", []),
                    default_reasoning_effort=m.get("default_reasoning_effort"),
                    max_reasoning_effort=m.get("max_reasoning_effort"),
                )
                self.models_by_id[spec.model_id.lower()] = spec
                self.models_by_alias[spec.alias.lower()] = spec

    def get_model(self, identifier: str) -> Optional[ModelCapabilitySpec]:
        """Lookup model spec by exact ID or alias."""
        clean = identifier.lower().strip()
        return self.models_by_id.get(clean) or self.models_by_alias.get(clean)

    def get_fallback_order(self, role: str) -> List[str]:
        """Return declared fallback model identifiers for a role."""
        clean_role = role.lower().strip()
        role_spec = self.roles_mapping.get(clean_role)
        if not role_spec:
            # Check alias_of
            for r_name, r_data in self.roles_mapping.items():
                if r_data.get("alias_of") == clean_role:
                    role_spec = r_data
                    break
        if not role_spec:
            return []
        return role_spec.get("fallback_order", role_spec.get("allowed_models", []))

    def is_strict_floor_role(self, role: str) -> bool:
        """Check if role enforces architectural floor (anti-degradation)."""
        clean_role = role.lower().strip()
        role_spec = self.roles_mapping.get(clean_role, {})
        return bool(role_spec.get("strict_floor_enforcement", clean_role == "high_architecture"))


# ==============================================================================
# Quota Assessment Helper
# ==============================================================================


class QuotaState:
    """Extracts provider availability and quota reset timestamps from quota reports."""

    def __init__(self, quota_report: Optional[Any] = None) -> None:
        self.report = quota_report
        self.exhausted_providers: Set[str] = set()
        self.model_whitelist_by_provider: Dict[str, Set[str]] = {}
        self.resets_by_provider: Dict[str, str] = {}
        self._parse()

    def _parse(self) -> None:
        if self.report is None:
            return

        # Case 1: AccountUsageReport object or duck-typed with accounts
        accounts = getattr(self.report, "accounts", None)
        if accounts is not None:
            for acc in accounts:
                provider_id = getattr(acc, "provider_id", "").lower()
                status = getattr(acc, "status", None)
                status_val = status.value if hasattr(status, "value") else str(status).lower()

                # Treat limited, degraded, disconnected, or unknown as exhausted/unavailable
                if status_val in {"limited", "degraded", "disconnected", "unknown"}:
                    self.exhausted_providers.add(provider_id)

                # Check windows
                windows = getattr(acc, "windows", []) or []
                for win in windows:
                    used = getattr(win, "used_percent", None)
                    rem = getattr(win, "remaining_percent", None)
                    resets_at = getattr(win, "resets_at", None)

                    if (used is not None and used >= 100.0) or (rem is not None and rem <= 0.0):
                        self.exhausted_providers.add(provider_id)
                    if resets_at:
                        self.resets_by_provider[provider_id] = str(resets_at)
            return

        # Case 2: Dictionary format
        if isinstance(self.report, dict):
            # Check if it has serialized accounts
            if "accounts" in self.report and isinstance(self.report["accounts"], list):
                for acc in self.report["accounts"]:
                    provider_id = acc.get("provider_id", "").lower()
                    status_val = str(acc.get("status", "")).lower()
                    if status_val in {"limited", "degraded", "disconnected", "unknown"}:
                        self.exhausted_providers.add(provider_id)
                    for win in acc.get("windows", []):
                        if win.get("used_percent", 0.0) >= 100.0 or win.get("remaining_percent", 100.0) <= 0.0:
                            self.exhausted_providers.add(provider_id)
                        if win.get("resets_at"):
                            self.resets_by_provider[provider_id] = win["resets_at"]
                return

            # Direct provider mapping dict: {"anthropic": False, "ollama": {"models_available": [...]}}
            for key, val in self.report.items():
                p_clean = key.lower()
                if isinstance(val, bool):
                    if not val:
                        self.exhausted_providers.add(p_clean)
                elif isinstance(val, str):
                    if val.lower() in {"limited", "degraded", "disconnected", "unknown", "unavailable"}:
                        self.exhausted_providers.add(p_clean)
                elif isinstance(val, dict):
                    status_val = str(val.get("status", "")).lower()
                    if (
                        val.get("available") is False
                        or val.get("available") == "unknown"
                        or status_val in {"limited", "degraded", "disconnected", "unknown"}
                    ):
                        self.exhausted_providers.add(p_clean)
                    if "models_available" in val and isinstance(val["models_available"], list):
                        self.model_whitelist_by_provider[p_clean] = {
                            m.lower().strip() for m in val["models_available"]
                        }
                    if "resets_at" in val:
                        self.resets_by_provider[p_clean] = str(val["resets_at"])

    def is_provider_available(self, provider_id: str) -> bool:
        """Check if provider is not quota exhausted or in unknown status."""
        return provider_id.lower() not in self.exhausted_providers

    def is_model_available(self, provider_id: str, model_id: str, alias: str) -> bool:
        """Check if specific model is available for provider (unknown is not available)."""
        p_clean = provider_id.lower()
        if p_clean in self.exhausted_providers:
            return False
        if p_clean in self.model_whitelist_by_provider:
            allowed = self.model_whitelist_by_provider[p_clean]
            if model_id.lower() not in allowed and alias.lower() not in allowed:
                return False
        if isinstance(self.report, dict) and p_clean in self.report and isinstance(self.report[p_clean], dict):
            models_status = self.report[p_clean].get("models_status", {})
            m_stat = str(models_status.get(model_id, models_status.get(alias, ""))).lower()
            if m_stat in {"unavailable", "limited", "unknown"}:
                return False
        return True

    def get_earliest_reset(self, candidate_providers: Set[str]) -> Optional[str]:
        """Compute earliest wakeup timestamp among affected candidate providers."""
        resets = [
            self.resets_by_provider[p]
            for p in candidate_providers
            if p in self.resets_by_provider
        ]
        if not resets:
            return None
        # Sort ISO timestamps chronologically
        resets.sort()
        return resets[0]


# ==============================================================================
# Canonical Route Selector (HF-07-02)
# ==============================================================================


def select_route(
    job: Any,
    catalog: Optional[Any] = None,
    quota_report: Optional[Any] = None,
    budget_manager: Optional[Any] = None,
) -> RouteDecision:
    """Select optimal qualified executor route conforming to 2026 contracts.

    Args:
        job: Dict, JobSpec or StageContext describing role, project_id, task, etc.
        catalog: Optional custom catalog dict, CatalogRegistry, or None (loads default).
        quota_report: Optional AccountUsageReport or provider status mapping.
        budget_manager: Optional PortfolioBudgetManager or ExecutionBudgetManager.

    Returns:
        RouteDecision with status ("QUALIFIED", "WAITING_RESOURCE", "DISQUALIFIED").
    """
    # 1. Normalize job inputs
    job_dict = job if isinstance(job, dict) else (job.model_dump() if hasattr(job, "model_dump") else getattr(job, "__dict__", {}))
    raw_role = job_dict.get("role") or job_dict.get("task_type")
    if not raw_role and job_dict.get("stage"):
        stg = str(job_dict["stage"]).lower().strip()
        if stg in {"planning", "plan", "architecture"}:
            raw_role = "high_architecture"
        elif stg in {"independent_review", "review", "audit", "verifier"}:
            raw_role = "verifier"
        elif stg in {"research"}:
            raw_role = "researcher"
        else:
            raw_role = "economy"
    role = raw_role or "economy"

    # Map task_type synonyms to roles if needed
    clean_role = str(role).lower().strip().replace("-", "_")
    if clean_role in {"architecture", "plan", "prd", "high_architecture"}:
        target_role = "high_architecture"
    elif clean_role in {"review", "audit", "adversarial", "verifier", "independent_review"}:
        target_role = "verifier"
    elif clean_role in {"economy", "testing", "test", "validate", "microtask", "low_complexity", "coding_low"}:
        target_role = "economy"
    else:
        target_role = clean_role

    project_id = job_dict.get("project_id", "darkfac")
    implementer_model = job_dict.get("implementer_model") or job_dict.get("implementer_family")
    implementer_family = normalize_family(implementer_model) if implementer_model else None
    existing_reservation_id = job_dict.get("existing_reservation_id") or job_dict.get("reservation_id")
    tool_calling_required = job_dict.get("tool_calling_required", True)
    required_tools = job_dict.get("required_tools") or job_dict.get("tool_capabilities") or []
    complexity = job_dict.get("complexity", "medium")
    reasoning_effort = sanitize_reasoning_effort(job_dict.get("reasoning_effort"), complexity=complexity)

    # 2. Prepare catalog registry
    if isinstance(catalog, CatalogRegistry):
        registry = catalog
    elif isinstance(catalog, dict):
        registry = CatalogRegistry(raw_catalog=catalog)
    else:
        registry = CatalogRegistry()

    # 3. Prepare quota state
    quota = QuotaState(quota_report)

    # 4. Check budget manager for cloud cutoff (LOCAL_ONLY)
    paid_cloud_allowed = True
    if budget_manager is not None:
        if hasattr(budget_manager, "is_paid_cloud_allowed"):
            try:
                paid_cloud_allowed = budget_manager.is_paid_cloud_allowed(project_id)
            except Exception as exc:
                logger.debug("Error checking is_paid_cloud_allowed: %s", exc)

    # 5. Resolve candidate fallback models for role
    fallback_candidates = registry.get_fallback_order(target_role)
    if not fallback_candidates:
        return RouteDecision(
            selected_model=None,
            selected_provider=None,
            status="DISQUALIFIED",
            reason=f"Role '{role}' is not declared or has no configured candidates in catalog.",
            estimated_cost_usd=0.0,
            reservation_required=False,
            wakeup_at=None,
        )

    # 6. Evaluate candidates in strict fallback order
    rejection_reasons: List[str] = []
    quota_blocked_providers: Set[str] = set()
    disqualified_by_tools = False

    for cand_id in fallback_candidates:
        model_spec = registry.get_model(cand_id)
        if not model_spec:
            rejection_reasons.append(f"Model '{cand_id}' not found in catalog.")
            continue

        cand_provider = model_spec.provider_id
        is_local = (cand_provider == "ollama" or model_spec.cost_per_1k_input_usd == 0.0)

        # A. Context Window and Quality Check
        req_context = (
            job_dict.get("context_window_required")
            or job_dict.get("context_required")
            or (job_dict.get("estimated_input_tokens") if job_dict.get("estimated_input_tokens", 0) > 4000 else None)
        )
        if req_context is not None and req_context > model_spec.context_window:
            rejection_reasons.append(
                f"Model '{cand_id}' context window ({model_spec.context_window}) is insufficient for required tokens ({req_context})."
            )
            continue

        # B. Host / Environment Isolation Check
        target_env = str(
            job_dict.get("host")
            or job_dict.get("environment_ref")
            or job_dict.get("environment")
            or ""
        ).lower().strip()
        if target_env in {"cloud", "vps", "headless_vps", "cloud-binding", "docker_cloud"}:
            if is_local and not job_dict.get("allow_local_on_cloud", False):
                rejection_reasons.append(
                    f"Host/environment '{target_env}' cannot access desktop local Ollama model '{cand_id}'."
                )
                continue

        # C. Tool-Calling Capability Check
        if tool_calling_required and not model_spec.tool_calling_supported:
            rejection_reasons.append(f"Model '{cand_id}' does not support structured tool calling.")
            disqualified_by_tools = True
            continue

        if required_tools:
            missing_tools = [t for t in required_tools if t not in model_spec.tool_capabilities]
            if missing_tools:
                rejection_reasons.append(f"Model '{cand_id}' lacks required capabilities: {missing_tools}.")
                disqualified_by_tools = True
                continue

        # D. Cross-Model Family Isolation (verifier / independent_review)
        if target_role in {"verifier", "independent_review"} and implementer_family:
            cand_family = normalize_family(model_spec.family)
            if cand_family == implementer_family:
                rejection_reasons.append(
                    f"Cross-Model Family Isolation: Revisor family '{cand_family}' matches implementer family '{implementer_family}'."
                )
                continue

        # E. Budget Ceiling / LOCAL_ONLY Enforcement
        if not is_local and not paid_cloud_allowed:
            rejection_reasons.append(
                f"Project '{project_id}' budget reached 100% ceiling (LOCAL_ONLY); cloud model '{cand_id}' blocked."
            )
            continue

        # F. Quota and Availability Check
        if not quota.is_model_available(cand_provider, model_spec.model_id, model_spec.alias):
            rejection_reasons.append(f"Provider '{cand_provider}' or model '{cand_id}' exhausted by quota limit.")
            quota_blocked_providers.add(cand_provider)
            continue

        # Candidate passed all gates! Calculate costs and reservation
        est_input_tokens = job_dict.get("estimated_input_tokens", 4000)
        est_output_tokens = job_dict.get("estimated_output_tokens", 1000)
        if is_local:
            estimated_cost = 0.0
            reservation_required = False
        else:
            estimated_cost = round(
                (est_input_tokens / 1000.0) * model_spec.cost_per_1k_input_usd
                + (est_output_tokens / 1000.0) * model_spec.cost_per_1k_output_usd,
                6,
            )
            reservation_required = True

        # Handle existing reservation (avoid double budget reservation)
        active_reservation_id = existing_reservation_id
        if existing_reservation_id and budget_manager is not None:
            if is_local and hasattr(budget_manager, "release_if_active"):
                try:
                    budget_manager.release_if_active(
                        existing_reservation_id,
                        reason="Route re-selected to local $0 model",
                    )
                    active_reservation_id = None
                except Exception as exc:
                    logger.debug("Error releasing previous reservation on local fallback: %s", exc)

        # Fallback model reference (next available candidate in order)
        fallback_model = None
        cand_idx = fallback_candidates.index(cand_id)
        if cand_idx + 1 < len(fallback_candidates):
            fallback_model = fallback_candidates[cand_idx + 1]

        return RouteDecision(
            selected_model=model_spec.model_id,
            selected_provider=cand_provider,
            status="QUALIFIED",
            reason=f"Qualified route selected for role '{target_role}' via provider '{cand_provider}'.",
            estimated_cost_usd=estimated_cost,
            reservation_required=reservation_required,
            wakeup_at=None,
            route_ref=f"{cand_provider}:{model_spec.model_id}",
            model_family=model_spec.family,
            reasoning_effort=reasoning_effort,
            reservation_id=active_reservation_id,
            fallback_model=fallback_model,
        )

    # 7. No candidates qualified. Determine WAITING_RESOURCE vs DISQUALIFIED
    is_strict_floor = registry.is_strict_floor_role(target_role)

    # Anti-Degradation: High Architecture strictly blocks in WAITING_RESOURCE
    if is_strict_floor:
        earliest_wakeup = quota.get_earliest_reset(quota_blocked_providers)
        if not earliest_wakeup:
            # Fallback default reset window: +45 minutes
            earliest_wakeup = (datetime.now(UTC) + timedelta(minutes=45)).isoformat()

        return RouteDecision(
            selected_model=None,
            selected_provider=None,
            status="WAITING_RESOURCE",
            reason=(
                "Inviolabilidade do piso arquitetural: todos os modelos de alta arquitetura "
                f"estão indisponíveis por cota/recurso ({'; '.join(rejection_reasons)}). "
                "Rebaixamento para economy ($0 local) é estritamente proibido."
            ),
            estimated_cost_usd=0.0,
            reservation_required=False,
            wakeup_at=earliest_wakeup,
            reasoning_effort=reasoning_effort,
        )

    # If blocked solely due to temporary quotas on all options
    if quota_blocked_providers and not disqualified_by_tools:
        earliest_wakeup = quota.get_earliest_reset(quota_blocked_providers) or (
            datetime.now(UTC) + timedelta(minutes=30)
        ).isoformat()
        return RouteDecision(
            selected_model=None,
            selected_provider=None,
            status="WAITING_RESOURCE",
            reason=f"All candidates temporarily blocked by provider quotas: {'; '.join(rejection_reasons)}",
            estimated_cost_usd=0.0,
            reservation_required=False,
            wakeup_at=earliest_wakeup,
            reasoning_effort=reasoning_effort,
        )

    # Permanent disqualification (e.g. tool capabilities missing, invalid spec)
    return RouteDecision(
        selected_model=None,
        selected_provider=None,
        status="DISQUALIFIED",
        reason=f"No route qualified for role '{target_role}': {'; '.join(rejection_reasons)}",
        estimated_cost_usd=0.0,
        reservation_required=False,
        wakeup_at=None,
        reasoning_effort=reasoning_effort,
    )
