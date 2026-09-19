"""Deterministic unit tests for HF-08-03: Telegram integration and legacy backfill.

Governed by:
- docs/handoffs/continuous-autonomy/HF-08-03.md
- docs/handoffs/continuous-autonomy/CONTRACTS.md
- core/demands/backfill.py
- core/integrations/telegram.py
- core/demands/autonomous_intake.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.demands.autonomous_intake import AutonomousIntakeService
from core.demands.backfill import ImportItem, ImportPage, backfill
from core.demands.models import DeliveryStatus, UserTicket
from core.integrations.telegram import (
    TelegramActionType,
    TelegramChat,
    TelegramConfig,
    TelegramGateway,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
)
from core.workflow.control_contracts import (
    IdempotencyConflict,
    IntakeCommand,
)
from core.workflow.control_store import SQLiteControlStore

BASE_TIME = datetime(2026, 9, 19, 10, 0, 0, tzinfo=UTC)


def _make_ticket(
    ticket_id: str,
    title: str = "Legacy Task",
    status: DeliveryStatus | str = DeliveryStatus.PLANNED,
    project_id: str = "darkfac",
    evidence_refs: list[str] | None = None,
    proof: str | None = None,
) -> UserTicket:
    ticket = UserTicket(
        id=ticket_id,
        project_id=project_id,
        title=title,
        problem_statement=f"Legacy problem for {ticket_id}",
        core_journey=[f"User runs {ticket_id}"],
        non_goals=["Out of scope"],
        acceptance_criteria=["Criteria met"],
        status=status if isinstance(status, DeliveryStatus) else DeliveryStatus(status),
        created_at=BASE_TIME,
        updated_at=BASE_TIME,
    )
    if evidence_refs:
        object.__setattr__(ticket, "evidence_refs", evidence_refs)
    if proof:
        object.__setattr__(ticket, "proof", proof)
    return ticket


# ==============================================================================
# 1. Backfill Tests
# ==============================================================================


def test_backfill_transactional_idempotent_restart_with_cursor(tmp_path: Path) -> None:
    """Test that backfill paginates using cursor and restarts before or at cursor idempotently."""
    db_path = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=db_path)

    tickets = [
        _make_ticket("LEG-01", "Task 1", status=DeliveryStatus.COMPLETED, evidence_refs=["ev-1"]),
        _make_ticket("LEG-02", "Task 2", status=DeliveryStatus.COMPLETED, evidence_refs=["ev-2"]),
        _make_ticket("LEG-03", "Task 3", status=DeliveryStatus.COMPLETED, evidence_refs=["ev-3"]),
        _make_ticket("LEG-04", "Task 4", status=DeliveryStatus.COMPLETED, evidence_refs=["ev-4"]),
        _make_ticket("LEG-05", "Task 5", status=DeliveryStatus.COMPLETED, evidence_refs=["ev-5"]),
    ]

    # Page 1: 2 items
    page_1 = backfill(store=store, legacy_tickets=tickets, cursor=None, limit=2)
    assert page_1.cursor is None
    assert page_1.total_processed == 2
    assert len(page_1.items) == 2
    assert page_1.items[0].ticket_id == "LEG-01"
    assert page_1.items[1].ticket_id == "LEG-02"
    assert page_1.has_more is True
    assert page_1.next_cursor == "LEG-02"

    # Count rows in store
    conn = store._connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM runs")
    assert cur.fetchone()[0] == 2

    # Restart before cursor: cursor=None with limit=2
    # Must NOT duplicate records or create concurrent runs
    page_restart = backfill(store=store, legacy_tickets=tickets, cursor=None, limit=2)
    assert page_restart.total_processed == 2
    assert [i.ticket_id for i in page_restart.items] == ["LEG-01", "LEG-02"]
    cur.execute("SELECT COUNT(*) FROM runs")
    assert cur.fetchone()[0] == 2  # Still 2, no duplicates!

    # Continue from next_cursor: page 2
    page_2 = backfill(store=store, legacy_tickets=tickets, cursor=page_1.next_cursor, limit=2)
    assert page_2.cursor == "LEG-02"
    assert page_2.total_processed == 2
    assert [i.ticket_id for i in page_2.items] == ["LEG-03", "LEG-04"]
    assert page_2.has_more is True
    assert page_2.next_cursor == "LEG-04"
    cur.execute("SELECT COUNT(*) FROM runs")
    assert cur.fetchone()[0] == 4

    # Restart at cursor: re-running page 2 must be idempotent
    page_2_restart = backfill(store=store, legacy_tickets=tickets, cursor=page_1.next_cursor, limit=2)
    assert page_2_restart.total_processed == 2
    assert [i.ticket_id for i in page_2_restart.items] == ["LEG-03", "LEG-04"]
    cur.execute("SELECT COUNT(*) FROM runs")
    assert cur.fetchone()[0] == 4  # Still 4, no duplicates!

    # Page 3: last item
    page_3 = backfill(store=store, legacy_tickets=tickets, cursor=page_2.next_cursor, limit=2)
    assert page_3.total_processed == 1
    assert [i.ticket_id for i in page_3.items] == ["LEG-05"]
    assert page_3.has_more is False
    assert page_3.next_cursor is None
    cur.execute("SELECT COUNT(*) FROM runs")
    assert cur.fetchone()[0] == 5


def test_backfill_terminal_legacy_does_not_reexecute_jobs(tmp_path: Path) -> None:
    """Terminal legacy tickets (COMPLETED, CANCELLED with proof) must NOT create pending jobs."""
    db_path = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=db_path)

    tickets = [
        _make_ticket("LEG-COMP-1", "Done Task", status=DeliveryStatus.COMPLETED, evidence_refs=["evidence-pass-sha"]),
        {
            "id": "LEG-CANC-2",
            "project_id": "darkfac",
            "title": "Cancelled Task",
            "status": "cancelled",
            "problem": "No longer needed",
            "journey": ["Discarded"],
            "non_goals": [],
            "criteria": [],
            "proof": "Cancelled by owner decision 2026-09-01",
        },
    ]

    page = backfill(store=store, legacy_tickets=tickets)
    assert page.total_processed == 2
    assert page.items[0].status == "completed"
    assert page.items[0].reconciliation_required is False
    assert page.items[1].status == "cancelled"
    assert page.items[1].reconciliation_required is False

    # Verify runs table statuses
    conn = store._connect()
    cur = conn.cursor()
    cur.execute("SELECT demand_id, status FROM runs ORDER BY demand_id")
    rows = cur.fetchall()
    assert tuple(rows[0]) == ("LEG-CANC-2", "cancelled")
    assert tuple(rows[1]) == ("LEG-COMP-1", "completed")

    # Critical requirement: NO pending jobs in jobs table!
    cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'")
    assert cur.fetchone()[0] == 0

    # Ensure claim returns None
    claim = store.claim(worker="grill-worker", capabilities=["grill_engine"], now=BASE_TIME)
    assert claim is None


def test_backfill_legacy_without_terminal_proof_marks_reconciliation_required(tmp_path: Path) -> None:
    """Legacy tickets without terminal proof must be marked reconciliation_required with no blind runs."""
    db_path = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=db_path)

    tickets = [
        # Claims completed, but has NO proof
        _make_ticket("LEG-NO-PROOF-1", "Unverified Done", status=DeliveryStatus.COMPLETED),
        # Planned/in-progress ticket from legacy
        _make_ticket("LEG-PLANNED-2", "Unfinished Task", status=DeliveryStatus.PLANNED),
        # Dict ticket without proof
        {
            "id": "LEG-DICT-3",
            "project_id": "darkfac",
            "title": "Old dict ticket",
            "status": "completed",
            "problem": "Legacy",
            "journey": ["Step"],
            "non_goals": [],
            "criteria": [],
        },
    ]

    page = backfill(store=store, legacy_tickets=tickets)
    assert page.total_processed == 3
    for item in page.items:
        assert item.reconciliation_required is True
        assert item.status == "reconciliation_required"

    # Verify runs table
    conn = store._connect()
    cur = conn.cursor()
    cur.execute("SELECT demand_id, status FROM runs ORDER BY demand_id")
    rows = cur.fetchall()
    for row in rows:
        assert row[1] == "reconciliation_required"

    # Verify no blind execution runs scheduled
    cur.execute("SELECT COUNT(*) FROM jobs WHERE status = 'pending'")
    assert cur.fetchone()[0] == 0

    # Verify entries in reconciliation_ledger
    cur.execute("SELECT COUNT(*) FROM reconciliation_ledger WHERE action_type = 'flag_reconciliation'")
    assert cur.fetchone()[0] == 3


# ==============================================================================
# 2. Telegram Gateway Tests
# ==============================================================================


def test_telegram_strict_authorization(tmp_path: Path) -> None:
    """Verify strict authorization against authorized_user_ids and authorized_chat_ids."""
    cfg = TelegramConfig(
        authorized_user_ids=[100],
        authorized_chat_ids=[200],
    )
    gw = TelegramGateway(config=cfg, state_dir=tmp_path)

    # 1. Authorized user in authorized chat -> Accepted
    res_auth = gw.process_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": 100, "is_bot": False, "first_name": "Owner"},
                "chat": {"id": 200, "type": "private"},
                "date": 1700000000,
                "text": "/start",
            },
        }
    )
    assert res_auth.authorized is True
    assert res_auth.action == TelegramActionType.START

    # 2. Unauthorized user in authorized chat -> Rejected
    res_unauth_user = gw.process_update(
        {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "from": {"id": 999, "is_bot": False, "first_name": "Intruder"},
                "chat": {"id": 200, "type": "private"},
                "date": 1700000000,
                "text": "/demand Hack something",
            },
        }
    )
    assert res_unauth_user.authorized is False
    assert res_unauth_user.action == TelegramActionType.UNAUTHORIZED

    # 3. Authorized user in unauthorized chat -> Rejected
    res_unauth_chat = gw.process_update(
        {
            "update_id": 3,
            "message": {
                "message_id": 12,
                "from": {"id": 100, "is_bot": False, "first_name": "Owner"},
                "chat": {"id": 999, "type": "group"},
                "date": 1700000000,
                "text": "/demand Something",
            },
        }
    )
    assert res_unauth_chat.authorized is False
    assert res_unauth_chat.action == TelegramActionType.UNAUTHORIZED

    # 4. Fail-closed: No authorized IDs configured -> Rejected
    gw_empty = TelegramGateway(config=TelegramConfig(), state_dir=tmp_path / "empty")
    res_empty = gw_empty.process_update(
        {
            "update_id": 4,
            "message": {
                "message_id": 13,
                "from": {"id": 100, "is_bot": False, "first_name": "Owner"},
                "chat": {"id": 200, "type": "private"},
                "date": 1700000000,
                "text": "/start",
            },
        }
    )
    assert res_empty.authorized is False


def test_telegram_durable_offset_post_commit_and_message_deduplication(tmp_path: Path) -> None:
    """Verify durable offset persistence only post-commit and message deduplication."""
    db_path = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=db_path)
    intake_service = AutonomousIntakeService(store=store)

    state_dir = tmp_path / "telegram_state"
    cfg = TelegramConfig(
        authorized_user_ids=[100],
        authorized_chat_ids=[200],
    )
    gw = TelegramGateway(config=cfg, state_dir=state_dir, intake_service=intake_service)

    # Ingest demand 1
    upd_1 = {
        "update_id": 501,
        "message": {
            "message_id": 1001,
            "from": {"id": 100, "is_bot": False, "first_name": "Owner"},
            "chat": {"id": 200, "type": "private"},
            "date": 1700000000,
            "text": "/demand Add payment gateway integration",
        },
    }
    res_1 = gw.process_update(upd_1)
    assert res_1.authorized is True
    assert res_1.action == TelegramActionType.DEMAND
    assert res_1.error is None
    assert gw.last_offset == 502

    # Check state file persisted on disk
    state_file = state_dir / "gateway_state.json"
    assert state_file.exists()
    state_data = json.loads(state_file.read_text(encoding="utf-8"))
    assert state_data["last_offset"] == 502
    assert 501 in state_data["processed_update_ids"]

    # Replay identical update_id -> Duplicate
    res_replay_update = gw.process_update(upd_1)
    assert res_replay_update.duplicate is True

    # Replay same message under a new update_id -> Deduplicated by message
    upd_same_msg = {
        "update_id": 502,
        "message": {
            "message_id": 1001,
            "from": {"id": 100, "is_bot": False, "first_name": "Owner"},
            "chat": {"id": 200, "type": "private"},
            "date": 1700000000,
            "text": "/demand Add payment gateway integration",
        },
    }
    res_replay_msg = gw.process_update(upd_same_msg)
    assert res_replay_msg.duplicate is True

    # Check that failed transaction does NOT advance offset post-commit
    mock_failing_intake = MagicMock()
    mock_failing_intake.accept.side_effect = RuntimeError("DB write lock failure")
    gw_failing = TelegramGateway(config=cfg, state_dir=tmp_path / "fail_state", intake_service=mock_failing_intake)

    upd_fail = {
        "update_id": 601,
        "message": {
            "message_id": 2001,
            "from": {"id": 100, "is_bot": False, "first_name": "Owner"},
            "chat": {"id": 200, "type": "private"},
            "date": 1700000000,
            "text": "/demand Something that crashes",
        },
    }
    res_fail = gw_failing.process_update(upd_fail)
    assert res_fail.error is not None
    # Offset must NOT advance on transaction failure
    assert gw_failing.last_offset == 0
    assert 601 not in gw_failing.processed_update_ids


def test_shared_single_channel_intake_for_n8n_and_telegram_no_second_queue(tmp_path: Path) -> None:
    """Telegram and n8n gateway share the same intake service with unified queue in ControlStore."""
    db_path = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=db_path)
    intake_service = AutonomousIntakeService(store=store)

    # 1. Telegram gateway wired to shared intake_service
    tg_gw = TelegramGateway(
        config=TelegramConfig(authorized_user_ids=[100], authorized_chat_ids=[200]),
        state_dir=tmp_path / "tg",
        intake_service=intake_service,
    )

    # Submit demand via Telegram
    tg_update = {
        "update_id": 701,
        "message": {
            "message_id": 3001,
            "from": {"id": 100, "is_bot": False, "first_name": "Owner"},
            "chat": {"id": 200, "type": "private"},
            "date": 1700000000,
            "text": "/demand Feature from Telegram",
        },
    }
    tg_res = tg_gw.process_update(tg_update)
    assert tg_res.authorized is True
    assert tg_res.target_id is not None
    tg_demand_id = tg_res.target_id

    # 2. n8n submits via same intake_service (shared intake endpoint)
    n8n_cmd = IntakeCommand(
        project_id="darkfac",
        channel="n8n",
        external_id="n8n-webhook-4001",
        payload={
            "title": "Feature from n8n",
            "problem": "Webhook event triggered",
            "journey": ["n8n receives payload", "forwards to darkfac"],
            "non_goals": [],
            "criteria": ["Processed autonomously"],
        },
        mode="autonomous",
        policy_ref="n8n-policy-v1",
    )
    n8n_receipt = intake_service.accept(n8n_cmd, now=BASE_TIME)
    assert n8n_receipt.demand_id is not None
    n8n_demand_id = n8n_receipt.demand_id

    # Verify both demands reside in the exact same control tables without duplicate queues
    conn = store._connect()
    cur = conn.cursor()
    cur.execute("SELECT channel, external_id, demand_id FROM intake_commands ORDER BY channel")
    channels = cur.fetchall()
    assert len(channels) == 2
    assert channels[0][0] == "n8n"
    assert channels[1][0] == "telegram"

    # Verify single shared queue of pending jobs in ControlStore
    cur.execute("SELECT run_id, stage, status FROM jobs WHERE status = 'pending' ORDER BY run_id")
    jobs = cur.fetchall()
    assert len(jobs) == 2
    assert jobs[0][1] == "grill"
    assert jobs[1][1] == "grill"

    # A single worker can claim both jobs sequentially from the shared queue
    claim_1 = store.claim(worker="unified-worker", capabilities=["grill_engine"], now=BASE_TIME)
    assert claim_1 is not None
    claim_2 = store.claim(worker="unified-worker", capabilities=["grill_engine"], now=BASE_TIME)
    assert claim_2 is not None
    claim_3 = store.claim(worker="unified-worker", capabilities=["grill_engine"], now=BASE_TIME)
    assert claim_3 is None
