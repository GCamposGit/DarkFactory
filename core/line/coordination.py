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


class CoordinationRefused(RuntimeError):
    """An exclusive coordination action was requested by a session that is not the coordinator."""

    def __init__(
        self,
        action: str,
        *,
        coordinator: dict[str, Any] | None,
        absence_reason: str | None,
        session_id: str,
    ) -> None:
        self.action = action
        self.coordinator = coordinator
        self.absence_reason = absence_reason
        self.session_id = session_id
        if coordinator is not None:
            detail = (
                f"coordenador atual: harness={coordinator['harness']} host={coordinator['host']} "
                f"sessao={coordinator['session_id']} heartbeat={coordinator['last_heartbeat']}"
            )
        else:
            detail = f"sem coordenador: causa={absence_reason}"
        super().__init__(
            f"acao exclusiva da coordenacao recusada ({action}): {detail}. "
            "Sessao paralela segue o fluxo normal de ticket; o owner assume com "
            "`python scripts/line_briefing.py assumir --decisao-owner`."
        )


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
        _record_expiry(connection, moment)
        _refresh_lease_if_holder(connection, identity, moment)
        view = _read_view(connection, identity, moment)
    return view


def authorize(
    state: SharedState,
    identity: SessionIdentity,
    action: str,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Allow an exclusive coordination action only for the session holding a valid lease.

    The comparison runs in one immediate transaction. The coordinator's heartbeat is renewed;
    any other session gets ``CoordinationRefused`` naming the current coordinator or the cause
    of the absence. Ordinary work (tickets, planning, analysis) never calls this.
    """

    moment = _aware(now)
    with state.transaction(immediate=True) as connection:
        _touch_session(connection, identity, moment)
        _record_expiry(connection, moment)
        _refresh_lease_if_holder(connection, identity, moment)
        view = _read_view(connection, identity, moment)
    coordinator = view["coordinator"]
    if coordinator is not None and coordinator["session_id"] == identity.session_id:
        return view
    raise CoordinationRefused(
        action,
        coordinator=coordinator,
        absence_reason=view["absence_reason"],
        session_id=identity.session_id,
    )


def require_coordinator(
    action: str,
    *,
    state: SharedState | None = None,
    identity: SessionIdentity | None = None,
    now: datetime | None = None,
) -> None:
    """Central guard for the exclusive actions: dispatcher, fleet policy and coordination handoff.

    A process without a live harness session signal (the dispatcher service, a fleet worker, the
    pump) is not an interactive session and is not subject to the lease. A detected session must
    be the coordinator, otherwise ``CoordinationRefused`` is raised.
    """

    from core.line.line_lock import is_fleet_mode
    from core.line.session_identity import current_session_identity

    declared = os.environ.get("DARKFAC_EXECUTION_CONTEXT", "").strip().lower()
    if identity is None and (declared == "headless" or is_fleet_mode()):
        return
    who = identity or current_session_identity()
    if who.harness is None:
        return
    authorize(
        state or open_state(),
        who,
        action,
        now=now or datetime.now(timezone.utc),
    )


def publish_handoff(
    path: Any,
    content: str,
    *,
    state: SharedState | None = None,
    identity: SessionIdentity | None = None,
    now: datetime | None = None,
) -> None:
    """Write the coordination handoff file. Reserved to the coordinator."""

    from pathlib import Path

    require_coordinator("publicar_handoff", state=state, identity=identity, now=now)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(content)


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


def _record_expiry(connection: Any, now: datetime) -> None:
    """Persist ``heartbeat_expired`` and one ``lease_expired`` event for an expired lease."""

    moment = _epoch(now)
    row = connection.execute(
        "SELECT lease_id, session_id, harness, host, expires_at FROM coordination_lease WHERE singleton_id = 1"
    ).fetchone()
    if row is None or float(row["expires_at"]) > moment:
        return
    already = connection.execute(
        "SELECT 1 FROM coordination_events WHERE event_type = 'lease_expired' AND lease_id = ?",
        (row["lease_id"],),
    ).fetchone()
    if already is not None:
        return
    connection.execute(
        """
        INSERT INTO coordination_status (singleton_id, absence_reason, changed_at)
        VALUES (1, ?, ?)
        ON CONFLICT(singleton_id) DO UPDATE SET
            absence_reason = excluded.absence_reason,
            changed_at = excluded.changed_at
        """,
        (ABSENCE_HEARTBEAT_EXPIRED, moment),
    )
    details = {
        "session_id": row["session_id"],
        "harness": row["harness"],
        "host": row["host"],
        "expired_at": _iso(float(row["expires_at"])),
    }
    connection.execute(
        """
        INSERT INTO coordination_events (
            event_type, session_id, lease_id, actor_session_id, cause, created_at, details_json
        ) VALUES ('lease_expired', ?, ?, NULL, ?, ?, ?)
        """,
        (
            row["session_id"],
            row["lease_id"],
            ABSENCE_HEARTBEAT_EXPIRED,
            moment,
            json.dumps(details, ensure_ascii=False),
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
