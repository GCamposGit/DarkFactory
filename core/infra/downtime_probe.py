"""One-second public /health sampling for measuring Hub downtime."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping

DEFAULT_URL = "https://darkhub.ggcampos.com/health"
DEFAULT_TIMEOUT_SECONDS = 3.0

ProbeResult = tuple[int, str | None]
Sample = dict[str, object]
Prober = Callable[[str, float], ProbeResult]
Writer = Callable[[Path, str], None]


def http_probe(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> ProbeResult:
    """Return the HTTP status and reported git SHA; network errors are raised to the sampler."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            payload = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        payload = exc.read()
    sha: str | None = None
    try:
        data = json.loads(payload.decode("utf-8"))
        value = data.get("git_sha", data.get("sha")) if isinstance(data, dict) else None
        sha = str(value) if value else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return status, sha


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line + "\n")


def run_probe(
    url: str = DEFAULT_URL,
    seconds: int = 600,
    out: Path = Path("hub_downtime.jsonl"),
    *,
    prober: Prober = http_probe,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    writer: Writer = _append_line,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Sample immediately and then on integer-second deadlines without accumulating probe drift."""
    if seconds < 1:
        raise ValueError("seconds must be at least 1")
    output = Path(out)
    start = clock()
    samples: list[Sample] = []
    for index in range(seconds):
        deadline = start + index
        delay = deadline - clock()
        if delay > 0:
            sleep(delay)
        began = clock()
        ts = wall_clock().astimezone(timezone.utc).isoformat()
        status, sha, error = 0, None, None
        try:
            status, sha = prober(url, timeout)
        except Exception as exc:  # network and injected-prober failures are observations, not fatal errors
            error = str(exc) or exc.__class__.__name__
        sample: Sample = {
            "ts": ts,
            "status": int(status),
            "sha": sha,
            "latency_ms": round(max(0.0, clock() - began) * 1000, 3),
            "error": error,
        }
        samples.append(sample)
        writer(output, json.dumps(sample, ensure_ascii=False, separators=(",", ":")))
    return summarize(samples)


def summarize(samples: Iterable[Mapping[str, object]]) -> dict[str, object]:
    """Purely summarize observations, preserving first-seen order for distinct SHAs."""
    total = non200 = current_streak = max_streak = 0
    shas: list[str] = []
    for sample in samples:
        total += 1
        status = sample.get("status")
        if status != 200:
            non200 += 1
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0
        sha = sample.get("sha")
        if sha and str(sha) not in shas:
            shas.append(str(sha))
    return {
        "total": total,
        "non200": non200,
        "max_failure_streak": max_streak,
        "distinct_shas": shas,
    }
