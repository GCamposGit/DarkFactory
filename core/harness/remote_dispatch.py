#!/usr/bin/env python3
"""Remote dispatch of the official validation harness to a primary test worker.

Client half of the async job protocol implemented by
``core.harness.remote_worker`` (see that module's ``JobManager``). Ships the
current commit to a dedicated, always-on test worker (Tailscale mesh) over a
``git bundle`` sent directly over HTTP -- no GitHub push involved -- so a
laptop stays free for interactive dev while a dedicated on-prem box (or the
VPS) runs the suite.

Topology and fallback rules (owner decision, not re-litigated here):

- ``DARKFAC_TEST_WORKERS`` -- ordered, comma-separated base URLs; the first
  reachable, non-self, non-busy worker wins.
- Worker offline -> next worker, or local immediately.
- Worker busy -> poll its ``/health`` for up to
  ``DARKFAC_REMOTE_BUSY_WAIT_SEC`` (default 300s), then move on.
- Nothing usable -> local, UNLESS ``--remote-required``/
  ``DARKFAC_REMOTE_HARNESS=required`` was set, in which case
  :class:`RemoteRequiredError` is raised (a ``RuntimeError``, so
  ``core.harness.runner.main`` reports it exactly like any other harness
  configuration error).

Remote is disabled automatically -- silently, never raising even under
``--remote-required`` -- when: ``CI`` is truthy; the runner is itself
executing inside a worker job (``DARKFAC_HARNESS_WORKER_JOB=1``, set by the
worker for the ``runner.py --local`` subprocess it launches); or a
candidate worker resolves to this same machine (self-dispatch loop guard).
``DARKFAC_REMOTE_HARNESS=off`` / ``--local`` also disable it, but as an
explicit user choice rather than a safety invariant.

Trust model: this module never forwards the worker's own supervisor
markers. It parses the worker's structured ``HarnessResult`` JSON, verifies
``candidate_sha``/``config_hash`` against the local values, and only then
emits its OWN ``[STEP_*]``/``[TEST_COUNT]``/``[HARNESS_RESULT]``/
``[HARNESS_PASS]``/``[HARNESS_FAIL]`` markers -- with ``executed_on`` set on
the structured result. Remote job log chunks are sanitized
(:func:`core.harness.markers.sanitize_child_output`) before being printed,
so a remote log can never inject a forged marker into the supervisor's own
output.
"""

from __future__ import annotations

import base64
import collections
import contextlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

from core.harness import cache as harness_cache
from core.harness.markers import (
    MARKER_HARNESS_FAIL,
    MARKER_HARNESS_PASS,
    MARKER_HARNESS_RESULT,
    MARKER_STEP_FAIL,
    MARKER_STEP_PASS,
    MARKER_STEP_START,
    MARKER_STEP_TIME,
    MARKER_TEST_COUNT,
    sanitize_child_output,
)
from core.harness.models import HarnessResult, HarnessStepConfig

# --- env vars / constants ---------------------------------------------------

ENV_TEST_WORKERS = "DARKFAC_TEST_WORKERS"
ENV_REMOTE_HARNESS = "DARKFAC_REMOTE_HARNESS"  # auto (default) | off | required
ENV_WORKER_TOKEN = "DARKFAC_WORKER_TOKEN"
ENV_BUSY_WAIT_SEC = "DARKFAC_REMOTE_BUSY_WAIT_SEC"
#: USR-141: ceiling (seconds without a successful job poll) after which a worker
#: that still answers ``/health`` is finally declared unavailable.
ENV_WORKER_SILENCE_FALLBACK_SEC = "DARKFAC_WORKER_SILENCE_FALLBACK_SEC"
#: USR-147: timeout (seconds) of the ``/health`` probe (default 2.0).
ENV_WORKER_HEALTH_TIMEOUT_SEC = "DARKFAC_WORKER_HEALTH_TIMEOUT_SEC"
ENV_WORKER_JOB = "DARKFAC_HARNESS_WORKER_JOB"
#: Test-only escape hatch: a loopback test dispatches to a worker running on
#: 127.0.0.1 on purpose, which the real self-dispatch guard would otherwise
#: (correctly) refuse. Never meant to be set outside a test process.
ENV_SELF_DISPATCH_TEST_OVERRIDE = "DARKFAC_REMOTE_DISPATCH_TEST_OVERRIDE"

DEFAULT_WORKERS = "http://100.78.181.90:8080"
DEFAULT_BUSY_WAIT_SEC = 300.0
HEALTH_PROBE_TIMEOUT_SEC = 2.0
POLL_INTERVAL_SEC = 2.0
#: A worker that stops answering GET /harness/jobs/{id} for longer than this
#: is probed on ``/health`` (USR-141): a worker that still answers ``/health``
#: is merely SLOW (e.g. a saturated CPU running ``pytest -n``) and keeps being
#: awaited up to ``worker_silence_ceiling_sec()``; one that does not is gone
#: and triggers a local fallback.
WORKER_SILENCE_FALLBACK_SEC = 60.0
#: Default ceiling for a slow-but-alive worker (``DARKFAC_WORKER_SILENCE_FALLBACK_SEC``).
DEFAULT_WORKER_SILENCE_CEILING_SEC = 180.0
#: ``/health`` probes of a silent worker use at least this timeout: the point
#: is to tell "slow" from "dead", so the probe must tolerate a busy host.
SLOW_WORKER_PROBE_TIMEOUT_SEC = 10.0
#: Minimum spacing between two ``/health`` probes of the same silent worker.
SLOW_WORKER_PROBE_INTERVAL_SEC = 10.0
SUBMIT_TIMEOUT_SEC = 30.0
POLL_HTTP_TIMEOUT_SEC = 10.0
#: USR-162: worker ``error_code`` values of a ``failed`` job that is NOT a test
#: verdict (the run never finished). They fall back like an unavailable worker.
INFRASTRUCTURE_ERROR_CODES = frozenset({"worker_restarted", "worker_internal_error", "worker_setup_failed"})
#: Lines of streamed job log kept as evidence when a job is given up on.
EVIDENCE_TAIL_LINES = 400
LOST_JOBS_SUBDIR = "remote_jobs"
MARKER_REMOTE_JOB_LOST = "[REMOTE_JOB_LOST]"


class RemoteRequiredError(RuntimeError):
    """``--remote-required``/``DARKFAC_REMOTE_HARNESS=required`` but no
    worker could run the job (never raised for the unconditional safety
    guards -- CI, worker-job re-entrancy -- which always fall back to local
    silently, by design)."""


class _WorkerUnavailable(Exception):
    """Internal signal: abandon this worker and try the next one (or fall
    back to local); NOT a definitive remote verdict.

    ``details`` carries the structured cause (USR-162): ``error_code``,
    ``job_id``, ``worker_url`` and whatever evidence was gathered, so the
    abandoned job is never reduced to an anonymous "stopped responding"."""

    def __init__(self, message: str = "", details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details: dict[str, Any] = dict(details or {})


class RemoteJobLost(_WorkerUnavailable):
    """A remote job that was dispatched but will never deliver a verdict
    (unknown to the worker after a restart, worker silent past the ceiling,
    credentials rejected, worker-side infrastructure failure). The caller
    falls back exactly as for any ``_WorkerUnavailable``; the evidence was
    already printed (``[REMOTE_JOB_LOST]``) and written to disk."""


# --- env helpers -------------------------------------------------------------


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def worker_urls() -> list[str]:
    raw = os.environ.get(ENV_TEST_WORKERS, DEFAULT_WORKERS)
    return [url.strip() for url in raw.split(",") if url.strip()]


def _effective_mode(*, remote_required: bool) -> str:
    if remote_required:
        return "required"
    raw = os.environ.get(ENV_REMOTE_HARNESS, "auto").strip().lower()
    return raw if raw in ("auto", "off", "required") else "auto"


def _unconditional_disable_reason() -> str | None:
    """Safety invariants that disable remote dispatch no matter what the
    caller asked for -- never raise :class:`RemoteRequiredError`."""

    if _env_truthy("CI"):
        return "CI environment"
    if _env_truthy(ENV_WORKER_JOB):
        return "already executing inside a worker job"
    return None


def _busy_wait_sec() -> float:
    raw = os.environ.get(ENV_BUSY_WAIT_SEC, "").strip()
    if not raw:
        return DEFAULT_BUSY_WAIT_SEC
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_BUSY_WAIT_SEC
    return value if value >= 0 else DEFAULT_BUSY_WAIT_SEC


def _positive_float_env(name: str, default: float) -> float:
    """Parse a strictly positive float env var; anything invalid -> ``default``."""

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return default
    return value


def health_probe_timeout_sec() -> float:
    """Timeout of the ``/health`` probe (``DARKFAC_WORKER_HEALTH_TIMEOUT_SEC``)."""

    return _positive_float_env(ENV_WORKER_HEALTH_TIMEOUT_SEC, HEALTH_PROBE_TIMEOUT_SEC)


def worker_silence_ceiling_sec() -> float:
    """Longest a still-healthy-but-silent worker is awaited (never below the
    plain silence threshold)."""

    configured = _positive_float_env(ENV_WORKER_SILENCE_FALLBACK_SEC, DEFAULT_WORKER_SILENCE_CEILING_SEC)
    return max(configured, WORKER_SILENCE_FALLBACK_SEC)


def _auth_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = dict(extra or {})
    token = os.environ.get(ENV_WORKER_TOKEN, "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# --- self-dispatch guard ------------------------------------------------------


def _local_addresses() -> set[str]:
    addrs = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
    with contextlib.suppress(OSError):
        addrs.add(socket.gethostname().lower())
    with contextlib.suppress(OSError):
        addrs.add(socket.getfqdn().lower())
    with contextlib.suppress(OSError):
        _, _, ip_list = socket.gethostbyname_ex(socket.gethostname())
        addrs.update(ip_list)
    with contextlib.suppress(OSError):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            addrs.add(probe.getsockname()[0])
    return addrs


def is_self_dispatch(base_url: str, health: dict[str, Any] | None) -> bool:
    """True when ``base_url`` (or the node it reports) is this same host."""

    if _env_truthy(ENV_SELF_DISPATCH_TEST_OVERRIDE):
        return False
    parsed = urllib.parse.urlparse(base_url if "://" in base_url else f"http://{base_url}")
    host = (parsed.hostname or "").lower()
    if host in _local_addresses():
        return True
    if health:
        reported_host = str(health.get("hostname") or "").strip().lower()
        if reported_host and reported_host == socket.gethostname().lower():
            return True
    return False


# --- HTTP plumbing (stdlib only -- consistent with core.harness.test_subagent) --


def probe_health(base_url: str, timeout: float | None = None) -> dict[str, Any] | None:
    if timeout is None:
        timeout = health_probe_timeout_sec()
    url = f"{base_url.rstrip('/')}/health"
    try:
        req = urllib.request.Request(url, headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _submit_job(base_url: str, bundle_path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    meta_b64 = base64.b64encode(json.dumps(metadata, separators=(",", ":")).encode("utf-8")).decode("ascii")
    data = bundle_path.read_bytes()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/harness/jobs",
        data=data,
        method="POST",
        headers=_auth_headers({"Content-Type": "application/octet-stream", "X-Job-Meta": meta_b64}),
    )
    try:
        with urllib.request.urlopen(req, timeout=SUBMIT_TIMEOUT_SEC) as resp:
            raw = resp.read()
            body = json.loads(raw.decode("utf-8")) if raw else {}
            return {"status_code": resp.status, "body": body}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"detail": raw}
        return {"status_code": exc.code, "body": body}


def _poll_job(base_url: str, job_id: str, offset: int) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/harness/jobs/{job_id}?offset={offset}"
    req = urllib.request.Request(url, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=POLL_HTTP_TIMEOUT_SEC) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _cancel_job(base_url: str, job_id: str) -> None:
    url = f"{base_url.rstrip('/')}/harness/jobs/{job_id}"
    req = urllib.request.Request(url, method="DELETE", headers=_auth_headers())
    with contextlib.suppress(Exception):
        urllib.request.urlopen(req, timeout=POLL_HTTP_TIMEOUT_SEC)


# --- bundle / git plumbing ----------------------------------------------------


def _ensure_clean_worktree(project_root: Path) -> None:
    proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none"],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"unable to verify candidate worktree cleanliness: {proc.stderr.strip()}")
    if proc.stdout.strip():
        raise RuntimeError("candidate worktree is dirty; commit or use a clean checkout")


def _local_candidate_sha(project_root: Path) -> str:
    _ensure_clean_worktree(project_root)
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    sha = proc.stdout.strip().lower()
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        raise RuntimeError(f"unable to resolve candidate SHA: {proc.stderr.strip()}")
    return sha


def _create_bundle(project_root: Path, *, base_shas: list[str]) -> Path:
    fd, tmp_name = tempfile.mkstemp(suffix=".bundle", prefix="darkfac-harness-")
    os.close(fd)
    tmp_path = Path(tmp_name)

    def _bundle(negatives: list[str]) -> subprocess.CompletedProcess[str]:
        cmd = ["git", "bundle", "create", str(tmp_path), "HEAD"]
        if negatives:
            cmd += ["--not", *negatives]
        return subprocess.run(
            cmd, cwd=project_root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )

    # The worker may advertise commits this checkout has never seen (e.g. an
    # unpushed local commit on the worker); `--not <unknown sha>` makes git
    # abort with "bad object", so only exclude commits that exist here.
    known_locally = [
        sha
        for sha in base_shas
        if subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"], cwd=project_root, capture_output=True, check=False
        ).returncode
        == 0
    ]
    proc = _bundle(known_locally)
    if proc.returncode != 0 and "empty bundle" in proc.stderr:
        # The worker already has HEAD (typically validating the same main it
        # has checked out) or a descendant of it. git refuses an empty bundle,
        # so ship just the HEAD commit: its parents are prerequisites the
        # worker is known to hold, and the job still gets a ref to fetch.
        proc = _bundle(["HEAD^@"])
    if proc.returncode != 0:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise RuntimeError(f"git bundle create failed: {proc.stderr.strip()}")
    return tmp_path


def _relative_config_path(config_path: Path, project_root: Path) -> str:
    try:
        return str(config_path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return str(config_path)


# --- job streaming -------------------------------------------------------------


_REMOTE_STEP_TIME_RE = re.compile(r"^\[CHILD_STEP_TIME\] (\S+) ([0-9]+(?:\.[0-9]+)?)s")
_HEALTH_EVIDENCE_KEYS = (
    "busy",
    "queue_length",
    "active_runs",
    "instance_id",
    "started_at",
    "git_sha",
    "hostname",
    "node_id",
    "status",
)


def _health_subset(health: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(health, dict):
        return None
    return {key: health[key] for key in _HEALTH_EVIDENCE_KEYS if key in health}


def _record_lost_job(details: dict[str, Any], log_tail: str) -> None:
    """Print the structured ``[REMOTE_JOB_LOST]`` line and keep the evidence
    on disk (``<harness state>/remote_jobs/<job_id>.json``). Never raises."""

    with contextlib.suppress(Exception):
        print(f"{MARKER_REMOTE_JOB_LOST} {sanitize_child_output(json.dumps(details, sort_keys=True, default=str))}")
    with contextlib.suppress(Exception):
        directory = harness_cache.state_dir() / LOST_JOBS_SUBDIR
        directory.mkdir(parents=True, exist_ok=True)
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(details.get("job_id") or "unknown"))[:80]
        record = {"details": details, "log_tail": log_tail, "recorded_at": time.time()}
        tmp_path = directory / f"{safe_id}.json.tmp"
        tmp_path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        os.replace(tmp_path, directory / f"{safe_id}.json")


def _hard_poll_failure(exc: Exception) -> tuple[str, str] | None:
    """Classify a failed job poll. Returns ``(error_code, message)`` when the
    answer is DEFINITIVE (retrying cannot help), ``None`` for the transient
    kind (timeout, refused connection, 5xx, a bare 404 from a proxy) that the
    USR-141 silence logic handles."""

    if not isinstance(exc, urllib.error.HTTPError):
        return None
    if exc.code in (401, 403):
        return (
            "worker_rejected_credentials",
            f"worker rejected the job poll with HTTP {exc.code} (check DARKFAC_WORKER_TOKEN)",
        )
    if exc.code == 404:
        body: Any = {}
        with contextlib.suppress(Exception):
            body = json.loads(exc.read().decode("utf-8", errors="replace"))
        if isinstance(body, dict) and (body.get("error_code") == "job_not_found" or body.get("detail") == "job not found"):
            return (
                "job_lost_on_worker",
                "the worker has no record of this job (HTTP 404 job not found)",
            )
    return None


def _recover_terminal_state(base_url: str, job_id: str, offset: int) -> dict[str, Any] | None:
    """One last read of the job before giving up: a worker that was merely
    slow may hold the terminal verdict (and the rest of the log), or be
    answering again."""

    try:
        payload = _poll_job(base_url, job_id, offset)
    except Exception:  # noqa: BLE001 - recovery is best effort
        return None
    return payload if isinstance(payload, dict) and payload.get("status") else None


def _stream_job(
    base_url: str,
    job_id: str,
    *,
    deadline: float,
    submitted_instance_id: str | None = None,
) -> dict[str, Any]:
    """Poll until the job reaches a terminal status; stream sanitized log
    chunks prefixed with the worker's URL as they arrive.

    Never gives up anonymously (USR-162): every abandonment raises
    :class:`RemoteJobLost` carrying ``job_id``, the cause and the evidence
    gathered, after one last attempt to read a terminal verdict."""

    offset = 0
    last_ok = time.monotonic()
    last_probe = float("-inf")
    announced_slow = False
    last_status: str | None = None
    step_times: dict[str, str] = {}
    log_tail: collections.deque[str] = collections.deque(maxlen=EVIDENCE_TAIL_LINES)

    def _give_up(error_code: str, message: str, *, health: dict[str, Any] | None = None) -> RemoteJobLost:
        if health is None:
            health = probe_health(base_url)
        current_instance = health.get("instance_id") if isinstance(health, dict) else None
        restarted: bool | None = None
        if submitted_instance_id and current_instance:
            restarted = submitted_instance_id != current_instance
        full_message = f"job {job_id} on {base_url}: {message}"
        if restarted:
            full_message += (
                f"; the worker process restarted after the job was submitted "
                f"(instance {submitted_instance_id} -> {current_instance})"
            )
        details = {
            "error_code": error_code,
            "job_id": job_id,
            "worker_url": base_url,
            "message": message,
            "last_status": last_status,
            "last_offset": offset,
            "silent_for_sec": round(time.monotonic() - last_ok, 1),
            "submitted_instance_id": submitted_instance_id,
            "worker_restarted_since_submit": restarted,
            "health": _health_subset(health),
        }
        _record_lost_job(details, "\n".join(log_tail))
        return RemoteJobLost(full_message, details)

    while True:
        now = time.monotonic()
        if now > deadline:
            _cancel_job(base_url, job_id)
            raise _give_up("client_deadline_exceeded", "client-side deadline exceeded")
        try:
            payload = _poll_job(base_url, job_id, offset)
            last_ok = time.monotonic()
            announced_slow = False
        except Exception as exc:  # noqa: BLE001
            hard = _hard_poll_failure(exc)
            if hard is not None:
                raise _give_up(*hard) from exc
            silent_for = time.monotonic() - last_ok
            recovered: dict[str, Any] | None = None
            if silent_for > WORKER_SILENCE_FALLBACK_SEC:
                # USR-141: distinguish a SLOW worker from a DEAD one before
                # abandoning the run. A saturated host can miss job polls
                # while its HTTP server still answers /health.
                ceiling = worker_silence_ceiling_sec()
                if silent_for > ceiling:
                    recovered = _recover_terminal_state(base_url, job_id, offset)
                    if recovered is None:
                        raise _give_up("worker_silent", f"worker stopped responding for >{ceiling:.0f}s mid-run") from exc
                    last_ok = time.monotonic()
                elif time.monotonic() - last_probe >= SLOW_WORKER_PROBE_INTERVAL_SEC:
                    last_probe = time.monotonic()
                    alive = probe_health(base_url, timeout=max(health_probe_timeout_sec(), SLOW_WORKER_PROBE_TIMEOUT_SEC))
                    if alive is None:
                        raise _give_up(
                            "worker_unreachable",
                            f"worker stopped responding for >{silent_for:.0f}s mid-run and /health is unreachable",
                            health={},
                        ) from exc
                    if not announced_slow:
                        announced_slow = True
                        print(
                            f"[REMOTE {base_url}] job polls silent for {silent_for:.0f}s but /health answers "
                            f"(busy={alive.get('busy')}); worker is slow, not dead - waiting up to {ceiling:.0f}s"
                        )
            if recovered is None:
                time.sleep(POLL_INTERVAL_SEC)
                continue
            payload = recovered

        chunk = payload.get("log_chunk") or ""
        if chunk:
            for line in sanitize_child_output(chunk).splitlines():
                print(f"[REMOTE {base_url}] {line}")
                log_tail.append(line)
                timing = _REMOTE_STEP_TIME_RE.match(line)
                if timing:
                    step_times[timing.group(1)] = timing.group(2)
        offset = int(payload.get("next_offset", offset))
        last_status = payload.get("status")
        if last_status in ("done", "failed", "cancelled"):
            # Display-only: remote timings never feed the verdict.
            return {**payload, "_step_times": step_times, "_log_tail": "\n".join(log_tail)}
        time.sleep(POLL_INTERVAL_SEC)


# --- result verification / marker emission -------------------------------------


def _emit_local_fail(message: str) -> None:
    print(f"[ERROR] {message}")
    print(f"{MARKER_TEST_COUNT} count=0")
    print(MARKER_HARNESS_FAIL)


def _finalize_remote_result(
    payload: dict[str, Any],
    *,
    candidate_sha: str,
    config_hash: str,
    base_url: str,
    health: dict[str, Any],
    job_id: str | None = None,
) -> tuple[bool, HarnessResult | None]:
    status = payload.get("status")
    if status not in ("done", "failed"):
        raise _WorkerUnavailable(f"unexpected terminal job status: {status!r}")

    error_code = payload.get("error_code")
    if status == "failed" and error_code in INFRASTRUCTURE_ERROR_CODES:
        # The worker closed the job without a verdict (restart, crash, setup
        # failure): that is not a test failure and must not be reported as one.
        worker_error = str(payload.get("error") or error_code)
        details = {
            "error_code": error_code,
            "job_id": job_id or payload.get("job_id"),
            "worker_url": base_url,
            "message": worker_error,
            "last_status": status,
            "worker_instance_id": payload.get("worker_instance_id"),
            "health": _health_subset(health),
        }
        _record_lost_job(details, str(payload.get("_log_tail") or ""))
        raise RemoteJobLost(
            f"job {details['job_id']} on {base_url} ended without a verdict ({error_code}): {worker_error}", details
        )

    raw_result = payload.get("result_json")
    if not raw_result:
        _emit_local_fail(f"remote worker {base_url} finished without structured HARNESS_RESULT evidence")
        return False, None

    try:
        remote_result = HarnessResult.model_validate(raw_result)
    except ValidationError as exc:
        _emit_local_fail(f"remote worker {base_url} returned an invalid HarnessResult: {exc}")
        return False, None

    if remote_result.candidate_sha != candidate_sha:
        _emit_local_fail(
            f"remote candidate_sha {remote_result.candidate_sha[:12]} != local HEAD "
            f"{candidate_sha[:12]}; refusing to trust evidence from {base_url}"
        )
        return False, None

    if remote_result.config_hash != config_hash:
        _emit_local_fail(
            f"remote config_hash {remote_result.config_hash[:12]} != local {config_hash[:12]}; "
            f"refusing to trust evidence from {base_url}"
        )
        return False, None

    executed_on = {
        "host": str(health.get("hostname") or base_url),
        "url": base_url,
        "mode": "remote",
        "platform_family": health.get("platform_family"),
    }
    final_result = remote_result.model_copy(update={"executed_on": executed_on})

    step_times = payload.get("_step_times") or {}
    for step_name in final_result.required_steps:
        print(f"{MARKER_STEP_START} {step_name}")
        print(f"{MARKER_STEP_PASS if step_name in final_result.passed_steps else MARKER_STEP_FAIL} {step_name}")
        print(f"{MARKER_STEP_TIME} {step_name} {step_times.get(step_name, '0.0')}s (remote: {base_url})")

    print(f"{MARKER_TEST_COUNT} count={final_result.discovered_count}")
    print(f"[REMOTE] executed on {executed_on['host']} ({base_url})")
    print(f"{MARKER_HARNESS_RESULT} {final_result.model_dump_json()}")

    success = (
        not final_result.failed_steps
        and len(final_result.started_steps) == len(final_result.required_steps)
        and final_result.discovered_count > 0
        and final_result.passed_count > 0
    )
    print(MARKER_HARNESS_PASS if success else MARKER_HARNESS_FAIL)
    return success, (final_result if success else None)


# --- top-level orchestration ---------------------------------------------------


def _dispatch_one_job(
    base_url: str,
    *,
    health: dict[str, Any],
    project_root: Path,
    candidate_sha: str,
    tree_hash: str | None,
    config_hash: str,
    quick: bool,
    include_holdout: bool,
    config_path: Path,
    total_timeout_sec: float,
) -> tuple[bool, HarnessResult | None] | None:
    known_shas = [str(sha) for sha in (health.get("known_shas") or []) if sha]
    try:
        bundle_path = _create_bundle(project_root, base_shas=known_shas)
    except RuntimeError as exc:
        print(f"[REMOTE] {base_url}: failed to create bundle ({exc}); skipping")
        return None

    metadata = {
        "candidate_sha": candidate_sha,
        "tree_sha": tree_hash or "",
        "quick": quick,
        "include_holdout": include_holdout,
        "config_path": _relative_config_path(config_path, project_root),
        "requesting_host": socket.gethostname(),
        "config_hash": config_hash,
    }

    try:
        submit_response = _submit_job(base_url, bundle_path, metadata)
        if submit_response["status_code"] == 409 and submit_response["body"].get("missing_prerequisites"):
            with contextlib.suppress(OSError):
                bundle_path.unlink()
            bundle_path = _create_bundle(project_root, base_shas=[])
            print(f"[REMOTE] {base_url}: worker missing prerequisites; retrying with a full bundle")
            submit_response = _submit_job(base_url, bundle_path, metadata)
    finally:
        with contextlib.suppress(OSError):
            bundle_path.unlink()

    if submit_response["status_code"] != 200:
        print(f"[REMOTE] {base_url}: job submission failed ({submit_response['status_code']}: {submit_response['body']})")
        return None

    job_id = submit_response["body"].get("job_id")
    if not job_id:
        print(f"[REMOTE] {base_url}: submission response missing job_id")
        return None

    print(f"[REMOTE] dispatched suite to {base_url} (job {job_id})")
    deadline = time.monotonic() + total_timeout_sec
    submitted_instance_id = submit_response["body"].get("worker_instance_id") or health.get("instance_id")
    try:
        payload = _stream_job(base_url, job_id, deadline=deadline, submitted_instance_id=submitted_instance_id)
    except KeyboardInterrupt:
        print(f"[REMOTE] interrupted; cancelling job {job_id} on {base_url}")
        _cancel_job(base_url, job_id)
        raise

    return _finalize_remote_result(
        payload,
        candidate_sha=candidate_sha,
        config_hash=config_hash,
        base_url=base_url,
        health=health,
        job_id=job_id,
    )


def maybe_dispatch_remote(
    *,
    steps: list[HarnessStepConfig],
    config_hash: str,
    quick: bool,
    include_holdout: bool,
    config_path: Path,
    project_root: Path,
    cache_lookup_enabled: bool,
    local_only: bool,
    remote_required: bool,
    emit_cache_hit: Callable[..., bool],
    notify_hub_on_pass: Callable[[], None],
) -> bool | None:
    """Try to run the official suite on a remote test worker.

    Returns ``True``/``False`` when the run was resolved remotely (markers
    already emitted -- caller returns this value directly), or ``None`` when
    remote dispatch did not happen at all and the caller should proceed with
    its existing local path unchanged.
    """

    if local_only:
        return None

    mode = _effective_mode(remote_required=remote_required)
    if mode == "off":
        return None

    if _unconditional_disable_reason() is not None:
        return None

    urls = worker_urls()
    if not urls:
        if remote_required:
            raise RemoteRequiredError("--remote-required: DARKFAC_TEST_WORKERS is empty")
        return None

    tried_any_reachable = False

    for base_url in urls:
        health = probe_health(base_url)
        if health is None:
            print(f"[REMOTE] {base_url} unreachable (worker offline or port blocked)")
            continue
        if is_self_dispatch(base_url, health):
            continue
        tried_any_reachable = True

        if health.get("busy"):
            worker_deadline = time.monotonic() + _busy_wait_sec()
            while health is not None and health.get("busy"):
                if time.monotonic() > worker_deadline:
                    health = None
                    break
                time.sleep(min(5.0, max(0.5, worker_deadline - time.monotonic())))
                health = probe_health(base_url)
                if health is not None and is_self_dispatch(base_url, health):
                    health = None
            if health is None:
                print(f"[REMOTE] {base_url} still busy after {_busy_wait_sec():.0f}s (or vanished)")
                continue  # next worker / local

        worker_platform = str(health.get("platform_family") or "windows")
        worker_python = str(health.get("python_version") or "") or None
        tree_hash = harness_cache.tree_sha(project_root)

        worker_key: str | None = None
        if cache_lookup_enabled and tree_hash:
            worker_key = harness_cache.compute_cache_key(
                tree_hash=tree_hash,
                config_hash=config_hash,
                step_names=[step.name for step in steps],
                quick=quick,
                include_holdout=include_holdout,
                platform_family_override=worker_platform,
                python_version_override=worker_python,
            )
            hit = harness_cache.get_verdict(worker_key)
            if hit is not None:
                return emit_cache_hit(hit, config_path=config_path)

        try:
            candidate_sha = _local_candidate_sha(project_root)
        except RuntimeError:
            return None  # let the existing local path produce the canonical error

        total_timeout_sec = float(sum(step.timeout_sec for step in steps)) + 120.0
        try:
            outcome = _dispatch_one_job(
                base_url,
                health=health,
                project_root=project_root,
                candidate_sha=candidate_sha,
                tree_hash=tree_hash,
                config_hash=config_hash,
                quick=quick,
                include_holdout=include_holdout,
                config_path=config_path,
                total_timeout_sec=total_timeout_sec,
            )
        except _WorkerUnavailable as exc:
            print(f"[REMOTE] {base_url} unavailable ({exc}); trying next option")
            continue

        if outcome is None:
            continue

        success, result = outcome
        if success and result is not None:
            if worker_key is not None and tree_hash:
                record = harness_cache.build_record(
                    tree_hash=tree_hash,
                    candidate_sha=result.candidate_sha,
                    config_hash=config_hash,
                    result=result.model_dump(mode="json"),
                )
                record["host"] = str(health.get("hostname") or base_url)
                record["os_family"] = worker_platform
                if worker_python:
                    record["py_version"] = worker_python
                harness_cache.store_verdict(worker_key, record)
            notify_hub_on_pass()
        return success

    if remote_required:
        reason = "no reachable, non-self worker" if not tried_any_reachable else "every worker busy/unavailable/failed"
        raise RemoteRequiredError(f"--remote-required: {reason} among {urls}")
    # Never fall back silently: a green local run must not hide a dead worker.
    print("[REMOTE] no remote worker used; running the suite locally on this host")
    return None
