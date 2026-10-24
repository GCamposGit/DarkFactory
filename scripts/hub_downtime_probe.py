"""One-second public health probe and zero-downtime report verifier (USR-207)."""

from __future__ import annotations

import argparse
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

DEFAULT_URL = "https://darkhub.ggcampos.com/health"
DEFAULT_INTERVAL_SECONDS = 1.0
logger = logging.getLogger(__name__)


def extract_sha(payload: Any) -> Optional[str]:
    """Return the health payload's git SHA when present and usable."""
    if not isinstance(payload, dict):
        return None
    sha = payload.get("git_sha")
    if not isinstance(sha, str) or not sha.strip():
        return None
    return sha.strip()


def _http_get(url: str, timeout: float) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # HTTP errors are responses, so retain their status and attempt to extract /health's SHA.
        body = exc.read()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        return {"status": exc.code, "payload": payload}
    with response:
        body = response.read()
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        return {"status": response.status, "payload": payload}


def _error_kind(exc: BaseException) -> str:
    if isinstance(exc, (TimeoutError, socket_timeout_type())):
        return "timeout"
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket_timeout_type())):
        return "timeout"
    return "connection_error"


def socket_timeout_type() -> type[BaseException]:
    # urllib's socket timeout is exposed as TimeoutError on supported Python versions.
    import socket

    return socket.timeout


def sample_probe(
    url: str,
    output_path: Path,
    *,
    duration: float,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    timeout: float = 5.0,
    http_get: Callable[[str, float], Dict[str, Any]] = _http_get,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Write one JSONL sample immediately and at each interval until duration elapses."""
    if interval <= 0 or duration < 0 or timeout <= 0:
        raise ValueError("duration must be >= 0 and interval/timeout must be > 0")
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    start = monotonic()
    deadline = start + duration
    next_sample = start
    count = 0
    with output_path.open("w", encoding="utf-8", newline="\n") as stream:
        while next_sample <= deadline:
            delay = next_sample - monotonic()
            if delay > 0:
                sleep(delay)
            sample_started = monotonic()
            row: Dict[str, Any] = {
                "timestamp": now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "status": None,
                "sha": None,
                "latency_ms": None,
                "error": None,
            }
            try:
                response = http_get(url, timeout)
                row["status"] = response.get("status")
                row["sha"] = extract_sha(response.get("payload", response.get("body")))
            except (TimeoutError, urllib.error.URLError, OSError) as exc:
                row["error"] = _error_kind(exc)
                logger.debug("health probe request failed: %s", row["error"])
            finally:
                row["latency_ms"] = round(max(0.0, monotonic() - sample_started) * 1000, 3)
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            count += 1
            next_sample += interval
    return count


def verify_report(report_path: Path) -> Dict[str, Any]:
    """Approve only reports with all HTTP 200 samples and at least two health SHAs."""
    rows = []
    malformed = 0
    try:
        lines = Path(report_path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {"passed": False, "samples": 0, "non200": 1, "shas": [], "error": str(exc)}
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("sample must be a JSON object")
            rows.append(row)
        except (json.JSONDecodeError, ValueError):
            malformed += 1
    non200 = sum(row.get("status") != 200 or bool(row.get("error")) for row in rows) + malformed
    shas = sorted({sha for row in rows if (sha := extract_sha({"git_sha": row.get("sha")}))})
    passed = bool(rows) and non200 == 0 and len(shas) >= 2
    return {"passed": passed, "samples": len(rows) + malformed, "non200": non200, "shas": shas}


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", type=Path, help="verify an existing JSONL sample report")
    parser.add_argument("--verdict-output", type=Path, help="also save the verifier's JSON verdict")
    parser.add_argument("--url", default=DEFAULT_URL, help="public HTTPS /health URL")
    parser.add_argument("--output", type=Path, help="JSONL output file for sampling")
    parser.add_argument("--duration", type=float, default=600.0, help="sampling duration in seconds")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, help="seconds between samples")
    parser.add_argument("--timeout", type=float, default=5.0, help="per-request timeout in seconds")
    args = parser.parse_args(argv)
    if args.verify:
        result = verify_report(args.verify)
        verdict = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if args.verdict_output:
            args.verdict_output.parent.mkdir(parents=True, exist_ok=True)
            args.verdict_output.write_text(verdict + "\n", encoding="utf-8")
        print(verdict)
        return 0 if result["passed"] else 1
    if not args.output:
        parser.error("--output is required when sampling")
    if not args.url.lower().startswith("https://"):
        parser.error("--url must use HTTPS")
    count = sample_probe(args.url, args.output, duration=args.duration, interval=args.interval, timeout=args.timeout)
    print(f"Saved {count} samples to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
