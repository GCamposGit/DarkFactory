"""Zero-Touch Cloud Dispatcher CLI for Dark Factory.

Enables remote dispatch and monitoring of autonomous tasks on the Cloud Coordinator & Worker
via HTTP over Tailscale, with zero Dokploy UI / web terminal intervention required.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("darkfac.dispatch")


def http_request(url: str, method: str = "GET", data: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    """Execute HTTP request using standard library urllib."""
    body_bytes = None
    headers = {"Accept": "application/json"}
    if data is not None:
        body_bytes = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = Request(url, data=body_bytes, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            res_body = response.read().decode("utf-8")
            return json.loads(res_body) if res_body else {}
    except HTTPError as err:
        err_body = err.read().decode("utf-8")
        try:
            parsed = json.loads(err_body)
            detail = parsed.get("detail", err_body)
        except Exception:
            detail = err_body
        raise RuntimeError(f"HTTP {err.code} from {url}: {detail}") from err
    except URLError as err:
        raise ConnectionError(f"Could not connect to {url}: {err.reason}") from err


def dispatch_cloud_task(
    coordinator_url: str,
    title: str,
    problem: str,
    journey: str,
    timeout_sec: float = 300.0,
    poll_interval_sec: float = 2.0,
    project_id: str = "darkfac",
) -> dict[str, Any]:
    """Dispatch an autonomous demand to the Cloud Coordinator and stream progress until completion."""
    coordinator_url = coordinator_url.rstrip("/")
    logger.info("Connecting to Dark Factory Cloud Coordinator at %s", coordinator_url)

    # 1. Health Probe
    try:
        health = http_request(f"{coordinator_url}/healthz", method="GET", timeout=5.0)
        logger.info(
            "Coordinator online: role=%s, db=%s, engine=%s, slots=%s",
            health.get("role"),
            health.get("database_status"),
            health.get("dbos_engine"),
            health.get("max_concurrent_slots"),
        )
        if health.get("database_status") != "ready":
            logger.warning("Database status is '%s'. Task execution may be deferred until DB is ready.", health.get("database_status"))
    except Exception as exc:
        raise ConnectionError(
            f"Failed to reach coordinator at {coordinator_url}. Ensure Tailscale is connected and the coordinator container is active: {exc}"
        ) from exc

    # 2. Intake Command Submission
    demand_uuid = uuid4().hex[:8]
    ext_id = f"remote-task-{demand_uuid}"
    command_payload = {
        "channel": "workstation_remote_cli",
        "external_id": ext_id,
        "project_id": project_id,
        "mode": "autonomous",
        "policy_ref": "policy-hf03-remote-dispatch",
        "payload": {
            "title": title,
            "problem": problem,
            "journey": journey,
            "non_goals": [
                "Sem intervencao em dashboards web Dokploy",
                "Sem comandos manuais de terminal",
                "Sem violacao de invariantes deterministas",
            ],
            "criteria": [
                "Status = completed",
                "Todos os estagios do DAG produtivo executados",
                "Artefatos verificados com hash SHA-256",
                "Conclusao 100% autonoma",
            ],
        },
    }

    logger.info("Submitting autonomous intake command (external_id=%s)...", ext_id)
    receipt = http_request(f"{coordinator_url}/api/v1/tasks", method="POST", data=command_payload, timeout=10.0)
    run_id = receipt["run_id"]
    demand_id = receipt["demand_id"]
    logger.info("Intake accepted! run_id=%s, demand_id=%s, mode=%s", run_id, demand_id, receipt.get("mode"))

    # 3. Stream & Poll Progress
    t_start = time.monotonic()
    deadline = t_start + timeout_sec
    observed_stages: dict[str, str] = {}

    print("\n" + "=" * 65)
    print(f"DARK FACTORY AUTONOMOUS PIPELINE EXECUTION ({run_id})")
    print("=" * 65)

    last_report_time = 0.0
    while time.monotonic() < deadline:
        try:
            run_status = http_request(f"{coordinator_url}/api/v1/tasks/{run_id}", method="GET", timeout=5.0)
        except Exception as exc:
            logger.warning("Transient error polling status: %s", exc)
            time.sleep(poll_interval_sec)
            continue

        overall_status = run_status.get("status", "unknown")
        jobs = run_status.get("jobs", [])

        # Log new stage transitions
        for j in jobs:
            st = j["stage"]
            jst = j["status"]
            if observed_stages.get(st) != jst:
                observed_stages[st] = jst
                elapsed = time.monotonic() - t_start
                out_ref = j["output_refs"][0] if j.get("output_refs") else ""
                print(f"[{elapsed:6.1f}s] Stage '{st}': {jst.upper()} {f'-> {out_ref}' if out_ref else ''}")

        if overall_status in ("completed", "succeeded"):
            total_elapsed = time.monotonic() - t_start
            print("=" * 65)
            print(f"PIPELINE COMPLETED SUCCESSFULLY IN {total_elapsed:.2f}s!")
            print("=" * 65)
            return run_status

        if overall_status in ("failed", "cancelled"):
            print("=" * 65)
            print(f"PIPELINE TERMINATED WITH STATUS: {overall_status.upper()}")
            print("=" * 65)
            return run_status

        time.sleep(poll_interval_sec)

    raise TimeoutError(f"Task {run_id} timed out after {timeout_sec:.1f}s (last status: {overall_status})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dark Factory Remote Cloud Dispatcher CLI")
    parser.add_argument(
        "--coordinator-url",
        type=str,
        default=os.environ.get("DARKFAC_COORDINATOR_URL", "http://100.83.176.60:8001"),
        help="URL of the Dark Factory Cloud Coordinator (default: http://100.83.176.60:8001)",
    )
    parser.add_argument(
        "--title",
        type=str,
        default="Tarefa Autonoma de Nuvem Zero-Touch",
        help="Title of the autonomous task",
    )
    parser.add_argument(
        "--problem",
        type=str,
        default="Validar pipeline 100% autonomo ponta a ponta sem qualquer intervencao manual no Dokploy.",
        help="Problem description for intake",
    )
    parser.add_argument(
        "--journey",
        type=str,
        default="Intake -> Grill -> Planning -> Development -> Validation -> Integration -> Deploy -> Journey Test",
        help="Journey description for intake",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Maximum seconds to wait for completion (default: 300s)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Polling interval in seconds (default: 2.0s)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output full JSON result on completion",
    )
    args = parser.parse_args(argv)

    try:
        res = dispatch_cloud_task(
            coordinator_url=args.coordinator_url,
            title=args.title,
            problem=args.problem,
            journey=args.journey,
            timeout_sec=args.timeout,
            poll_interval_sec=args.poll_interval,
        )
        if args.json:
            print(json.dumps(res, indent=2))
        return 0 if res.get("status") in ("completed", "succeeded") else 1
    except Exception as exc:
        logger.error("Dispatch execution failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
