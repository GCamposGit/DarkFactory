"""Single coordinator lease stored in the shared line registry.

A session heartbeat and the coordinator lease are different rows. Registering
the session that is asking for a briefing never makes that session the
coordinator. Only an explicit owner decision replaces the lease, and it does
so inside one immediate transaction so two machines cannot both hold it.

The registry file is the shared state (``DARKFAC_SHARED_STATE_PATH`` or
``<state root>/state/shared.sqlite3``). Host, session and harness travel in
the row, so the same file is the view across PCs that point at it.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from core.line.session_identity import SessionIdentity
from core.line.shared_state import DEFAULT_SHARED_STATE_ENV, SharedState


logger = logging.getLogger("darkfac.line.coordination")

ORIGIN_OWNER_DECISION = "owner_decision"
ABSENCE_NO_COORDINATOR = "no_coordinator"
ABSENCE_HEARTBEAT_EXPIRED = "heartbeat_expired"
LEASE_TTL_SECONDS = 900
STORED_UNKNOWN_HARNESS = "unknown"


class AssumptionRefused(RuntimeError):
    """The caller asked to take coordination without an explicit owner decision."""

    def __init__(self, cause: str = "owner_decision_required") -> None:
        super().__init__(cause)
        self.cause = cause


@dataclass(frozen=True)
class LeaseView:
    lease_id: str
    session_id: str
    harness: str
    model: str | None
    host: str
    decided_at: str
    origin: str
    last_heartbeat: str
    expires_at: str
    age_seconds: int
    freshness: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "session_id": self.session_id,
            "harness": self.harness,
            "model": self.model,
            "host": self.host,
            "decided_at": self.decided_at,
            "origin": self.origin,
            "last_heartbeat": self.last_heartbeat,
            "expires_at": self.expires_at,
            "age_seconds": self.age_seconds,
            "freshness": self.freshness,
        }


def resolve_state_path(explicit: str | None = None) -> str:
    """Absolute registry path. An explicit argument wins over the environment."""

    if explicit and explicit.strip():
        from pathlib import Path

        return str(Path(explicit).expanduser().resolve())
    configured = os.environ.get(DEFAULT_SHARED_STATE_ENV, "").strip()
    if configured:
        from pathlib import Path

        return str(Path(configured).expanduser().resolve())
    from core.paths import state_root

    return str((state_root() / "state" / "shared.sqlite3").resolve())


def open_state(explicit: str | None = None) -> SharedState:
    return SharedState(resolve_state_path(explicit))


def consult(
    state: SharedState,
    identity: SessionIdentity,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Record this session and return the coordinator view. Does not take the lease."""

    moment = _aware(now)
    with state.transaction(immediate=True) as connection:
        _touch_session(connection, identity, moment)
        _refresh_lease_if_holder(connection, identity, moment)
        view = _read_view(connection, identity, moment)
    return view


def assume(
    state: SharedState,
    identity: SessionIdentity,
    *,
    owner_decision: bool,
    now: datetime,
) -> dict[str, Any]:
    """Replace the lease immediately when the caller is an explicit owner decision.

    Any harness, including an unrecognized one, may assume. A call that is not
    marked as the owner's decision writes nothing.
    """

    if not owner_decision:
        raise AssumptionRefused()
    moment = _aware(now)
    with state.transaction(immediate=True) as connection:
        previous = connection.execute(
            "SELECT lease_id, session_id, harness, host FROM coordination_lease WHERE singleton_id = 1"
        ).fetchone()
        _touch_session(connection, identity, moment)
        lease_id = str(uuid.uuid4())
        decided = _epoch(moment)
        expires = _epoch(moment + timedelta(seconds=LEASE_TTL_SECONDS))
        connection.execute(
            """
            INSERT INTO coordination_lease (
                singleton_id, lease_id, session_id, harness, model, host,
                decided_at, origin, last_heartbeat, expires_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                lease_id = excluded.lease_id,
                session_id = excluded.session_id,
                harness = excluded.harness,
                model = excluded.model,
                host = excluded.host,
                decided_at = excluded.decided_at,
                origin = excluded.origin,
                last_heartbeat = excluded.last_heartbeat,
                expires_at = excluded.expires_at
            """,
            (
                lease_id,
                identity.session_id,
                _stored_harness(identity),
                identity.model,
                identity.host,
                decided,
                ORIGIN_OWNER_DECISION,
                decided,
                expires,
            ),
        )
        connection.execute(
            """
            INSERT INTO coordination_status (singleton_id, absence_reason, changed_at)
            VALUES (1, NULL, ?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                absence_reason = NULL,
                changed_at = excluded.changed_at
            """,
            (decided,),
        )
        details = {
            "origin": ORIGIN_OWNER_DECISION,
            "session_id": identity.session_id,
            "harness": _stored_harness(identity),
            "host": identity.host,
            "previous_lease_id": None if previous is None else previous["lease_id"],
            "previous_session_id": None if previous is None else previous["session_id"],
        }
        connection.execute(
            """
            INSERT INTO coordination_events (
                event_type, session_id, lease_id, actor_session_id, cause, created_at, details_json
            ) VALUES ('owner_assume', ?, ?, ?, ?, ?, ?)
            """,
            (
                identity.session_id,
                lease_id,
                identity.session_id,
                ORIGIN_OWNER_DECISION,
                decided,
                json.dumps(details, ensure_ascii=False),
            ),
        )
        view = _read_view(connection, identity, moment)
    logger.info(
        "coordinator_assumed session=%s harness=%s host=%s lease=%s",
        identity.session_id,
        _stored_harness(identity),
        identity.host,
        lease_id,
    )
    view["assumed"] = True
    return view


def _touch_session(connection: Any, identity: SessionIdentity, now: datetime) -> None:
    started = _parse_epoch(identity.started_at, fallback=now)
    heartbeat = _epoch(now)
    expires = _epoch(now + timedelta(seconds=LEASE_TTL_SECONDS))
    connection.execute(
        """
        INSERT INTO line_sessions (
            session_id, harness, model, host, started_at, last_heartbeat, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            harness = excluded.harness,
            model = excluded.model,
            host = excluded.host,
            last_heartbeat = excluded.last_heartbeat,
            expires_at = excluded.expires_at
        """,
        (
            identity.session_id,
            _stored_harness(identity),
            identity.model,
            identity.host,
            started,
            heartbeat,
            expires,
        ),
    )


def _refresh_lease_if_holder(connection: Any, identity: SessionIdentity, now: datetime) -> None:
    """Extend a still-valid lease held by this session. An expired lease stays expired."""

    heartbeat = _epoch(now)
    expires = _epoch(now + timedelta(seconds=LEASE_TTL_SECONDS))
    connection.execute(
        """
        UPDATE coordination_lease
        SET last_heartbeat = ?, expires_at = ?
        WHERE singleton_id = 1 AND session_id = ? AND expires_at > ?
        """,
        (heartbeat, expires, identity.session_id, heartbeat),
    )


def _read_view(connection: Any, identity: SessionIdentity, now: datetime) -> dict[str, Any]:
    row = connection.execute(
        "SELECT * FROM coordination_lease WHERE singleton_id = 1"
    ).fetchone()
    status = connection.execute(
        "SELECT absence_reason, changed_at FROM coordination_status WHERE singleton_id = 1"
    ).fetchone()
    now_epoch = _epoch(now)
    coordinator: LeaseView | None = None
    expired: LeaseView | None = None
    absence = ABSENCE_NO_COORDINATOR
    if row is not None:
        lease = _lease_from_row(row, now_epoch)
        if row["expires_at"] > now_epoch:
            coordinator = lease
            absence_reason = None
        else:
            expired = lease
            absence_reason = ABSENCE_HEARTBEAT_EXPIRED
    else:
        absence_reason = absence
        if status is not None and status["absence_reason"]:
            absence_reason = str(status["absence_reason"])
    sessions = [
        _session_from_row(item, now_epoch)
        for item in connection.execute(
            """
            SELECT * FROM line_sessions
            WHERE expires_at > ?
            ORDER BY started_at, session_id
            """,
            (now_epoch,),
        ).fetchall()
    ]
    coordinator_id = None if coordinator is None else coordinator.session_id
    parallel = [item for item in sessions if item["session_id"] != coordinator_id]
    return {
        "observed_at": _iso(now_epoch),
        "caller": {
            **identity.to_dict(),
            "is_coordinator": coordinator_id == identity.session_id,
        },
        "coordinator": None if coordinator is None else coordinator.to_dict(),
        "expired_coordinator": None if expired is None else expired.to_dict(),
        "absence_reason": None if coordinator is not None else absence_reason,
        "sessions": sessions,
        "parallel_sessions": parallel,
        "assumed": False,
    }


def _lease_from_row(row: Any, now_epoch: float) -> LeaseView:
    age = max(0, int(now_epoch - float(row["last_heartbeat"])))
    fresh = float(row["expires_at"]) > now_epoch
    return LeaseView(
        lease_id=str(row["lease_id"]),
        session_id=str(row["session_id"]),
        harness=str(row["harness"]),
        model=None if row["model"] is None else str(row["model"]),
        host=str(row["host"]),
        decided_at=_iso(float(row["decided_at"])),
        origin=str(row["origin"]),
        last_heartbeat=_iso(float(row["last_heartbeat"])),
        expires_at=_iso(float(row["expires_at"])),
        age_seconds=age,
        freshness="current" if fresh else "stale",
    )


def _session_from_row(row: Any, now_epoch: float) -> dict[str, Any]:
    age = max(0, int(now_epoch - float(row["last_heartbeat"])))
    return {
        "session_id": str(row["session_id"]),
        "harness": str(row["harness"]),
        "model": None if row["model"] is None else str(row["model"]),
        "host": str(row["host"]),
        "started_at": _iso(float(row["started_at"])),
        "last_heartbeat": _iso(float(row["last_heartbeat"])),
        "expires_at": _iso(float(row["expires_at"])),
        "age_seconds": age,
        "freshness": "current",
    }


def _stored_harness(identity: SessionIdentity) -> str:
    return identity.harness or STORED_UNKNOWN_HARNESS


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _epoch(moment: datetime) -> float:
    return _aware(moment).timestamp()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_epoch(value: str, *, fallback: datetime) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _epoch(fallback)
    return _epoch(parsed)
