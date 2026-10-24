"""Generate concurrent Hub API load and report cgroup memory evidence."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import threading
import time
from typing import Any, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

ENDPOINTS = ("/api/cloud/status", "/api/usage/accounts", "/api/telemetry/tickets")


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Measure Hub memory under concurrent API load")
    parser.add_argument("--base-url", required=True, help="Hub origin, for example https://darkhub.example")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--seconds", type=float, default=300)
    parser.add_argument("--out", type=Path, required=True, help="Path for the JSON evidence report")
    args = parser.parse_args(argv)
    if args.concurrency < 1:
        parser.error("--concurrency must be at least 1")
    if args.seconds <= 0:
        parser.error("--seconds must be greater than 0")
    args.base_url = args.base_url.rstrip("/")
    return args


def _percentile_95(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 2)


def build_report(
    *,
    latencies_ms: Sequence[float],
    errors: int,
    memory: dict[str, Any],
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    """Build a JSON-ready report from collected values without performing I/O."""
    peak_mb = memory.get("peak_mb")
    limit_mb = memory.get("limit_mb")
    peak_percent = memory.get("percent_of_limit")
    if isinstance(peak_mb, (int, float)) and isinstance(limit_mb, (int, float)) and limit_mb > 0:
        peak_percent = round(peak_mb * 100 / limit_mb, 2)
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "requests": len(latencies_ms),
        "errors": max(0, int(errors)),
        "latency_p95_ms": _percentile_95(latencies_ms),
        "peak_percent": peak_percent,
        "peak_mb": peak_mb,
        "limit_mb": limit_mb,
        "oom_kills": memory.get("oom_kills"),
        "memory": memory,
    }


def _get(url: str, *, timeout: float = 15.0) -> tuple[float, bool]:
    started = time.perf_counter()
    failed = False
    try:
        request = Request(url, headers={"User-Agent": "DarkFac-Hub-Memory-Load/1.0"})
        with urlopen(request, timeout=timeout) as response:
            response.read(1)
            failed = response.status < 200 or response.status >= 400
    except (HTTPError, URLError, TimeoutError, OSError):
        failed = True
    return (time.perf_counter() - started) * 1000, failed


def _load_worker(base_url: str, deadline: float, worker_index: int, results: list[tuple[float, bool]], lock: threading.Lock) -> None:
    endpoint_index = worker_index % len(ENDPOINTS)
    while time.monotonic() < deadline:
        latency, failed = _get(base_url + ENDPOINTS[endpoint_index])
        with lock:
            results.append((latency, failed))
        endpoint_index = (endpoint_index + 1) % len(ENDPOINTS)


def _read_memory(base_url: str) -> dict[str, Any]:
    try:
        with urlopen(base_url + "/health?memory=1", timeout=20.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        memory = payload.get("memory")
        return memory if isinstance(memory, dict) else {"error": "health response did not include memory"}
    except Exception as exc:
        return {"error": f"health memory read failed: {type(exc).__name__}"}


def run_load(args: argparse.Namespace) -> dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat()
    deadline = time.monotonic() + args.seconds
    results: list[tuple[float, bool]] = []
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(_load_worker, args.base_url, deadline, index, results, lock) for index in range(args.concurrency)]
        for future in as_completed(futures):
            future.result()
    finished = datetime.now(timezone.utc).isoformat()
    memory = _read_memory(args.base_url)
    return build_report(
        latencies_ms=[latency for latency, _ in results],
        errors=sum(1 for _, failed in results if failed),
        memory=memory,
        started_at=started,
        finished_at=finished,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    report = run_load(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report.get("oom_kills") is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
