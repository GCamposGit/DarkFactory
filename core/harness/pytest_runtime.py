"""Memory-safe pytest arguments and a single recovery from xdist infrastructure failure."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any


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


def run_with_serial_retry(command: list[str], *, cwd: Path, timeout: float, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Retry a crashed parallel pytest once, sharing its original time budget."""
    started = time.monotonic()
    result = subprocess.run(command, cwd=cwd, timeout=timeout, **kwargs)
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
    return subprocess.run(retry, cwd=cwd, timeout=remaining, **kwargs)
