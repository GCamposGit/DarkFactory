#!/usr/bin/env python3
"""Verdict cache and cross-host single-flight for the validation harness.

A PASS verdict is keyed on the *tree* hash (``git rev-parse HEAD^{tree}``),
not the commit SHA, so a rebase or merge commit that reproduces an
already-tested tree reuses the verdict instead of re-running ~500s of
tests. Failures are never cached (flakes must always get a fresh run).

Two backends:

- **local**: a JSON file in the machine-global state dir
  (:func:`core.harness.suite_lock.state_dir`), atomic write via temp file +
  ``os.replace``.
- **remote** (optional): a Postgres table, used only when
  ``DARKFAC_HF02_DATABASE_URL`` is set *and* ``psycopg`` is importable.
  Every remote failure degrades to local-only with a single warning line;
  credentials are never logged (see ``sanitize_database_url``).

Cross-host single-flight uses a second Postgres table (``harness_inflight``)
so that when host B starts the same (tree, config, steps, platform) run host
A is already running, B polls for A's verdict instead of burning its own
CPU on a duplicate run. That polling happens *before* (and independently
of) the caller's local machine-wide lock, so host B never idles its own
queue of suites while waiting on host A. Claims use a short lease
(:func:`inflight_lease_sec`, default 90s) refreshed by a heartbeat thread
(:func:`heartbeat_inflight`) for as long as the holder is actually running,
so a crashed holder only blocks the key for one lease period rather than
the whole run.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from core.harness.suite_lock import state_dir

_CONNECT_TIMEOUT_SEC = 5
_DEFAULT_REMOTE_WAIT_SEC = 900
_INFLIGHT_POLL_SEC = 10
_DEFAULT_INFLIGHT_LEASE_SEC = 90.0
_MIN_HEARTBEAT_INTERVAL_SEC = 0.05


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def cache_disabled() -> bool:
    if _env_truthy("CI"):
        return True
    if os.environ.get("DARKFAC_HARNESS_CACHE", "").strip().lower() == "off":
        return True
    return False


def platform_family() -> str:
    return "windows" if sys.platform == "win32" else "posix"


def python_version_tag() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def tree_sha(project_root: Path) -> str | None:
    """The working tree's committed tree hash, or ``None`` if unavailable."""

    try:
        process = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if process.returncode != 0:
        return None
    value = process.stdout.strip().lower()
    return value or None


def compute_cache_key(
    *,
    tree_hash: str,
    config_hash: str,
    step_names: list[str],
    quick: bool,
    include_holdout: bool,
) -> str:
    """Deterministic key: tree + config + selected steps + flags + platform + python."""

    payload = {
        "tree_sha": tree_hash,
        "config_hash": config_hash,
        "step_names": sorted(step_names),
        "quick": quick,
        "include_holdout": include_holdout,
        "platform_family": platform_family(),
        "python_version": python_version_tag(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _local_store_path() -> Path:
    return state_dir() / "verdicts.json"


def _read_local_store() -> dict[str, Any]:
    path = _local_store_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_local_store(data: dict[str, Any]) -> None:
    path = _local_store_path()
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp_path, path)


def get_local_verdict(key: str) -> dict[str, Any] | None:
    return _read_local_store().get(key)


def set_local_verdict(key: str, record: dict[str, Any]) -> None:
    data = _read_local_store()
    data[key] = record
    _write_local_store(data)


def _import_psycopg() -> Any | None:
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        return None
    return psycopg


def _database_url() -> str | None:
    url = os.environ.get("DARKFAC_HF02_DATABASE_URL")
    return url.strip() if url and url.strip() else None


VERDICTS_DDL = """
CREATE TABLE IF NOT EXISTS harness_verdicts (
    key VARCHAR(64) PRIMARY KEY,
    tree_sha VARCHAR(64) NOT NULL,
    candidate_sha VARCHAR(64) NOT NULL,
    config_hash VARCHAR(64) NOT NULL,
    os_family VARCHAR(16) NOT NULL,
    py_version VARCHAR(16) NOT NULL,
    host VARCHAR(160) NOT NULL,
    result_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

INFLIGHT_DDL = """
CREATE TABLE IF NOT EXISTS harness_inflight (
    key VARCHAR(64) PRIMARY KEY,
    host VARCHAR(160) NOT NULL,
    pid INTEGER NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL
);
"""


def _sanitized_warning(prefix: str, exc: Exception) -> None:
    from core.orchestrator.cloud_db import sanitize_database_url

    message = str(exc)
    url = _database_url()
    if url:
        message = message.replace(url, sanitize_database_url(url))
    print(f"[WARN] {prefix}: {message}")


def _connect(psycopg_module: Any, url: str) -> Any:
    return psycopg_module.connect(url, connect_timeout=_CONNECT_TIMEOUT_SEC)


def get_remote_verdict(key: str) -> dict[str, Any] | None:
    """Fetch a cached PASS verdict from Postgres, or ``None`` on any failure/absence."""

    url = _database_url()
    if not url:
        return None
    psycopg_module = _import_psycopg()
    if psycopg_module is None:
        return None
    try:
        with _connect(psycopg_module, url) as conn:
            with conn.cursor() as cur:
                cur.execute(VERDICTS_DDL)
                cur.execute(
                    "SELECT tree_sha, candidate_sha, config_hash, os_family, py_version, host, "
                    "result_json, created_at FROM harness_verdicts WHERE key = %s",
                    (key,),
                )
                row = cur.fetchone()
                conn.commit()
                if not row:
                    return None
                created_at = row[7]
                return {
                    "tree_sha": row[0],
                    "candidate_sha": row[1],
                    "config_hash": row[2],
                    "os_family": row[3],
                    "py_version": row[4],
                    "host": row[5],
                    "result": json.loads(row[6]),
                    "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at),
                }
    except Exception as exc:  # noqa: BLE001 - any remote failure degrades to local-only
        _sanitized_warning("Remote verdict cache unavailable", exc)
        return None


def set_remote_verdict(key: str, record: dict[str, Any]) -> None:
    url = _database_url()
    if not url:
        return
    psycopg_module = _import_psycopg()
    if psycopg_module is None:
        return
    try:
        with _connect(psycopg_module, url) as conn:
            with conn.cursor() as cur:
                cur.execute(VERDICTS_DDL)
                cur.execute(
                    """
                    INSERT INTO harness_verdicts (
                        key, tree_sha, candidate_sha, config_hash, os_family, py_version, host, result_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (key) DO UPDATE SET
                        candidate_sha = EXCLUDED.candidate_sha,
                        host = EXCLUDED.host,
                        result_json = EXCLUDED.result_json,
                        created_at = CURRENT_TIMESTAMP
                    """,
                    (
                        key,
                        record["tree_sha"],
                        record["candidate_sha"],
                        record["config_hash"],
                        record["os_family"],
                        record["py_version"],
                        record["host"],
                        json.dumps(record["result"]),
                    ),
                )
                conn.commit()
    except Exception as exc:  # noqa: BLE001
        _sanitized_warning("Remote verdict cache write failed", exc)


def get_verdict(key: str) -> dict[str, Any] | None:
    """Local first, then remote (remote populates local so it's found fast next time)."""

    local = get_local_verdict(key)
    if local is not None:
        return local
    remote = get_remote_verdict(key)
    if remote is not None:
        set_local_verdict(key, remote)
    return remote


def store_verdict(key: str, record: dict[str, Any]) -> None:
    set_local_verdict(key, record)
    set_remote_verdict(key, record)


def build_record(*, tree_hash: str, candidate_sha: str, config_hash: str, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "tree_sha": tree_hash,
        "candidate_sha": candidate_sha,
        "config_hash": config_hash,
        "os_family": platform_family(),
        "py_version": python_version_tag(),
        "host": socket.gethostname(),
        "result": result,
        "created_at": time.time(),
    }


def acquire_inflight(key: str, *, expires_in_sec: float) -> bool:
    """Claim cross-host single-flight for ``key``. ``True`` if this host now owns it."""

    url = _database_url()
    if not url:
        return True  # no shared coordination available; proceed locally
    psycopg_module = _import_psycopg()
    if psycopg_module is None:
        return True
    try:
        with _connect(psycopg_module, url) as conn:
            with conn.cursor() as cur:
                cur.execute(INFLIGHT_DDL)
                cur.execute(
                    "SELECT host, pid, expires_at FROM harness_inflight WHERE key = %s FOR UPDATE",
                    (key,),
                )
                row = cur.fetchone()
                now_expr = "CURRENT_TIMESTAMP"
                if row is not None:
                    expires_at = row[2]
                    cur.execute("SELECT %s < CURRENT_TIMESTAMP", (expires_at,))
                    is_expired = cur.fetchone()[0]
                    if not is_expired:
                        conn.commit()
                        return False  # another host holds a live inflight claim
                cur.execute(
                    f"""
                    INSERT INTO harness_inflight (key, host, pid, started_at, expires_at)
                    VALUES (%s, %s, %s, {now_expr}, {now_expr} + %s * INTERVAL '1 second')
                    ON CONFLICT (key) DO UPDATE SET
                        host = EXCLUDED.host,
                        pid = EXCLUDED.pid,
                        started_at = {now_expr},
                        expires_at = {now_expr} + %s * INTERVAL '1 second'
                    """,
                    (key, socket.gethostname(), os.getpid(), expires_in_sec, expires_in_sec),
                )
                conn.commit()
                return True
    except Exception as exc:  # noqa: BLE001
        _sanitized_warning("Remote single-flight unavailable", exc)
        return True


def release_inflight(key: str) -> None:
    url = _database_url()
    if not url:
        return
    psycopg_module = _import_psycopg()
    if psycopg_module is None:
        return
    with contextlib.suppress(Exception):
        with _connect(psycopg_module, url) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM harness_inflight WHERE key = %s AND pid = %s", (key, os.getpid()))
                conn.commit()


def get_inflight_claim(key: str) -> dict[str, Any] | None:
    """Return the *live* inflight claim for ``key``, or ``None``.

    ``None`` covers every case in which there is nothing left to wait for:
    no row at all, a row whose lease has already expired (abandoned --
    crashed process/container, or a heartbeat that stopped), or the remote
    backend being unavailable/unconfigured. Waiters use this to notice a
    holder is gone -- including a holder that FINISHED with a failure,
    since failures are never stored as verdicts and the holder simply
    deletes its own inflight row -- without waiting out a full poll
    deadline.
    """

    url = _database_url()
    if not url:
        return None
    psycopg_module = _import_psycopg()
    if psycopg_module is None:
        return None
    try:
        with _connect(psycopg_module, url) as conn:
            with conn.cursor() as cur:
                cur.execute(INFLIGHT_DDL)
                cur.execute(
                    "SELECT host, pid, expires_at FROM harness_inflight WHERE key = %s",
                    (key,),
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return None
                host, pid, expires_at = row
                cur.execute("SELECT %s < CURRENT_TIMESTAMP", (expires_at,))
                is_expired = cur.fetchone()[0]
                conn.commit()
                if is_expired:
                    return None
                return {"host": host, "pid": pid, "expires_at": expires_at}
    except Exception as exc:  # noqa: BLE001 - any remote failure degrades to "nothing to wait for"
        _sanitized_warning("Remote inflight status unavailable", exc)
        return None


def refresh_inflight(key: str, *, expires_in_sec: float) -> bool:
    """Extend ``key``'s inflight lease -- but only while this host+pid still
    owns it. Returns ``False`` (without changing anything) once another host
    has taken over, e.g. because a previous heartbeat tick was too slow and
    the lease already lapsed and got claimed by someone else. When no remote
    backend is configured there is nothing to refresh, so this degrades to
    ``True`` (proceed locally, same convention as :func:`acquire_inflight`).
    """

    url = _database_url()
    if not url:
        return True
    psycopg_module = _import_psycopg()
    if psycopg_module is None:
        return True
    try:
        with _connect(psycopg_module, url) as conn:
            with conn.cursor() as cur:
                cur.execute(INFLIGHT_DDL)
                cur.execute(
                    "UPDATE harness_inflight SET expires_at = CURRENT_TIMESTAMP + %s * INTERVAL '1 second' "
                    "WHERE key = %s AND host = %s AND pid = %s",
                    (expires_in_sec, key, socket.gethostname(), os.getpid()),
                )
                still_owned = cur.rowcount > 0
                conn.commit()
                return still_owned
    except Exception as exc:  # noqa: BLE001
        _sanitized_warning("Remote inflight heartbeat failed", exc)
        return False


def inflight_lease_sec() -> float:
    """Short-lived lease duration for a fresh inflight claim (default 90s).

    Deliberately much shorter than the ~20 minute sum of step timeouts used
    previously: a crashed process/container now only blocks the key for one
    lease, not the whole run. A heartbeat thread (:func:`heartbeat_inflight`)
    keeps a still-alive holder's claim renewed for as long as it actually
    runs.
    """

    raw = os.environ.get("DARKFAC_HARNESS_INFLIGHT_LEASE_SEC", "").strip()
    if not raw:
        return _DEFAULT_INFLIGHT_LEASE_SEC
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_INFLIGHT_LEASE_SEC
    return value if value > 0 else _DEFAULT_INFLIGHT_LEASE_SEC


def heartbeat_inflight(
    key: str,
    *,
    stop_event: threading.Event,
    lease_sec: float | None = None,
) -> None:
    """Run in a daemon thread for as long as this host owns ``key``'s
    inflight claim, refreshing its lease roughly every ``lease_sec / 3`` so
    a long-running suite doesn't outlive its short lease. Stops as soon as
    ``stop_event`` is set (the caller's ``finally``) or ownership is lost.

    Any refresh failure is logged only once (not per-tick) so a flaky
    connection during a 15-minute run doesn't spam the harness console.
    """

    effective_lease = lease_sec if lease_sec is not None else inflight_lease_sec()
    interval = max(_MIN_HEARTBEAT_INTERVAL_SEC, effective_lease / 3)
    warned = False
    while not stop_event.wait(interval):
        try:
            still_owned = refresh_inflight(key, expires_in_sec=effective_lease)
        except Exception as exc:  # noqa: BLE001 - heartbeat must never crash the run
            if not warned:
                _sanitized_warning("Inflight heartbeat failed", exc)
                warned = True
            continue
        if not still_owned:
            if not warned:
                print(
                    f"[WARN] Inflight heartbeat lost ownership of {key[:12]}; "
                    "another host took over"
                )
                warned = True
            return


def poll_for_remote_verdict(key: str, *, deadline_sec: float) -> dict[str, Any] | None:
    """Poll every ~10s (bounded by ``deadline_sec`` and the wait env var) for
    a PASS verdict.

    Each iteration also checks the holder's inflight claim
    (:func:`get_inflight_claim`): once that claim is gone or expired *and*
    still no verdict has been stored, the holder is done -- either it
    finished with a failure (failures are never cached, so the only trace of
    a finished failed run is the holder deleting its inflight row) or it
    crashed. Either way, waiting any longer is pointless, so this returns
    ``None`` immediately instead of idling out the rest of ``deadline_sec``.
    """

    wait_cap = float(os.environ.get("DARKFAC_HARNESS_REMOTE_WAIT_SEC", str(_DEFAULT_REMOTE_WAIT_SEC)))
    effective_deadline = min(deadline_sec, wait_cap)
    start = time.monotonic()
    while time.monotonic() - start < effective_deadline:
        verdict = get_remote_verdict(key)
        if verdict is not None:
            return verdict
        if get_inflight_claim(key) is None:
            return None
        time.sleep(min(_INFLIGHT_POLL_SEC, max(0.0, effective_deadline - (time.monotonic() - start))))
    return None
