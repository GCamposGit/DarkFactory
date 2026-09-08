"""Módulo de Demandas do Usuário da Dark Factory.

Permite que o usuário/dono do projeto registre, oriente, refine e insira
demandas manuais no backlog de desenvolvimento com tags exclusivas
(user-demand) e resiliência total a custo zero ($0.00).
"""

from __future__ import annotations

from core.demands.models import (
    DemandInput,
    DemandOrigin,
    DemandSpecificationGuidance,
    UserTicket,
    TAG_USER_DEMAND,
    TAG_CODE_REVIEW,
    TAG_AGENT_FEATURE,
)
from core.demands.service import DemandsService, build_default_demands_service
from core.demands.specifier import DemandSpecifier, HeuristicDemandSpecifier
from core.demands.store import DemandsStore

__all__ = [
    "DemandInput",
    "DemandOrigin",
    "DemandSpecificationGuidance",
    "UserTicket",
    "TAG_USER_DEMAND",
    "TAG_CODE_REVIEW",
    "TAG_AGENT_FEATURE",
    "DemandsService",
    "build_default_demands_service",
    "DemandSpecifier",
    "HeuristicDemandSpecifier",
    "DemandsStore",
]
