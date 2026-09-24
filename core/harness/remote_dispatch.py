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
#: (network blip vs. genuinely gone) triggers a local fallback.
WORKER_SILENCE_FALLBACK_SEC = 60.0
SUBMIT_TIMEOUT_SEC = 30.0
POLL_HTTP_TIMEOUT_SEC = 10.0


class RemoteRequiredError(RuntimeError):
    """``--remote-required``/``DARKFAC_REMOTE_HARNESS=required`` but no
    worker could run the job (never raised for the unconditional safety
    guards -- CI, worker-job re-entrancy -- which always fall back to local
    silently, by design)."""


class _WorkerUnavailable(Exception):
    """Internal signal: abandon this worker and try the next one (or fall
    back to local); NOT a definitive remote verdict."""


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


def probe_health(base_url: str, timeout: float = HEALTH_PROBE_TIMEOUT_SEC) -> dict[str, Any] | None:
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
    cmd = ["git", "bundle", "create", str(tmp_path), "HEAD"]
    if base_shas:
        cmd += ["--not", *base_shas]
    proc = subprocess.run(
        cmd, cwd=project_root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
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


def _stream_job(base_url: str, job_id: str, *, deadline: float) -> dict[str, Any]:
    """Poll until the job reaches a terminal status; stream sanitized log
    chunks prefixed with the worker's URL as they arrive."""

    offset = 0
    last_ok = time.monotonic()
    while True:
        now = time.monotonic()
        if now > deadline:
            _cancel_job(base_url, job_id)
            raise _WorkerUnavailable("client-side deadline exceeded")
        try:
            payload = _poll_job(base_url, job_id, offset)
            last_ok = time.monotonic()
        except Exception:
            if time.monotonic() - last_ok > WORKER_SILENCE_FALLBACK_SEC:
                raise _WorkerUnavailable("worker stopped responding for >60s mid-run")
            time.sleep(POLL_INTERVAL_SEC)
            continue

        chunk = payload.get("log_chunk") or ""
        if chunk:
            for line in sanitize_child_output(chunk).splitlines():
                print(f"[REMOTE {base_url}] {line}")
        offset = int(payload.get("next_offset", offset))
        status = payload.get("status")
        if status in ("done", "failed", "cancelled"):
            return payload
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
) -> tuple[bool, HarnessResult | None]:
    status = payload.get("status")
    if status not in ("done", "failed"):
        raise _WorkerUnavailable(f"unexpected terminal job status: {status!r}")

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

    for step_name in final_result.required_steps:
        print(f"{MARKER_STEP_START} {step_name}")
        print(f"{MARKER_STEP_PASS if step_name in final_result.passed_steps else MARKER_STEP_FAIL} {step_name}")
        print(f"{MARKER_STEP_TIME} {step_name} 0.0s (remote: {base_url})")

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
    try:
        payload = _stream_job(base_url, job_id, deadline=deadline)
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
                continue  # still busy (or vanished) after waiting -- next worker / local

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
    return None
