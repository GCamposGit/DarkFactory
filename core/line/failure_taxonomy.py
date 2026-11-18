"""Shared failure vocabulary for launchers, dispatch and delivery retries."""
from __future__ import annotations

import re
from typing import Literal

FailureClass = Literal["infra", "candidate", "owner", "unknown"]
INFRA_CAUSES = frozenset({
    "gate_insufficient_ram", "gate_remote_unavailable", "gate_timeout", "gate_infra_no_tests",
    "base_red_suspected", "gate_dirty_worktree", "route_wait", "quota", "lock", "resource",
    "context", "host_permission", "network", "infra", "merge_blocked",
})
CANDIDATE_CAUSES = frozenset({
    "gate_failed", "fix_agent_no_changes", "agent_no_changes", "review_changes_required",
    "ci_red", "validate_exhausted", "agent_timeout", "rebase_conflict", "docs_sync_failed",
    "stage_artifact_missing", "build_evidence_mismatch",
})
OWNER_CAUSES = frozenset({"self_modification", "protected_path"})
CAUSES = INFRA_CAUSES | CANDIDATE_CAUSES | OWNER_CAUSES

# Legacy messages are interpreted here only; structured causes always take precedence.
_ALIASES = {
    "route_wait": ("no_route", "quota_critical", "development_no_route", "ci_fix_route_wait",
                   "ci_fix_requires_revalidation", "gate_capacity_zero"),
    "quota": ("quota", "rate limit", "rate_limit", "429"),
    "lock": ("[trava de linha]", "[reserva de frota]", "ja possui pr ativo",
             "canonical_ownership_conflict", "active_pull_request", "gate_busy"),
    "network": ("network", "connectionerror", "connection refused", "urlerror", "gh failed",
                "pull_request_lookup_unavailable", "could not resolve host", "tls handshake",
                "git fetch", "gherror", "github unavailable", "error connecting", "connectionreseterror",
                "http 502", "http 503", "falha de rede"),
    "host_permission": ("permission denied", "permissionerror", "access denied", "access is denied",
                        "acesso negado", "winerror 5", "winerror 10013"),
    "agent_timeout": ("agent_timeout", "plan_timeout", "timeouterror"),
    "gate_timeout": ("focal_timeout",),
    "gate_remote_unavailable": ("[remote_job_lost]", "--remote-required:", "remoterequirederror"),
    "infra": ("harness_infra_error", "review_timeout"),
    "resource": ("host_preflight_failed", "gate_probe_failed", "workspace_error", "shared_checkout_dirty"),
    "context": ("scanner", "scan failed", "input too long", "review_context_unavailable", "review_output_truncated"),
    "ci_red": ("ci_red_twice", "ci_recovery_budget_exhausted", "ci_fix_no_changes"),
    "review_changes_required": ("review_budget_exhausted", "review_exhausted_changes_required"),
    "gate_failed": ("focal_tests_failed",),
}


def canonical_cause(cause: str) -> str:
    """Normalize a cause or legacy diagnostic without retaining arbitrary error text."""
    text = str(cause).strip().casefold()
    token = text.split(":", 1)[0]
    if token in CAUSES:
        return token
    for canonical, aliases in _ALIASES.items():
        if token in aliases:
            return canonical
    for canonical, aliases in _ALIASES.items():
        if any(alias in text for alias in aliases):
            return canonical
    for known in sorted(CAUSES, key=len, reverse=True):
        if re.search(r"(?<![a-z0-9_])" + known + r"(?![a-z0-9_])", text):
            return known
    return "unknown"


def classify(cause: str) -> FailureClass:
    """Classify both canonical causes and historical launcher diagnostics."""
    normalized = canonical_cause(cause)
    if normalized in INFRA_CAUSES:
        return "infra"
    if normalized in CANDIDATE_CAUSES:
        return "candidate"
    if normalized in OWNER_CAUSES:
        return "owner"
    return "unknown"
