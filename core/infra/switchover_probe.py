"""External one-second availability evidence for a live Hub deployment."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

DEFAULT_HEALTH_URL = "https://darkhub.ggcampos.com/health"


@dataclass(frozen=True)
class ProbeSample:
    at: str
    status_code: Optional[int]
    git_sha: Optional[str]
    error: Optional[str] = None


@dataclass(frozen=True)
class SwitchoverEvidence:
    expected_sha: str
    started_at: str
    finished_at: str
    interval_seconds: float
    samples: tuple[ProbeSample, ...]
    all_http_200: bool
    converged: bool

    @property
    def passed(self) -> bool:
        return bool(self.samples) and self.all_http_200 and self.converged


HttpGet = Callable[[str], tuple[int, Mapping[str, Any]]]


def _http_get(url: str) -> tuple[int, Mapping[str, Any]]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.load(response)
            return int(response.status), body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except (OSError, ValueError):
            body = {}
        finally:
            exc.close()
        return int(exc.code), body if isinstance(body, dict) else {}


def probe_until_stopped(
    expected_sha: str,
    *,
    stop_event: threading.Event,
    evidence_path: Path,
    http_get: Optional[HttpGet] = None,
    interval_seconds: float = 1.0,
    url: str = DEFAULT_HEALTH_URL,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    wait: Callable[[threading.Event, float], bool] = lambda event, seconds: event.wait(seconds),
) -> SwitchoverEvidence:
    """Sample /health until deployment completion and persist JSON evidence."""
    if not expected_sha or interval_seconds <= 0:
        raise ValueError("expected_sha and a positive interval are required")
    getter = http_get or _http_get
    started_at = wall_clock().isoformat()
    started = clock()
    samples: list[ProbeSample] = []
    next_sample = started
    while not stop_event.is_set():
        now = clock()
        if now < next_sample:
            if wait(stop_event, next_sample - now):
                break
            now = clock()
        if stop_event.is_set():
            break
        stamp = wall_clock().isoformat()
        try:
            status, payload = getter(url)
            sha = payload.get("git_sha")
            samples.append(ProbeSample(stamp, int(status), str(sha) if sha is not None else None))
        except Exception as exc:
            samples.append(ProbeSample(stamp, None, None, type(exc).__name__))
        next_sample = max(next_sample + interval_seconds, clock())
    finished_at = wall_clock().isoformat()
    evidence = SwitchoverEvidence(
        expected_sha=expected_sha,
        started_at=started_at,
        finished_at=finished_at,
        interval_seconds=interval_seconds,
        samples=tuple(samples),
        all_http_200=bool(samples) and all(sample.status_code == 200 for sample in samples),
        converged=any((sample.git_sha or "").lower() == expected_sha.lower() for sample in samples),
    )
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(evidence)
    payload["passed"] = evidence.passed
    evidence_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return evidence


__all__ = ["DEFAULT_HEALTH_URL", "ProbeSample", "SwitchoverEvidence", "probe_until_stopped"]
