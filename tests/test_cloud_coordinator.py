"""Test suite for HF-03-03: Cloud Coordinator Service.

Tests:
- Default inspection reports waiting_access without error when URL is unset.
- Max concurrent slots configuration takes effect.
- Scan and recover returns empty list cleanly without throwing when disconnected.
- Clean shutdown.
"""

from __future__ import annotations

import pytest
from core.orchestrator.cloud_coordinator import CloudCoordinator, CoordinatorStatus


def test_coordinator_inspects_cleanly_without_database() -> None:
    coordinator = CloudCoordinator(database_url=None, max_concurrent_slots=2)
    status = coordinator.inspect_status()
    assert isinstance(status, CoordinatorStatus)
    assert status.role == "coordinator"
    assert status.application_version == "v1"
    assert status.database_status == "waiting_access"
    assert status.max_concurrent_slots == 2


def test_coordinator_custom_slots_and_version() -> None:
    coordinator = CloudCoordinator(database_url=None, max_concurrent_slots=4, application_version="v2")
    status = coordinator.inspect_status()
    assert status.max_concurrent_slots == 4
    assert status.application_version == "v2"


def test_coordinator_recovery_scan_fails_closed_when_disconnected() -> None:
    coordinator = CloudCoordinator(database_url=None)
    recovered = coordinator.scan_and_recover_pending()
    assert recovered == []


def test_coordinator_shutdown_is_idempotent() -> None:
    coordinator = CloudCoordinator(database_url=None)
    coordinator.shutdown()
    coordinator.shutdown()
