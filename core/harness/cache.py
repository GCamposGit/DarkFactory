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
CPU on a duplicate run.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from core.harness.suite_lock import state_dir

_CONNECT_TIMEOUT_SEC = 5
_DEFAULT_REMOTE_WAIT_SEC = 900
_INFLIGHT_POLL_SEC = 10


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


def poll_for_remote_verdict(key: str, *, deadline_sec: float) -> dict[str, Any] | None:
    """Poll every ~10s (bounded by ``deadline_sec`` and the wait env var) for a PASS verdict."""

    wait_cap = float(os.environ.get("DARKFAC_HARNESS_REMOTE_WAIT_SEC", str(_DEFAULT_REMOTE_WAIT_SEC)))
    effective_deadline = min(deadline_sec, wait_cap)
    start = time.monotonic()
    while time.monotonic() - start < effective_deadline:
        verdict = get_remote_verdict(key)
        if verdict is not None:
            return verdict
        time.sleep(min(_INFLIGHT_POLL_SEC, max(0.0, effective_deadline - (time.monotonic() - start))))
    return None
