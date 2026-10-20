"""Grill thresholds for model changes (USR-185, slice 8).

``requires_grill`` is a pure decision: it says whether a model change must pause in the Grill (``WAITING_HUMAN``,
the same human-decision gate used for ambiguous demands) or may proceed autonomously. The thresholds live in the
registry (``grill_thresholds``); nothing here reads files, the clock or the network. Slice 7 (promote/rollback) or the
orchestrator calls it before applying a change.

Grill is mandatory when: the model is forbidden-autonomous or frontier tier; its price exceeds the ceiling; a review
role changes family (or the reviewer would share the implementer's family); or the qualification is ambiguous
(missing, failed, approved without evidence, skipped steps, budget exceeded). Everything else is autonomous.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Optional

from core.models.registry import ModelEntry, ModelRegistry, _find, get_registry

ChangeKind = Literal["promote_candidate", "assign_role", "swap_reviewer"]
CHANGE_KINDS: tuple[str, ...] = ("promote_candidate", "assign_role", "swap_reviewer")


@dataclass(frozen=True)
class ModelChange:
    """A proposed model change. ``role`` is required for ``assign_role`` and ``swap_reviewer``.

    ``evidence`` is the persisted qualification evidence (``QualificationResult.to_json()``), when available.
    """

    kind: ChangeKind
    model_id: str
    role: Optional[str] = None
    evidence: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class GrillDecision:
    required: bool
    reasons: list[str] = field(default_factory=list)


def _is_forbidden(entry: ModelEntry, markers: list[str]) -> bool:
    low = entry.id.lower()
    return entry.forbidden_autonomous or any(m.strip().lower() in low for m in markers if m.strip())


def _qualification_reasons(entry: ModelEntry, evidence: Optional[Mapping[str, Any]], min_steps: int) -> list[str]:
    q = entry.qualification
    if q is None:
        return [f"qualificacao ausente para {entry.id!r}"]
    if q.status == "failed":
        return [f"qualificacao reprovada para {entry.id!r}"]
    if q.status == "grandfathered":
        return []
    reasons: list[str] = []
    if evidence is None:
        if not q.evidence_path:
            reasons.append(f"qualificacao aprovada sem evidencia para {entry.id!r}")
        return reasons
    steps = evidence.get("steps") or []
    if evidence.get("budget_exceeded"):
        reasons.append(f"qualificacao de {entry.id!r} estourou o orcamento")
    if evidence.get("status") not in (None, "approved"):
        reasons.append(f"evidencia de {entry.id!r} nao esta aprovada")
    if len(steps) < min_steps:
        reasons.append(f"qualificacao de {entry.id!r} com etapas puladas ({len(steps)}/{min_steps})")
    if any(not s.get("passed", False) for s in steps):
        reasons.append(f"qualificacao de {entry.id!r} aprovada com etapa reprovada")
    return reasons


def _review_reasons(reg: ModelRegistry, entry: ModelEntry, role: str) -> list[str]:
    by_id = {m.id: m for m in reg.models}
    reasons: list[str] = []
    current = by_id.get(reg.roles.get(role, ""))
    reviewer_roles = {p.reviewer for p in reg.review_pairs}
    if role in reviewer_roles and current is not None and current.family != entry.family:
        reasons.append(
            f"mudanca de familia no papel de revisao {role!r}: {current.family} -> {entry.family}"
        )
    for pair in reg.review_pairs:
        other_role = pair.implementer if role == pair.reviewer else pair.reviewer if role == pair.implementer else None
        if other_role is None:
            continue
        other = by_id.get(reg.roles.get(other_role, ""))
        if other is not None and other.family == entry.family:
            reasons.append(
                f"{role!r} ficaria na mesma familia ({entry.family}) de {other_role!r} no par de revisao"
            )
    return reasons


def requires_grill(change: ModelChange, registry: Optional[ModelRegistry] = None) -> GrillDecision:
    """Pure decision: must this model change go through the Grill (WAITING_HUMAN)?"""
    reg = registry if registry is not None else get_registry()
    th = reg.grill_thresholds
    if change.kind not in CHANGE_KINDS:
        return GrillDecision(True, [f"tipo de mudanca desconhecido {change.kind!r}"])
    entry = _find(reg, change.model_id)
    if entry is None:
        return GrillDecision(True, [f"modelo desconhecido {change.model_id!r}"])
    reasons: list[str] = []
    if change.kind in ("assign_role", "swap_reviewer") and not change.role:
        reasons.append(f"mudanca {change.kind!r} sem papel informado")

    if _is_forbidden(entry, reg.forbidden_autonomous_markers):
        reasons.append(f"modelo {entry.id!r} e proibido em modo autonomo")
    frontier = {t.strip().lower() for t in th.frontier_tiers}
    if entry.tier.strip().lower() in frontier:
        reasons.append(f"modelo {entry.id!r} e de faixa frontier ({entry.tier})")

    if entry.price_input_per_mtok is not None and entry.price_input_per_mtok > th.max_price_input_per_mtok:
        reasons.append(
            f"preco de entrada {entry.price_input_per_mtok:g} acima do teto {th.max_price_input_per_mtok:g} US$/Mtok"
        )
    if entry.price_output_per_mtok is not None and entry.price_output_per_mtok > th.max_price_output_per_mtok:
        reasons.append(
            f"preco de saida {entry.price_output_per_mtok:g} acima do teto {th.max_price_output_per_mtok:g} US$/Mtok"
        )
    unknown_price = entry.price_input_per_mtok is None or entry.price_output_per_mtok is None
    if unknown_price and th.require_grill_on_unknown_price and entry.status != "active":
        reasons.append(f"preco desconhecido para {entry.id!r} (nao ativo)")

    if change.kind == "promote_candidate" or entry.status != "active":
        reasons.extend(_qualification_reasons(entry, change.evidence, th.min_qualification_steps))
    if change.kind == "promote_candidate" and entry.status != "candidate":
        reasons.append(f"promocao de {entry.id!r} que nao e candidate (status={entry.status})")

    if change.role and change.kind in ("assign_role", "swap_reviewer"):
        reasons.extend(_review_reasons(reg, entry, change.role))

    unique = list(dict.fromkeys(reasons))
    return GrillDecision(bool(unique), unique)
