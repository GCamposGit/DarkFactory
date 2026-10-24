"""Blue-green deploy of the DarkHub compose (USR-339, OA-057 option A).

    python C:\\dev\\DarkFac\\scripts\\dokploy_bluegreen.py [--sha <40hex>] [--timeout 900]

Reads the compose at origin/main (or --sha), waits for the GHCR image, installs the overlap compose
in Dokploy, gates on the public /health serving the new sha, then installs the cutover compose.
Exit 0 ok, 1 deploy failed (previous colour kept), 2 usage/credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.orchestrator.bluegreen import BlueGreenError, parse_state, run_bluegreen_deploy  # noqa: E402
from core.infra.deploy_probe import DeployProbe, alert_non200  # noqa: E402
from core.orchestrator.compose_render import (  # noqa: E402
    ComposeRenderError,
    render_darkhub_compose,
    resolve_hub_image_source,
    wait_for_ghcr_image,
)
from scripts import dokploy_redeploy as dr  # noqa: E402


def _darkhub_service(transport: dr.Transport) -> dr.Service:
    services = dr._resolve_services(transport, dr.DEFAULT_PROJECT, dr.DEFAULT_ENVIRONMENT, ["Darkhub"])
    compose = [s for s in services if s.kind == "compose" and s.name.lower() == "darkhub"]
    if len(compose) != 1:
        raise dr.DokployUsageError("Darkhub compose not found uniquely in Dokploy")
    return compose[0]


def main(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", default=None)
    parser.add_argument("--restore-legacy", action="store_true",
                        help="only compose.update with the legacy (build) compose, no deploy")
    parser.add_argument("--timeout", type=float, default=dr.DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        api_url, api_key = dr.resolve_credentials(dict(env if env is not None else os.environ))
        image_source = resolve_hub_image_source(dict(env if env is not None else os.environ))
        if not args.restore_legacy and image_source != "ghcr":
            raise BlueGreenError("blue-green deployment requires DARKHUB_IMAGE_SOURCE='ghcr'")
        transport = dr.make_urllib_transport(api_url, api_key)
        if args.restore_legacy:
            sha = (args.sha or str(dr.fetch_darkhub_health().get("git_sha", ""))).lower()
        else:
            sha = (args.sha or dr.get_origin_main_sha() or "").lower()
        source = dr.get_origin_main_compose(sha)
        service = _darkhub_service(transport)
    except (dr.DokployUsageError, BlueGreenError, ComposeRenderError) as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return dr.EXIT_USAGE_ERROR

    if args.restore_legacy:
        try:
            legacy = render_darkhub_compose(source, sha, resolve_hub_image_source())
            if parse_state(legacy) is not None:
                raise BlueGreenError("legacy compose still contains the blue-green marker")
            transport("POST", "/api/compose.update", {
                "composeId": service.service_id, "sourceType": "raw",
                "composePath": "docker-compose.yml", "composeFile": legacy,
            })
        except Exception as exc:  # noqa: BLE001
            print(f"restore-legacy failed: {exc}", file=sys.stderr)
            return dr.EXIT_DEPLOY_FAILED
        print(f"restore-legacy ok: compose.update only (no deploy), image source={resolve_hub_image_source()} at sha={sha}")
        return dr.EXIT_OK

    def read_compose() -> Optional[str]:
        payload = dr.fetch_service_status(transport, service)
        value = payload.get("composeFile")
        return value if isinstance(value, str) else None

    def apply_compose(compose_file: str) -> None:
        baseline = dr.latest_deployment(dr.fetch_service_status(transport, service))
        transport(
            "POST",
            "/api/compose.update",
            {
                "composeId": service.service_id,
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "composeFile": compose_file,
            },
        )
        dr.trigger_deploy(transport, service)
        result = dr.wait_for_service(transport, service, baseline, args.timeout)
        if result.outcome != dr.WaitOutcome.DONE:
            raise BlueGreenError(f"Dokploy compose up did not succeed ({result.outcome})")

    def sha_is_serving(expected: str) -> bool:
        for _ in range(3):
            try:
                if str(dr.fetch_darkhub_health().get("git_sha", "")).lower() != expected:
                    return False
            except Exception:
                return False
            time.sleep(1)
        return True

    def deployments_for_sha() -> int:
        deployments = dr.fetch_service_status(transport, service).get("deployments")
        if not isinstance(deployments, list):
            return 0
        return sum(
            1 for item in deployments
            if isinstance(item, dict) and sha in json.dumps(item).lower()
        )

    try:
        deploy_probe = DeployProbe(target_sha=sha)
        final = run_bluegreen_deploy(
            source=source,
            new_sha=sha,
            read_compose=read_compose,
            apply_compose=apply_compose,
            sha_is_serving=sha_is_serving,
            wait_ghcr=lambda s: wait_for_ghcr_image(s),
            sleep_fn=time.sleep,
            deploy_probe=deploy_probe,
            deployments_for_sha=deployments_for_sha,
        )
    except Exception as exc:  # BlueGreenError, GHCR, Dokploy
        result = deploy_probe.finish() if "deploy_probe" in locals() else None
        if result is not None:
            print(f"deploy probe: samples={result.sample_count} non200={result.non200} shas={','.join(result.observed_shas)}", file=sys.stderr)
            alert_non200(result)
        print(f"blue-green deploy failed: {exc}", file=sys.stderr)
        return dr.EXIT_DEPLOY_FAILED
    result = final.probe_result
    if result is not None:
        print(f"deploy probe: samples={result.sample_count} non200={result.non200} shas={','.join(result.observed_shas)} deployments_for_sha={result.deployments_for_sha} duplicate_deployment={result.duplicate_deployment}")
        alert_non200(result)
        if result.non200:
            return dr.EXIT_DEPLOY_FAILED
    print(f"blue-green ok: active={final.active} sha={final.sha_of(final.active)}")
    return dr.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
