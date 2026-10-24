"""Measure Hub memory and latency under a bounded concurrent HTTP workload."""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

ENDPOINTS = ("/api/cloud/status", "/api/usage/accounts", "/api/telemetry/tickets")


def summarize_samples(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(samples)
    latencies = sorted(float(row["latency_ms"]) for row in rows if isinstance(row.get("latency_ms"), (int, float)))
    memories = [int(row["memory_current_bytes"]) for row in rows if isinstance(row.get("memory_current_bytes"), (int, float))]
    p95 = latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)] if latencies else None
    return {
        "sample_count": len(rows),
        "peak_memory_bytes": max(memories) if memories else None,
        "blue_green_overlap_peak_memory_bytes": max(memories) * 2 if memories else None,
        "latency_p95_ms": p95,
        "latency_sample_count": len(latencies),
    }


def _get(url: str, timeout: float) -> tuple[int, dict[str, Any] | None, float]:
    started = time.perf_counter()
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = None
            if "/health" in url:
                try:
                    value = json.loads(response.read().decode("utf-8"))
                    payload = value if isinstance(value, dict) else None
                except (UnicodeError, json.JSONDecodeError):
                    payload = None
            return int(response.status), payload, (time.perf_counter() - started) * 1000
    except urllib.error.HTTPError as exc:
        return int(exc.code), None, (time.perf_counter() - started) * 1000


def run_probe(
    base_url: str,
    *,
    duration_seconds: float = 300.0,
    concurrency: int = 20,
    timeout: float = 15.0,
    output: Path,
) -> dict[str, Any]:
    if duration_seconds <= 0 or concurrency <= 0:
        raise ValueError("duration and concurrency must be positive")
    base_url = base_url.rstrip("/")
    started_at = time.time()
    initial_status, initial_health, _ = _get(f"{base_url}/health?memory=true", timeout)
    if initial_status != 200 or initial_health is None:
        raise RuntimeError("initial /health probe failed")
    expected_instance = initial_health.get("instance_id")
    expected_sha = initial_health.get("git_sha")
    initial_memory = initial_health.get("memory") or {}
    initial_oom = initial_memory.get("oom_kill_count")
    deadline = time.monotonic() + duration_seconds
    samples: list[dict[str, Any]] = []
    health_samples: list[dict[str, Any]] = []
    lock = threading.Lock()
    abort_reason: list[str] = []
    endpoint_index = 0

    def request_once(endpoint: str) -> None:
        nonlocal abort_reason
        if time.monotonic() >= deadline or abort_reason:
            return
        status, _, latency = _get(f"{base_url}{endpoint}", timeout)
        row: dict[str, Any] = {"endpoint": endpoint, "status": status, "latency_ms": latency, "sampled_at": time.time()}
        with lock:
            samples.append(row)

    def sample_health() -> None:
        status, payload, latency = _get(f"{base_url}/health?memory=true", timeout)
        row: dict[str, Any] = {"status": status, "latency_ms": latency, "sampled_at": time.time()}
        if payload:
            memory = payload.get("memory") or {}
            row.update({
                "git_sha": payload.get("git_sha"), "instance_id": payload.get("instance_id"),
                "memory_current_bytes": memory.get("current_bytes"),
                "memory_limit_bytes": memory.get("limit_bytes"),
                "oom_kill_count": memory.get("oom_kill_count"),
            })
            if payload.get("instance_id") != expected_instance or payload.get("git_sha") != expected_sha:
                abort_reason.append("Hub instance or git SHA changed; restart/deploy detected")
            oom = memory.get("oom_kill_count")
            if initial_oom is not None and oom is not None and oom > initial_oom:
                abort_reason.append(f"oom_kill counter increased from {initial_oom} to {oom}")
        health_samples.append(row)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        while time.monotonic() < deadline and not abort_reason:
            batch = []
            for _ in range(concurrency):
                endpoint = ENDPOINTS[endpoint_index % len(ENDPOINTS)]
                endpoint_index += 1
                batch.append(pool.submit(request_once, endpoint))
            for future in batch:
                future.result()
            if not abort_reason:
                sample_health()

    metrics = summarize_samples(samples)
    memory_metrics = summarize_samples(health_samples)
    metrics["peak_memory_bytes"] = memory_metrics["peak_memory_bytes"]
    metrics["blue_green_overlap_peak_memory_bytes"] = memory_metrics["blue_green_overlap_peak_memory_bytes"]
    report = {
        "base_url": base_url,
        "duration_seconds": duration_seconds,
        "concurrency": concurrency,
        "endpoints": list(ENDPOINTS),
        "started_at": started_at,
        "initial_git_sha": expected_sha,
        "initial_instance_id": expected_instance,
        "initial_oom_kill_count": initial_oom,
        "aborted": bool(abort_reason),
        "abort_reason": abort_reason[0] if abort_reason else None,
        **metrics,
        "non_200_count": sum(row["status"] != 200 for row in samples),
        "samples": samples,
        "health_samples": health_samples,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://darkhub.ggcampos.com")
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--output", type=Path, default=Path("hub-load-report.json"))
    args = parser.parse_args()
    try:
        report = run_probe(args.url, duration_seconds=args.duration, concurrency=args.concurrency, timeout=args.timeout, output=args.output)
    except Exception as exc:  # noqa: BLE001 - CLI returns a structured diagnostic
        print(f"Falha na sonda: {exc}")
        return 1
    print(json.dumps({key: value for key, value in report.items() if key != "samples"}, ensure_ascii=False, indent=2))
    return 1 if report["aborted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
