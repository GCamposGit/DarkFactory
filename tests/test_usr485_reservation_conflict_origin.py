"""USR-485 T3: reservation conflicts include the resolved host and its source."""

from __future__ import annotations

import pytest

from core.line import line_lock
from core.workflow.control_contracts import (
    FleetCapacityExceededError,
    HostBusyError,
    ReservationConflictError,
    TicketAlreadyActiveError,
)


class ConflictingStore:
    mock_mode = False

    def __init__(self, error: ReservationConflictError) -> None:
        self.error = error

    def reserve_cycle(self, **_: object) -> None:
        raise self.error


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (HostBusyError("host busy"), "host busy"),
        (TicketAlreadyActiveError("ticket active", ticket_id="USR-485", run_id="run-1"), "ticket active"),
        (FleetCapacityExceededError("fleet full"), "fleet full"),
    ],
)
def test_reservation_conflicts_report_explicit_host_and_release_local_lock(
    tmp_path, monkeypatch, error: ReservationConflictError, message: str
) -> None:
    monkeypatch.delenv("DARKFAC_HOST_ID", raising=False)
    monkeypatch.setattr(line_lock.socket, "gethostname", lambda: "AI_Notebook")
    lock = line_lock.LineLock(
        "USR-485", root=tmp_path, store=ConflictingStore(error), host_id="desktop", fleet_mode=True
    )

    with pytest.raises(type(error), match=message) as caught:
        lock.acquire()

    assert "host resolvido: desktop (origem: argumento explícito; hostname: AI_Notebook)" in str(caught.value)
    assert lock.acquired is False
    assert not line_lock.lock_path(tmp_path, fleet_host="desktop").exists()
    if isinstance(error, TicketAlreadyActiveError):
        assert caught.value.existing_execution == {
            "ticket_id": "USR-485",
            "run_id": "run-1",
        }


@pytest.mark.parametrize(
    ("host_id", "env_host_id", "source"),
    [
        (None, "desktop", "DARKFAC_HOST_ID"),
        (None, None, "hostname"),
    ],
)
def test_conflict_reports_environment_or_hostname_resolution(
    tmp_path, monkeypatch, host_id: str | None, env_host_id: str | None, source: str
) -> None:
    if env_host_id is None:
        monkeypatch.delenv("DARKFAC_HOST_ID", raising=False)
    else:
        monkeypatch.setenv("DARKFAC_HOST_ID", env_host_id)
    monkeypatch.setattr(line_lock.socket, "gethostname", lambda: "AI_Notebook")
    lock = line_lock.LineLock(
        "USR-485", root=tmp_path, store=ConflictingStore(HostBusyError("busy")),
        host_id=host_id, fleet_mode=True,
    )

    with pytest.raises(HostBusyError) as caught:
        lock.acquire()

    assert f"host resolvido: {'desktop' if source == 'DARKFAC_HOST_ID' else 'notebook'} " in str(caught.value)
    assert f"(origem: {source}; hostname: AI_Notebook)" in str(caught.value)
    assert not line_lock.lock_path(tmp_path, fleet_host=lock._host_resolution.host_id).exists()


def test_fail_closed_conflict_reports_hostname_resolution_and_releases_local_lock(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("DARKFAC_HOST_ID", raising=False)
    monkeypatch.setattr(line_lock.socket, "gethostname", lambda: "AI_Notebook")
    monkeypatch.setattr("core.line.store_selection.default_control_store", lambda: None)
    lock = line_lock.LineLock("USR-485", root=tmp_path, require_reservation=True)

    with pytest.raises(ReservationConflictError, match="falhou fechada") as caught:
        lock.acquire()

    assert "host resolvido: notebook (origem: hostname; hostname: AI_Notebook)" in str(caught.value)
    assert lock.acquired is False
    assert not line_lock.lock_path(tmp_path).exists()
