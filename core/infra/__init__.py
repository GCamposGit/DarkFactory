"""Dark Factory Infrastructure Management and Scalability Module."""

from __future__ import annotations

from core.infra.models import (
    ArchitectureDecisionRecord,
    HardwareSpec,
    InfraInventory,
    InfraNode,
    NetworkSpec,
    NodeRole,
    NodeStatus,
    ServiceItem,
)
from core.infra.inventory import DEFAULT_INVENTORY_PATH, InventoryManager, build_default_inventory
from core.infra.persistence import HybridPersistenceAdapter, PersistenceRecord, SyncResult

__all__ = [
    "ArchitectureDecisionRecord",
    "HardwareSpec",
    "InfraInventory",
    "InfraNode",
    "NetworkSpec",
    "NodeRole",
    "NodeStatus",
    "ServiceItem",
    "DEFAULT_INVENTORY_PATH",
    "InventoryManager",
    "build_default_inventory",
    "HybridPersistenceAdapter",
    "PersistenceRecord",
    "SyncResult",
]

