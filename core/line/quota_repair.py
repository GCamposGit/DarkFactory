"""Idempotent DarkFac work items for providers whose requested quota refresh still failed."""

from __future__ import annotations

import re

from core.demands.models import UserTicket


def canonical_quota_cause(reason: str) -> str:
    """Collapse probe details into stable, credential-free repair categories."""
    value = (reason or "unknown").strip().lower()
    if "timeout" in value:
        return "probe-timeout"
    if "stale" in value or "old" in value:
        return "stale-sample"
    if "suspicious" in value or "invalid" in value:
        return "invalid-sample"
    if "unavailable" in value or "disconnected" in value:
        return "probe-unavailable"
    return "unknown-sample"


def ensure_quota_repair(provider_id: str, reason: str) -> UserTicket:
    """Upsert one stable repair ticket per provider and canonical cause in DarkFac."""
    provider = re.sub(r"[^a-z0-9-]+", "-", provider_id.strip().lower()).strip("-") or "unknown"
    cause = canonical_quota_cause(reason)
    ticket_id = f"INFRA-QUOTA-{provider}-{cause}"
    from core.demands import queue_store

    existing = queue_store.get_ticket(ticket_id)
    if existing is not None:
        return existing
    ticket = UserTicket(
        id=ticket_id,
        project_id="darkfac",
        priority=80,
        title=f"Restaurar telemetria de cota: {provider}",
        problem_statement=(
            f"A leitura do provedor {provider} permaneceu ausente, inválida ou obsoleta após uma nova "
            f"amostra solicitada. Diagnóstico canônico: {cause}."
        ),
        core_journey=["Uma amostra válida do provedor fica disponível para o roteador."],
        acceptance_criteria=[
            "Identificar e corrigir a causa persistente da telemetria indisponível.",
            "Confirmar uma amostra recente e válida sem expor credenciais.",
        ],
        tags=["infra", "quota-repair", f"quota-provider:{provider}", f"quota-cause:{cause}"],
    )
    return queue_store.register_ticket(ticket, source="quota_repair")


__all__ = ["canonical_quota_cause", "ensure_quota_repair"]
