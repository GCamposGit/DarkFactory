#!/usr/bin/env python3
"""Cross-platform validation runner that emits commit-bound evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from pydantic import ValidationError

IMPORT_ROOT = Path(__file__).resolve().parents[2]
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

from core.paths import project_root

PROJECT_ROOT = project_root()

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.harness import cache as harness_cache
from core.harness import suite_lock as harness_suite_lock
from core.harness.markers import (
    MARKER_HARNESS_FAIL,
    MARKER_HARNESS_PASS,
    MARKER_HARNESS_RESULT,
    MARKER_STEP_FAIL,
    MARKER_STEP_PASS,
    MARKER_STEP_START,
    MARKER_STEP_TIME,
    MARKER_TEST_COUNT,
    SUPERVISOR_MARKERS,
)
from core.harness.models import HarnessConfig, HarnessResult, HarnessStepConfig


def _notify_hub_on_pass() -> None:
    """
    Fire-and-forget: POST /api/benchmarks/refresh after HARNESS_PASS.

    - Uses only stdlib (urllib) — no external dependencies.
    - Reads DARKHUB_URL env var; defaults to "https://darkhub.ggcampos.com".
    - Also attempts localhost:8000 if DARKHUB_URL is not explicitly set to local,
      ensuring both production hub and local dev hub receive the refresh signal.
    - Timeout: 4 s. Any failure is swallowed silently — never blocks or alters the
      harness exit code. This is a best-effort notification, not a gate.
    """
    import urllib.request

    primary_url = os.environ.get("DARKHUB_URL", "https://darkhub.ggcampos.com").rstrip("/")
    target_urls = [primary_url]
    if "localhost" not in primary_url and "127.0.0.1" not in primary_url:
        target_urls.append("http://localhost:8000")

    for hub_url in target_urls:
        try:
            req = urllib.request.Request(
                f"{hub_url}/api/benchmarks/refresh",
                data=b"",
                method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "DarkFactory-Harness/1.0"},
            )
            with urllib.request.urlopen(req, timeout=4) as resp:
                print(f"[HUB] Benchmark refresh triggered → {hub_url} ({resp.status})")
        except Exception:
            pass  # Hub unreachable or offline — that's fine, fail-closed isolation preserved



@dataclass(frozen=True)
class StepExecution:
    name: str
    returncode: int
    discovered_count: int
    passed_count: int
    skipped_count: int

    @property
    def passed(self) -> bool:
        return self.returncode == 0


def load_config(config_path: Path) -> tuple[HarnessConfig, str]:
    """Load a config exactly as supplied; missing or invalid configs fail closed."""
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ValueError(f"Invalid harness config {config_path}: {exc}") from exc
    try:
        config = HarnessConfig.model_validate_json(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid harness config {config_path}: {exc}") from exc
    return config, hashlib.sha256(raw).hexdigest()


def sanitize_child_output(output: str) -> str:
    """Prevent a subprocess from emitting markers owned by the supervisor."""
    sanitized = output
    for marker in SUPERVISOR_MARKERS:
        sanitized = sanitized.replace(marker, marker.replace("[", "[CHILD_", 1))
    return sanitized


def resolve_command(command: str) -> list[str]:
    """Resolve an unqualified Python command to the runner's active interpreter."""

    arguments = shlex.split(command, posix=os.name != "nt")
    if os.name == "nt":
        # shlex.split(posix=False) keeps the surrounding quote characters
        # (needed on Windows to preserve quoted whitespace as one token, e.g.
        # `-m "not serial"`), unlike posix mode which strips them. Strip them
        # here so both platforms hand subprocess.run an identical, unquoted
        # argv -- otherwise the literal quotes end up inside e.g. a pytest
        # `-m` marker expression and silently select the wrong tests.
        arguments = [
            token[1:-1] if len(token) >= 2 and token[0] == token[-1] == '"' else token
            for token in arguments
        ]
    if arguments and arguments[0].casefold() in {"python", "python.exe"}:
        arguments[0] = sys.executable
    return arguments


_XDIST_FLAG_TOKENS = ("-n", "--dist", "--numprocesses")


def _xdist_available() -> bool:
    return importlib.util.find_spec("xdist") is not None


def _pytest_timeout_available() -> bool:
    return importlib.util.find_spec("pytest_timeout") is not None


def strip_xdist_args_if_unavailable(command: list[str]) -> list[str]:
    """Degrade gracefully: drop ``-n``/``--dist`` when pytest-xdist isn't installed.

    Without the plugin those flags are a hard pytest usage error (exit code
    4), not a soft warning, so the runner must remove them itself and fall
    back to a correct (if slower) serial run rather than fail closed on a
    missing optional dependency.
    """

    if _xdist_available():
        return command
    has_xdist_flag = any(
        token in _XDIST_FLAG_TOKENS or token.startswith(tuple(f"{flag}=" for flag in _XDIST_FLAG_TOKENS))
        for token in command
    )
    if not has_xdist_flag:
        return command
    print(
        "[WARN] pytest-xdist is not installed; stripping -n/--dist and running "
        "tests serially. Install with: python -m pip install -r requirements.txt"
    )
    stripped: list[str] = []
    skip_next = False
    for token in command:
        if skip_next:
            skip_next = False
            continue
        if token in _XDIST_FLAG_TOKENS:
            skip_next = True
            continue
        if token.startswith(tuple(f"{flag}=" for flag in _XDIST_FLAG_TOKENS)):
            continue
        stripped.append(token)
    return stripped


def _pytest_counts(output: str) -> tuple[int, int, int]:
    """Discovered/passed/skipped counts, robust to ``-q`` and xdist output.

    ``-q`` never prints "collected N items"; xdist (verbose only) prints
    "N workers [M items]" instead. When neither header is present the
    discovered count falls back to the sum of every outcome bucket in the
    final summary line, so a genuinely empty run still reports zero.
    """

    workers_items = re.search(r"\b\d+\s+workers?\s*\[(\d+)\s+items?\]", output)
    collected = re.search(r"collected\s+(\d+)\s+items?", output)
    passed = re.findall(r"(?:^|\s)(\d+)\s+passed(?:,|\s|$)", output)
    skipped = re.findall(r"(?:^|\s)(\d+)\s+skipped(?:,|\s|$)", output)
    failed = re.findall(r"(?:^|\s)(\d+)\s+failed(?:,|\s|$)", output)
    errors = re.findall(r"(?:^|\s)(\d+)\s+errors?(?:,|\s|$)", output)
    xfailed = re.findall(r"(?:^|\s)(\d+)\s+xfailed(?:,|\s|$)", output)
    xpassed = re.findall(r"(?:^|\s)(\d+)\s+xpassed(?:,|\s|$)", output)

    passed_count = int(passed[-1]) if passed else 0
    skipped_count = int(skipped[-1]) if skipped else 0
    failed_count = int(failed[-1]) if failed else 0
    error_count = int(errors[-1]) if errors else 0
    xfailed_count = int(xfailed[-1]) if xfailed else 0
    xpassed_count = int(xpassed[-1]) if xpassed else 0

    if workers_items:
        discovered_count = int(workers_items.group(1))
    elif collected:
        discovered_count = int(collected.group(1))
    else:
        discovered_count = (
            passed_count + skipped_count + failed_count + error_count + xfailed_count + xpassed_count
        )

    return discovered_count, passed_count, skipped_count


def _is_test_step(step: HarnessStepConfig) -> bool:
    return step.kind == "test" or (step.kind is None and "pytest" in step.cmd.casefold())


def run_step(step: HarnessStepConfig) -> StepExecution:
    print(f"{MARKER_STEP_START} {step.name}")
    started_at = time.perf_counter()

    def _elapsed() -> float:
        return time.perf_counter() - started_at

    try:
        command = resolve_command(step.cmd)
        if not command:
            raise ValueError("empty command")
        command = strip_xdist_args_if_unavailable(command)
        # DARKFAC_SUITE_LOCK_HELD is set by the caller (run_with_cache) while a
        # test step is running so a nested pytest invocation (this subprocess)
        # never re-acquires the machine-wide suite lock and deadlocks against
        # its own parent.
        process = subprocess.run(
            command,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=PROJECT_ROOT,
            timeout=step.timeout_sec,
            check=False,
        )
        output = process.stdout or ""
        if output:
            print(sanitize_child_output(output), end="" if output.endswith("\n") else "\n")
        discovered, passed, skipped = _pytest_counts(output) if _is_test_step(step) else (0, 0, 0)
        execution = StepExecution(step.name, process.returncode, discovered, passed, skipped)
        marker = MARKER_STEP_PASS if execution.passed else MARKER_STEP_FAIL
        suffix = "" if execution.passed else f" (exit code: {process.returncode})"
        print(f"{marker} {step.name}{suffix}")
        print(f"{MARKER_STEP_TIME} {step.name} {_elapsed():.1f}s")
        return execution
    except subprocess.TimeoutExpired:
        print(f"{MARKER_STEP_FAIL} {step.name} (timeout exceeded)")
        print(f"{MARKER_STEP_TIME} {step.name} {_elapsed():.1f}s")
        return StepExecution(step.name, 124, 0, 0, 0)
    except (OSError, ValueError) as exc:
        print(f"{MARKER_STEP_FAIL} {step.name} (error: {exc})")
        print(f"{MARKER_STEP_TIME} {step.name} {_elapsed():.1f}s")
        return StepExecution(step.name, 127, 0, 0, 0)


def _candidate_sha() -> str:
    _ensure_clean_worktree()
    process = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    sha = process.stdout.strip().lower()
    if process.returncode != 0 or not re.fullmatch(r"[0-9a-f]{7,64}", sha):
        raise RuntimeError(f"Unable to bind harness result to candidate SHA: {process.stderr.strip()}")
    return sha


def _ensure_clean_worktree() -> None:
    """Reject evidence that cannot be bound to the exact tested checkout.

    ``git rev-parse HEAD`` identifies only the committed tree.  Running the
    harness from a dirty checkout would therefore let a green result claim
    the commit while tests actually exercised local edits or untracked files.
    Porcelain status is used so both tracked changes and untracked paths are
    covered, and any inability to query Git fails closed.
    """

    command = [
        "git",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ]
    try:
        process = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(
            f"Unable to verify candidate worktree cleanliness: {exc}"
        ) from exc

    if process.returncode != 0:
        detail = process.stderr.strip() or process.stdout.strip() or "unknown git error"
        raise RuntimeError(
            f"Unable to verify candidate worktree cleanliness: {detail}"
        )

    if process.stdout.strip():
        raise RuntimeError(
            "Candidate worktree is dirty; commit or use a clean checkout before "
            "running the official harness"
        )


def _selected_steps(
    config: HarnessConfig, *, quick: bool, include_holdout: bool
) -> list[HarnessStepConfig]:
    return [
        step
        for step in config.steps
        if (not quick or step.quick) and (include_holdout or not step.holdout)
    ]


def execute(
    config: HarnessConfig,
    *,
    config_hash: str,
    quick: bool,
    include_holdout: bool,
    config_path: Path,
    on_result: Callable[[HarnessResult], None] | None = None,
) -> bool:
    try:
        _ensure_clean_worktree()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        print(f"{MARKER_TEST_COUNT} count=0")
        print(MARKER_HARNESS_FAIL)
        return False

    steps = _selected_steps(config, quick=quick, include_holdout=include_holdout)
    if not steps:
        print("[ERROR] Zero checks selected. Empty is not a pass.")
        print(f"{MARKER_TEST_COUNT} count=0")
        print(MARKER_HARNESS_FAIL)
        return False

    try:
        candidate_sha = _candidate_sha()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        print(f"{MARKER_TEST_COUNT} count=0")
        print(MARKER_HARNESS_FAIL)
        return False

    executions: list[StepExecution] = []
    for step in steps:
        execution = run_step(step)
        executions.append(execution)
        if not execution.passed:
            break

    discovered_count = sum(item.discovered_count for item in executions)
    passed_count = sum(item.passed_count for item in executions)
    skipped_count = sum(item.skipped_count for item in executions)
    passed_steps = [item.name for item in executions if item.passed]
    failed_steps = [item.name for item in executions if not item.passed]
    print(f"{MARKER_TEST_COUNT} count={discovered_count}")

    try:
        final_candidate_sha = _candidate_sha()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        print(MARKER_HARNESS_FAIL)
        return False
    if final_candidate_sha != candidate_sha:
        print(
            "[ERROR] Candidate HEAD changed during harness execution; refusing "
            "to emit evidence"
        )
        print(MARKER_HARNESS_FAIL)
        return False

    result = HarnessResult(
        candidate_sha=candidate_sha,
        config_hash=config_hash,
        required_steps=[step.name for step in steps],
        started_steps=[item.name for item in executions],
        passed_steps=passed_steps,
        failed_steps=failed_steps,
        discovered_count=discovered_count,
        passed_count=passed_count,
        skipped_count=skipped_count,
        exit_codes={item.name: item.returncode for item in executions},
        artifact_refs=[str(config_path)],
    )
    if on_result is not None:
        on_result(result)
    print(f"{MARKER_HARNESS_RESULT} {result.model_dump_json()}")

    success = (
        not failed_steps
        and len(executions) == len(steps)
        and discovered_count > 0
        and passed_count > 0
    )
    print(MARKER_HARNESS_PASS if success else MARKER_HARNESS_FAIL)
    if success:
        _notify_hub_on_pass()
    return success



def _emit_cache_hit(record: dict, *, config_path: Path) -> bool:
    """Replay a cached PASS verdict for the current candidate without re-running steps.

    Emits the full normal marker contract (STEP_START/PASS per step,
    TEST_COUNT, HARNESS_RESULT, HARNESS_PASS) so every existing consumer of
    those markers keeps working; ``reused_from`` on the structured result and
    a plain "[CACHE_HIT] ..." line name the origin (host, original
    candidate_sha, age) for humans reading the log.
    """

    try:
        current_sha = _candidate_sha()
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        print(f"{MARKER_TEST_COUNT} count=0")
        print(MARKER_HARNESS_FAIL)
        return False

    cached_result: dict = record["result"]
    required_steps: list[str] = list(cached_result["required_steps"])
    for step_name in required_steps:
        print(f"{MARKER_STEP_START} {step_name}")
        print(f"{MARKER_STEP_PASS} {step_name}")
        print(f"{MARKER_STEP_TIME} {step_name} 0.0s (cache hit)")

    discovered_count = int(cached_result["discovered_count"])
    print(f"{MARKER_TEST_COUNT} count={discovered_count}")

    age_sec = max(0.0, time.time() - float(record.get("created_at", time.time())))
    origin_host = str(record.get("host", "unknown"))
    origin_candidate_sha = str(cached_result.get("candidate_sha", record.get("candidate_sha", "")))
    reused_from = {
        "host": origin_host,
        "candidate_sha": origin_candidate_sha,
        "age_sec": round(age_sec, 1),
    }
    print(
        f"[CACHE_HIT] reusing PASS verdict from host={origin_host} "
        f"candidate_sha={origin_candidate_sha[:12]} age={age_sec:.0f}s "
        f"(tree unchanged; skipping {len(required_steps)} step(s))"
    )

    result = HarnessResult(
        candidate_sha=current_sha,
        config_hash=str(cached_result["config_hash"]),
        required_steps=required_steps,
        started_steps=required_steps,
        passed_steps=required_steps,
        failed_steps=[],
        discovered_count=discovered_count,
        passed_count=int(cached_result["passed_count"]),
        skipped_count=int(cached_result["skipped_count"]),
        exit_codes={name: 0 for name in required_steps},
        artifact_refs=[str(config_path)],
        reused_from=reused_from,
    )
    print(f"{MARKER_HARNESS_RESULT} {result.model_dump_json()}")
    print(MARKER_HARNESS_PASS)
    _notify_hub_on_pass()
    return True


def run_with_cache(
    config: HarnessConfig,
    *,
    config_hash: str,
    quick: bool,
    include_holdout: bool,
    config_path: Path,
    no_cache: bool = False,
) -> bool:
    """Cache-aware, single-flighted wrapper around :func:`execute`.

    Flow: check cache -> acquire the machine-wide suite lock -> re-check
    cache (another process on this host may have just finished the same
    key) -> cross-host single-flight via Postgres (poll another host's
    in-flight run instead of duplicating it) -> run steps with
    ``DARKFAC_SUITE_LOCK_HELD=1`` in the child environment -> store the
    verdict only on a PASS.

    ``execute()`` itself is untouched by any of this (it has no idea the
    cache or lock exist) so every existing direct caller/test of ``execute``
    keeps behaving exactly as before.
    """

    steps = _selected_steps(config, quick=quick, include_holdout=include_holdout)
    cache_enabled = bool(steps) and not no_cache and not harness_cache.cache_disabled()

    tree_hash: str | None = None
    cache_key: str | None = None
    if cache_enabled:
        try:
            _ensure_clean_worktree()
        except RuntimeError:
            cache_enabled = False  # let execute() below produce the real, matching error
        else:
            tree_hash = harness_cache.tree_sha(PROJECT_ROOT)
            if tree_hash:
                cache_key = harness_cache.compute_cache_key(
                    tree_hash=tree_hash,
                    config_hash=config_hash,
                    step_names=[step.name for step in steps],
                    quick=quick,
                    include_holdout=include_holdout,
                )

    if cache_key is None:
        return execute(
            config,
            config_hash=config_hash,
            quick=quick,
            include_holdout=include_holdout,
            config_path=config_path,
        )

    hit = harness_cache.get_verdict(cache_key)
    if hit is not None:
        return _emit_cache_hit(hit, config_path=config_path)

    with harness_suite_lock.suite_lock() as _lock:  # noqa: F841 - context manager for its side effect
        hit = harness_cache.get_verdict(cache_key)
        if hit is not None:
            return _emit_cache_hit(hit, config_path=config_path)

        total_timeout_sec = float(sum(step.timeout_sec for step in steps))
        inflight_owned = harness_cache.acquire_inflight(cache_key, expires_in_sec=total_timeout_sec)
        if not inflight_owned:
            remote_hit = harness_cache.poll_for_remote_verdict(cache_key, deadline_sec=total_timeout_sec)
            if remote_hit is not None:
                harness_cache.set_local_verdict(cache_key, remote_hit)
                return _emit_cache_hit(remote_hit, config_path=config_path)
            # The other host's claim expired without landing a verdict; take over.
            inflight_owned = harness_cache.acquire_inflight(cache_key, expires_in_sec=total_timeout_sec)

        # `suite_lock()` above already exported DARKFAC_SUITE_LOCK_HELD=1 for
        # this whole critical section (unless locking is disabled), so the
        # pytest subprocess `run_step` shells out to inherits it automatically
        # and never re-acquires the lock its own parent is holding.
        captured: dict[str, HarnessResult] = {}
        try:
            success = execute(
                config,
                config_hash=config_hash,
                quick=quick,
                include_holdout=include_holdout,
                config_path=config_path,
                on_result=lambda result: captured.setdefault("result", result),
            )
        finally:
            if inflight_owned:
                harness_cache.release_inflight(cache_key)

        if success and "result" in captured:
            record = harness_cache.build_record(
                tree_hash=tree_hash,  # type: ignore[arg-type]
                candidate_sha=captured["result"].candidate_sha,
                config_hash=config_hash,
                result=captured["result"].model_dump(mode="json"),
            )
            harness_cache.store_verdict(cache_key, record)

    return success


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic validation harness runner")
    parser.add_argument("--config", default="harness.config.json", help="Harness config path")
    parser.add_argument("--quick", action="store_true", help="Run only quick steps")
    parser.add_argument("--holdout", action="store_true", help="Include holdout verification")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Always run fresh; never reuse or store a cached verdict",
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = PROJECT_ROOT / config_path
    try:
        config, config_hash = load_config(config_path)
        return 0 if run_with_cache(
            config,
            config_hash=config_hash,
            quick=args.quick,
            include_holdout=args.holdout,
            config_path=config_path,
            no_cache=args.no_cache,
        ) else 1
    except (RuntimeError, ValueError) as exc:
        print(f"[ERROR] {exc}")
        print(MARKER_HARNESS_FAIL)
        return 2


if __name__ == "__main__":
    sys.exit(main())
