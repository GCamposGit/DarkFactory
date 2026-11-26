"""Memory-safe pytest arguments and a single recovery from xdist infrastructure failure."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, TextIO


def with_workers(command: list[str], workers: int) -> list[str]:
    """Replace xdist options, using in-process pytest for zero or one worker."""
    result: list[str] = []
    tokens = iter(command)
    for token in tokens:
        if token in ("-n", "--numprocesses", "--dist", "--maxprocesses"):
            value = next(tokens, None)
            if token == "--dist" and workers > 1 and value is not None:
                result.extend([token, value])
        elif token.startswith("--dist=") and workers > 1:
            result.append(token)
        elif not token.startswith(("-n=", "--numprocesses=", "--dist=", "--maxprocesses=")):
            result.append(token)
    return [*result, "-n", str(workers if workers > 1 else 0)]


def _last_failed_cache_exists(root: Path) -> bool:
    from tests._cache_dir import external_cache_dir

    try:
        cached = json.loads((external_cache_dir(root) / "v/cache/lastfailed").read_text(encoding="utf-8"))
        return isinstance(cached, dict) and bool(cached)
    except (OSError, ValueError):
        return False


class StreamedTimeout(subprocess.TimeoutExpired):
    """Timeout whose output was already echoed live; callers must not print it again."""


def run_streaming(
    command: list[str], *, cwd: Path, timeout: float, heartbeat_s: float = 30.0,
    out: TextIO | None = None, **_ignored: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a command echoing its output line by line (flushed) while still capturing it."""
    sink = out or sys.stdout
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    chunks: list[str] = []

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            chunks.append(line)
            sink.write(line)
            sink.flush()

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    started = time.monotonic()
    while True:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            process.kill()
            reader.join(5)
            process.wait()
            raise StreamedTimeout(command, timeout, output="".join(chunks))
        reader.join(min(heartbeat_s, remaining))
        if not reader.is_alive():
            break
        print(f"[TEST_PROGRESS] running elapsed_sec={time.monotonic() - started:.0f} output_lines={len(chunks)}",
              file=sink, flush=True)
    process.wait()
    result: subprocess.CompletedProcess[str] = subprocess.CompletedProcess(command, process.returncode, stdout="".join(chunks))
    result.streamed = True  # type: ignore[attr-defined]
    return result


def run_with_serial_retry(
    command: list[str], *, cwd: Path, timeout: float,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None, **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Retry a crashed parallel pytest once, sharing its original time budget."""
    run = runner or subprocess.run
    started = time.monotonic()
    result = run(command, cwd=cwd, timeout=timeout, **kwargs)
    parallel = any(token == "-n" and command[i + 1] not in ("0", "1")
                   for i, token in enumerate(command[:-1]))
    output = result.stdout or ""
    crashed = result.returncode == 3 or re.search(r"INTERNALERROR|EOFError|worker.*crash", output, re.IGNORECASE)
    if result.returncode == 0 or not parallel or not crashed:
        return result
    print("[XDIST_RETRY] worker crash; retrying once with -n 0")
    retry = with_workers(command, 0)
    if _last_failed_cache_exists(cwd):
        retry.append("--lf")
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise subprocess.TimeoutExpired(command, timeout, output=output)
    # Keep the first crash out of the verdict; the serial attempt is authoritative.
    return run(retry, cwd=cwd, timeout=remaining, **kwargs)
