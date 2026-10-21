from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.demands import DemandsStore, UserTicket
from core.line.backlog_feeder import BacklogFeeder
from core.line.local_progress import ABANDON_AFTER_SECONDS, LocalRunEvent, SqliteSink
from core.line.manual_ownership import active_manual_owner
from core.line.owner_intake import submit_ticket_to_line
from core.workflow.control_store import SQLiteControlStore


NOW = datetime(2026, 10, 21, tzinfo=UTC)


def test_manual_guard_survives_ledger_reload_and_two_coordinators(tmp_path: Path) -> None:
    path = tmp_path / "control.db"
    store = SQLiteControlStore(db_path=path)
    demands = DemandsStore(tmp_path / "demands.json")
    ticket = UserTicket(
        id="USR-327", title="Ownership reload",
        problem_statement="Impedir admissao duplicada durante execucao manual persistida no store compartilhado.",
        core_journey=["Abrir run manual", "Recarregar ledger e consultar alimentador"],
        non_goals=["Nao alterar cotas nem permissoes"],
        acceptance_criteria=["Run manual persistido impede nova admissao apos reload do ledger."],
    )
    demands.save_ticket(ticket)
    sink = SqliteSink(path)
    sink.write(LocalRunEvent(run_id="local-owner", ticket_id=ticket.id, project_id="darkfac",
                            phase="agent", status="running", at=NOW))
    # A fresh ledger still says planned; shared durable events survive both service reloads.
    for _ in range(2):
        reloaded = DemandsStore(demands.path)
        feeder = BacklogFeeder(reloaded, control_store=SQLiteControlStore(db_path=path))
        result = feeder.evaluate_ticket(ticket, now=NOW)
        assert not result.eligible
        assert result.reason == "manual_ownership"
        direct = submit_ticket_to_line(ticket.id, demands_store=reloaded, store=store, now=NOW)
        assert direct.state == "manual_ownership"
        assert direct.run_id == "local-owner"
    sink.write(LocalRunEvent(run_id="local-owner", ticket_id=ticket.id, project_id="darkfac",
                            phase="run", status="succeeded", at=NOW + timedelta(seconds=1)))
    assert store.get_manual_ticket_owner("darkfac", ticket.id, now=NOW) is None
    assert store.get_manual_ticket_owner("other-project", ticket.id, now=NOW) is None


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_terminal_order_and_expiration(status: str) -> None:
    event = {"run_id": "manual", "phase": "agent", "status": "running", "at": NOW}
    terminal = {**event, "phase": "run", "status": status}
    assert active_manual_owner([terminal, event], now=NOW) is None
    assert active_manual_owner([event, terminal], now=NOW) == "manual"
    assert active_manual_owner([event], now=NOW + timedelta(seconds=ABANDON_AFTER_SECONDS + 1)) is None


def test_invalid_timestamp_fails_closed_and_execution_telemetry_is_not_ownership() -> None:
    event = {"run_id": "manual", "phase": "agent", "status": "running", "at": "invalid"}
    with pytest.raises(ValueError, match="manual_ownership_invalid_timestamp"):
        active_manual_owner([event], now=NOW)
    assert active_manual_owner([{**event, "phase": "execution"}], now=NOW) is None


def test_lookup_failure_blocks_both_admission_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.workflow.control_contracts import StoreUnavailableError

    store = SQLiteControlStore(db_path=tmp_path / "control.db")
    demands = DemandsStore(tmp_path / "demands.json")
    ticket = UserTicket(id="USR-327", title="Lookup failure", problem_statement="A defined problem",
                        non_goals=["No fallback"], acceptance_criteria=["No run created on unavailable ownership"])
    demands.save_ticket(ticket)

    def unavailable(*args: object, **kwargs: object) -> None:
        raise StoreUnavailableError("ownership unavailable")

    monkeypatch.setattr(store, "get_manual_ticket_owner", unavailable)
    refused = submit_ticket_to_line(ticket.id, demands_store=demands, store=store, now=NOW)
    assert not refused.ok
    assert refused.run_id is None


def test_postgres_reader_is_scoped_and_bounded() -> None:
    from unittest.mock import MagicMock
    from core.orchestrator.adapters.control_postgres import PostgresControlStore

    store = object.__new__(PostgresControlStore)
    store.mock_mode = False
    store.raw_url = "postgresql://example.invalid/test"
    store._psycopg = MagicMock()
    cursor = store._psycopg.connect.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = ("local_run_events",)
    cursor.fetchall.return_value = [("local-owner", "agent", "running", NOW)]
    assert store.get_manual_ticket_owner("pilot", "PIL-1", now=NOW) == "local-owner"
    assert cursor.execute.call_args.args[1] == ("pilot", "PIL-1")
    assert store._psycopg.connect.call_args.kwargs["connect_timeout"] == 5


def test_postgres_lookup_error_does_not_disclose_driver_message() -> None:
    from unittest.mock import MagicMock
    from core.orchestrator.adapters.control_postgres import PostgresControlStore
    from core.workflow.control_contracts import StoreUnavailableError

    store = object.__new__(PostgresControlStore)
    store.mock_mode = False
    store.raw_url = "postgresql://example.invalid/test"
    store._psycopg = MagicMock()
    store._psycopg.connect.side_effect = RuntimeError("private driver details")
    with pytest.raises(StoreUnavailableError, match="Manual ownership lookup unavailable") as exc:
        store.get_manual_ticket_owner("darkfac", "USR-327", now=NOW)
    assert "private" not in str(exc.value)
