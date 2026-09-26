"""Deterministic Acceptance Test Suite for USR-17: Hybrid Persistence Layer (PostgreSQL + Local-First Sync).

Governed by:
- USR-17: Camada de Persistência Híbrida PostgreSQL + Sync R2 & On-Premises (INFRA-07 / INFRA-08)
- Reachability Contract: python -m pytest tests/test_postgres_hybrid_persistence.py -v
- Invariants:
  1. Local-first zero latency and WAL concurrency.
  2. Resilient fail-closed offline fallback without network blocking.
  3. Strict multi-tenant isolation.
  4. Monotonic conflict resolution and outbox draining.
  5. Zero credential leakage.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import concurrent.futures

import pytest

from core.demands.models import UserTicket
from core.demands.store import DemandsStore
from core.infra.persistence import (
    HybridPersistenceAdapter,
    PersistenceRecord,
    SyncResult,
)


@pytest.fixture
def local_adapter(tmp_path: Path) -> HybridPersistenceAdapter:
    """Provide an isolated HybridPersistenceAdapter in local-first offline mode."""
    db_file = tmp_path / "persistence.db"
    return HybridPersistenceAdapter(db_path=db_file)


# ==============================================================================
# 1. Local-First CRUD & Schema Tests
# ==============================================================================


def test_local_crud_lifecycle(local_adapter: HybridPersistenceAdapter) -> None:
    """Verifies complete CRUD operations in local-first SQLite WAL mode."""
    # 1. Put new entity
    record = local_adapter.put(
        collection="telemetry",
        entity_id="run-101",
        data={"model": "gemini-3.8-flash", "tokens": 450, "cost": 0.0012},
    )
    assert record.tenant_id == "darkfac"
    assert record.collection == "telemetry"
    assert record.entity_id == "run-101"
    assert record.version == 1
    assert not record.is_deleted
    assert record.data["model"] == "gemini-3.8-flash"

    # 2. Get entity
    fetched = local_adapter.get(collection="telemetry", entity_id="run-101")
    assert fetched is not None
    assert fetched.data == record.data
    assert fetched.version == 1

    # 3. Update entity (monotonic version increment)
    updated = local_adapter.put(
        collection="telemetry",
        entity_id="run-101",
        data={"model": "gemini-3.8-flash", "tokens": 900, "cost": 0.0024},
    )
    assert updated.version == 2
    assert updated.data["tokens"] == 900

    fetched_updated = local_adapter.get(collection="telemetry", entity_id="run-101")
    assert fetched_updated is not None
    assert fetched_updated.version == 2
    assert fetched_updated.data["tokens"] == 900

    # 4. Soft-delete entity
    deleted = local_adapter.delete(collection="telemetry", entity_id="run-101")
    assert deleted is True

    # Get should return None unless include_deleted is set
    assert local_adapter.get(collection="telemetry", entity_id="run-101") is None
    deleted_record = local_adapter.get(collection="telemetry", entity_id="run-101", include_deleted=True)
    assert deleted_record is not None
    assert deleted_record.is_deleted is True
    assert deleted_record.version == 3


def test_list_and_query_filtering(local_adapter: HybridPersistenceAdapter) -> None:
    """Verifies listing and predicate filtering for entities in a collection."""
    for i in range(5):
        local_adapter.put(
            collection="knowledge",
            entity_id=f"doc-{i}",
            data={"score": i * 10, "tag": "ai" if i % 2 == 0 else "infra"},
        )

    all_docs = local_adapter.list_records(collection="knowledge")
    assert len(all_docs) == 5

    ai_docs = local_adapter.query(
        collection="knowledge",
        predicate=lambda d: d.get("tag") == "ai",
    )
    assert len(ai_docs) == 3

    high_score = local_adapter.query(
        collection="knowledge",
        predicate=lambda d: d.get("score", 0) >= 30,
    )
    assert len(high_score) == 2


# ==============================================================================
# 2. Multi-Tenant Isolation
# ==============================================================================


def test_strict_multi_tenant_partitioning(local_adapter: HybridPersistenceAdapter) -> None:
    """Verifies that different tenants cannot overwrite or leak each other's data."""
    local_adapter.put(
        tenant_id="tenant-alpha",
        collection="secrets",
        entity_id="api-key",
        data={"token": "alpha-secret-999"},
    )
    local_adapter.put(
        tenant_id="tenant-beta",
        collection="secrets",
        entity_id="api-key",
        data={"token": "beta-secret-111"},
    )

    alpha_key = local_adapter.get(collection="secrets", entity_id="api-key", tenant_id="tenant-alpha")
    beta_key = local_adapter.get(collection="secrets", entity_id="api-key", tenant_id="tenant-beta")

    assert alpha_key is not None
    assert alpha_key.data["token"] == "alpha-secret-999"
    assert beta_key is not None
    assert beta_key.data["token"] == "beta-secret-111"

    # Listing alpha only returns alpha
    alpha_list = local_adapter.list_records(collection="secrets", tenant_id="tenant-alpha")
    assert len(alpha_list) == 1
    assert alpha_list[0].data["token"] == "alpha-secret-999"


# ==============================================================================
# 3. Offline Resilient Fallback & Security Fail-Closed
# ==============================================================================


def test_offline_mode_outbox_queueing(local_adapter: HybridPersistenceAdapter) -> None:
    """Verifies mutations are recorded to the outbox when running offline."""
    assert local_adapter.count_pending_outbox() == 0

    local_adapter.put(collection="state", entity_id="node-1", data={"status": "online"})
    local_adapter.put(collection="state", entity_id="node-2", data={"status": "standby"})
    local_adapter.delete(collection="state", entity_id="node-1")

    assert local_adapter.count_pending_outbox() == 3

    # sync_to_remote gracefully reports offline
    res = local_adapter.sync_to_remote()
    assert res.status == "offline"
    assert res.pushed_count == 0
    assert len(res.errors) > 0


def test_forbidden_superuser_fails_closed(tmp_path: Path) -> None:
    """Superuser credentials in database URL must be rejected and disabled immediately."""
    db_file = tmp_path / "persistence.db"
    superuser_url = "postgresql://postgres:mypassword123@vps.darkfactory.internal:5432/darkfac"

    adapter = HybridPersistenceAdapter(db_path=db_file, database_url=superuser_url)
    assert not adapter.is_remote_configured()
    assert adapter.raw_url is None


# ==============================================================================
# 4. Transactional Sync with PostgreSQL Mock
# ==============================================================================


def test_sync_outbox_to_mock_postgres(tmp_path: Path) -> None:
    """Simulates PostgreSQL push and marks outbox records synced upon success."""
    db_file = tmp_path / "persistence.db"
    mock_url = "postgresql://darkfac_worker:secure_token_abc@vps.darkfactory.internal:5432/darkfac"

    adapter = HybridPersistenceAdapter(db_path=db_file, database_url=mock_url)

    # Queue some changes
    adapter.put(collection="runs", entity_id="run-1", data={"stage": "implement"})
    adapter.put(collection="runs", entity_id="run-2", data={"stage": "validate"})
    assert adapter.count_pending_outbox() == 2

    # Mock psycopg module and connection
    mock_psycopg = MagicMock()
    mock_conn = MagicMock()
    mock_cur = MagicMock()

    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_psycopg.connect.return_value = mock_conn

    adapter._psycopg = mock_psycopg

    res = adapter.sync_to_remote()
    assert res.status == "success"
    assert res.pushed_count == 2
    assert adapter.count_pending_outbox() == 0

    # Ensure pg_cur.execute was called for the outbox rows
    assert mock_cur.execute.call_count >= 2
    mock_conn.commit.assert_called_once()


def test_sync_from_mock_postgres(tmp_path: Path) -> None:
    """Simulates pulling remote PostgreSQL records into the local SQLite cache."""
    db_file = tmp_path / "persistence.db"
    mock_url = "postgresql://darkfac_worker:secure_token_abc@vps.darkfactory.internal:5432/darkfac"

    adapter = HybridPersistenceAdapter(db_path=db_file, database_url=mock_url)

    # Setup mock remote data
    remote_records = [
        (
            "darkfac",
            "telemetry",
            "remote-run-99",
            {"source": "vps_agent", "cost": 0.05},
            5,
            datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 26, 12, 10, 0, tzinfo=timezone.utc),
            False,
        )
    ]

    mock_psycopg = MagicMock()
    mock_conn = MagicMock()
    mock_cur = MagicMock()

    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_cur.fetchall.return_value = remote_records
    mock_psycopg.connect.return_value = mock_conn

    adapter._psycopg = mock_psycopg

    res = adapter.sync_from_remote(collection="telemetry")
    assert res.status == "success"
    assert res.pulled_count == 1

    # Verify record now exists locally
    local_rec = adapter.get(collection="telemetry", entity_id="remote-run-99")
    assert local_rec is not None
    assert local_rec.version == 5
    assert local_rec.data["source"] == "vps_agent"


def test_monotonic_conflict_resolution(tmp_path: Path) -> None:
    """Verifies that older remote records do not overwrite newer local records."""
    db_file = tmp_path / "persistence.db"
    mock_url = "postgresql://darkfac_worker:secure_token_abc@vps.darkfactory.internal:5432/darkfac"

    adapter = HybridPersistenceAdapter(db_path=db_file, database_url=mock_url)

    # Put a local record with version 10
    adapter.put(collection="state", entity_id="config-1", data={"val": "local-newer"}, version=10)

    # Remote tries to push version 8 (stale)
    remote_stale = [
        (
            "darkfac",
            "state",
            "config-1",
            {"val": "remote-stale"},
            8,
            datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 26, 10, 5, 0, tzinfo=timezone.utc),
            False,
        )
    ]

    mock_psycopg = MagicMock()
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.cursor.return_value.__enter__.return_value = mock_cur
    mock_cur.fetchall.return_value = remote_stale
    mock_psycopg.connect.return_value = mock_conn
    adapter._psycopg = mock_psycopg

    res = adapter.sync_from_remote(collection="state")
    assert res.status == "success"
    assert res.pulled_count == 0  # Did not overwrite because local is v10 > v8

    current = adapter.get(collection="state", entity_id="config-1")
    assert current is not None
    assert current.data["val"] == "local-newer"
    assert current.version == 10


# ==============================================================================
# 5. DemandsStore Reconciliation Integration
# ==============================================================================


def test_demands_store_reconciliation(local_adapter: HybridPersistenceAdapter, tmp_path: Path) -> None:
    """Verifies integration with DemandsStore: reads tickets and projects to hybrid persistence."""
    demands_path = tmp_path / "demands.json"
    store = DemandsStore(demands_path)

    ticket = UserTicket(
        id="USR-99",
        project_id="darkfac",
        title="Teste de Reconciliação Híbrida",
        problem_statement="Testar sync entre DemandsStore e SQLite/PostgreSQL",
    )
    store.save_ticket(ticket)

    res = local_adapter.sync_demands_store(store)
    assert res.status == "offline"  # Offline mode since no remote is configured

    # Verify ticket was stored in local hybrid persistence
    persisted_ticket = local_adapter.get(collection="demands", entity_id="USR-99")
    assert persisted_ticket is not None
    assert persisted_ticket.data["id"] == "USR-99"
    assert persisted_ticket.data["title"] == "Teste de Reconciliação Híbrida"


# ==============================================================================
# 6. Concurrent Threads Safety (WAL Verification)
# ==============================================================================


def test_concurrent_threads_safety(local_adapter: HybridPersistenceAdapter) -> None:
    """Verifies thread safety across concurrent readers and writers without locks or corruption."""
    def worker(worker_id: int) -> int:
        for i in range(20):
            entity_id = f"worker-{worker_id}-item-{i}"
            local_adapter.put(
                collection="concurrency",
                entity_id=entity_id,
                data={"worker": worker_id, "step": i},
            )
            rec = local_adapter.get(collection="concurrency", entity_id=entity_id)
            assert rec is not None
        return worker_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(worker, w) for w in range(5)]
        for f in concurrent.futures.as_completed(futures):
            assert f.result() in range(5)

    all_records = local_adapter.list_records(collection="concurrency")
    assert len(all_records) == 100
