#!/usr/bin/env python3
"""Dokploy redeploy CLI for DarkFac ("Deploy pos-merge").

Owner decision: after any DarkFac change lands on `main`, every harness
(Claude Code, Codex, Grok, Antigravity) redeploys every compose/application
service of the `darkfac-core` project on Dokploy (https://dokploy.ggcampos.com)
and waits for the result. This script is the single implementation of that
step; see AGENTS.md ("Deploy pos-merge") and
docs/HARNESS_INTEROP.md for the cross-harness contract, and
docs/runbooks/dokploy_redeploy.md for how the Owner provisions the API token.

Design notes:
- stdlib only (urllib), so it runs on a bare Python 3.12+ install with no
  extra dependencies, on both Windows and Linux.
- Domain logic (response-shape normalization, service selection, wait-loop
  decisions) is implemented as pure functions with no I/O, kept separate
  from the HTTP transport and the CLI wiring, so it is unit-testable without
  a live Dokploy instance -- see tests/test_dokploy_redeploy.py, which
  injects a fake transport (following the convention already used by
  core/orchestrator/deployment_adapter.py's DokployDeploymentAdapter).
- Credentials are read from DOKPLOY_API_URL / DOKPLOY_API_KEY in the
  environment, falling back on Windows to the User registry hive
  (HKEY_CURRENT_USER\\Environment) -- the same fallback convention used by
  core/execution/providers.py for OPENROUTER_API_KEY, needed because a
  long-lived shell/IDE process started before the Owner set the variable
  does not see it in os.environ. The key is never printed, logged, or
  echoed back in an exception message; HTTP error bodies are sanitized
  before being surfaced.
- Discovery responses (project.all, compose.one, application.one) contain
  service environment variables and other secrets. Only specific,
  allow-listed fields (name, id, status, deployment title/status/createdAt)
  are ever extracted and printed -- the raw payloads are never dumped.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("dokploy_redeploy")

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.git.safe_show import safe_show

DEFAULT_PROJECT = "darkfac-core"
DEFAULT_ENVIRONMENT = "production"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DARKHUB_HEALTH_URL = "https://darkhub.ggcampos.com/health"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

# Hard allowlist: this tool may only ever act on these Dokploy projects,
# regardless of --project. Claude Code's own permission config allows
# `python scripts/dokploy_redeploy.py *` with any arguments (it is a routine
# post-merge step -- see docs/HARNESS_INTEROP.md), so `--project` is not
# gated by a human approval prompt the way an arbitrary new command would
# be. Without this check, `--project "My First Project"` (or any other
# unrelated Dokploy project) could be redeployed by mistake or by a
# malicious/careless argument, with no confirmation step in between. Case-
# sensitive exact match only; extend this set deliberately if the Owner
# adds another project this tool should be allowed to touch.
ALLOWED_PROJECTS = frozenset({"darkfac-core"})

# Only these services have Dokploy deploy titles comparable with origin/main.
# Darkhub's raw compose instead proves its commit through /health.
CODE_BEARING_SERVICES = frozenset({"darkfac-cloud"})

TERMINAL_SUCCESS_STATUSES = {"done"}
TERMINAL_FAILURE_STATUSES = {"error", "failed"}

EXIT_OK = 0
EXIT_DEPLOY_FAILED = 1
EXIT_USAGE_ERROR = 2
EXIT_NODE_DIRTY = 3

# A transport takes (method, path, json_body) and returns the parsed JSON
# response (a dict or a list, depending on the endpoint). Production code
# builds one via make_urllib_transport(); tests inject a fake.
Transport = Callable[[str, str, Optional[Dict[str, Any]]], Any]


class DokployUsageError(Exception):
    """Usage, credential, or discovery error -> process exit code 2."""


def check_project_allowed(project: str) -> None:
    """Hard guard: raises DokployUsageError unless `project` is in
    ALLOWED_PROJECTS (exact, case-sensitive match). Called both in `main()`
    right after argument parsing -- before credentials are even resolved or
    any transport is built, so a disallowed --project never reaches the
    network -- and again in `_resolve_services()` as defense in depth for
    any other caller of the discovery/selection functions."""
    if project not in ALLOWED_PROJECTS:
        raise DokployUsageError(
            f"Refusing --project {project!r}: only {', '.join(sorted(ALLOWED_PROJECTS))} may be "
            "deployed by this tool. Ask the Owner to extend ALLOWED_PROJECTS in "
            "scripts/dokploy_redeploy.py if this is intentional."
        )


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _read_windows_user_env(name: str) -> Optional[str]:
    """Best-effort read of a Windows *User* environment variable via the
    registry. Only used as a fallback when a variable is absent from
    ``os.environ`` (e.g. a shell/IDE process started before the Owner set
    it). Returns ``None`` on any failure, including on non-Windows
    platforms, so callers never need a platform check of their own."""
    if sys.platform != "win32":
        return None
    try:
        import winreg  # win32-only; imported lazily so the module still
        # imports cleanly on Linux/macOS.

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except Exception:
        return None


def resolve_credentials(
    env: Dict[str, str],
    registry_reader: Callable[[str], Optional[str]] = _read_windows_user_env,
) -> Tuple[str, str]:
    """Resolves (api_url, api_key) from `env`, falling back to
    `registry_reader` for whichever is missing. Raises DokployUsageError
    (naming only the variables, never a value) if either is still missing
    after the fallback."""
    api_url = (env.get("DOKPLOY_API_URL") or "").strip()
    if not api_url:
        api_url = (registry_reader("DOKPLOY_API_URL") or "").strip()
    api_key = (env.get("DOKPLOY_API_KEY") or "").strip()
    if not api_key:
        api_key = (registry_reader("DOKPLOY_API_KEY") or "").strip()

    missing = [name for name, value in (("DOKPLOY_API_URL", api_url), ("DOKPLOY_API_KEY", api_key)) if not value]
    if missing:
        raise DokployUsageError(
            "Missing Dokploy credentials: "
            + ", ".join(missing)
            + ". Set them as environment variables (Windows: User scope is also read "
            "automatically via the registry). See docs/runbooks/dokploy_redeploy.md."
        )
    return api_url.rstrip("/"), api_key


def _sanitize(text: str, *secrets: str) -> str:
    """Replaces every occurrence of any non-empty `secrets` value in `text`.
    Used on anything derived from an HTTP response or exception before it is
    printed or re-raised, so a credential can never leak through an error
    message even if a proxy or the API echoes a header back."""
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Service:
    """A single Dokploy compose or application service, scoped to one
    project/environment."""

    name: str
    kind: str  # "compose" | "application"
    service_id: str
    status: Optional[str] = None


@dataclass(frozen=True)
class Deployment:
    """One entry of a service's deployment history (allow-listed fields
    only -- never the full provider payload)."""

    deployment_id: str
    status: str
    title: str
    created_at: Optional[datetime]


class WaitOutcome:
    """String constants for the per-tick wait-loop decision."""

    CONTINUE = "continue"
    DONE = "done"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class ServiceWaitResult:
    service: Service
    outcome: str
    deployment: Optional[Deployment]
    elapsed_seconds: float


def _parse_iso8601(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pure domain functions: response-shape normalization
# ---------------------------------------------------------------------------


def _extract_services(container: Dict[str, Any]) -> List[Service]:
    services: List[Service] = []
    for compose in container.get("compose") or []:
        name = str(compose.get("name") or "")
        service_id = str(compose.get("composeId") or "")
        if name and service_id:
            services.append(
                Service(name=name, kind="compose", service_id=service_id, status=compose.get("composeStatus"))
            )
    for app in container.get("applications") or []:
        name = str(app.get("name") or "")
        service_id = str(app.get("applicationId") or "")
        if name and service_id:
            services.append(
                Service(name=name, kind="application", service_id=service_id, status=app.get("applicationStatus"))
            )
    return services


def normalize_project_services(
    projects: List[Dict[str, Any]],
    project: str,
    environment: str,
) -> List[Service]:
    """Extracts compose+application services for `project`/`environment`
    from a GET /api/project.all response.

    Handles both response shapes:
    - Current: each project has `environments: [{name, compose, applications,
      ...}, ...]`.
    - Older: `compose`/`applications` sit directly on the project (no
      `environments` list) -- these are only honored for the default
      ("production") environment, since the older shape has no explicit
      per-environment name to match against.

    Hard guard: only the project whose `name` equals `project` is ever
    inspected, and only its environment whose `name` equals `environment`.
    A same-named service (e.g. an unrelated project's own "n8n" compose) in
    a *different* project is structurally excluded -- it is never reached by
    this function regardless of `--only`.
    """
    for proj in projects:
        if str(proj.get("name") or "") != project:
            continue
        environments = proj.get("environments")
        if isinstance(environments, list) and environments:
            services: List[Service] = []
            for env in environments:
                if str(env.get("name") or "") != environment:
                    continue
                services.extend(_extract_services(env))
            return services
        # Older shape: no environments list at all.
        if environment != DEFAULT_ENVIRONMENT:
            return []
        return _extract_services(proj)
    return []


def select_services(services: Sequence[Service], only: Optional[Sequence[str]]) -> List[Service]:
    """Returns the subset of `services` matching `only` (case-insensitive
    name match, order follows `only`), or all of `services` when `only` is
    falsy. Raises DokployUsageError listing valid names if any requested
    name does not match."""
    if not only:
        return list(services)

    by_lower: Dict[str, Service] = {svc.name.lower(): svc for svc in services}
    selected: List[Service] = []
    unknown: List[str] = []
    seen: set[str] = set()
    for raw_name in only:
        name = raw_name.strip()
        if not name:
            continue
        svc = by_lower.get(name.lower())
        if svc is None:
            unknown.append(name)
        elif svc.name not in seen:
            selected.append(svc)
            seen.add(svc.name)

    if unknown:
        valid = ", ".join(sorted(svc.name for svc in services)) or "(none)"
        raise DokployUsageError(
            f"Unknown --only service name(s): {', '.join(unknown)}. Valid names: {valid}"
        )
    return selected


def latest_deployment(payload: Dict[str, Any]) -> Optional[Deployment]:
    """Extracts the most recent Deployment from a compose.one/application.one
    payload's `deployments` list. Only allow-listed fields are read -- the
    rest of `payload` (which includes service env vars/secrets) is never
    touched here."""
    raw_deployments = payload.get("deployments") or []
    parsed = [
        Deployment(
            deployment_id=str(item.get("deploymentId") or ""),
            status=str(item.get("status") or ""),
            title=str(item.get("title") or ""),
            created_at=_parse_iso8601(item.get("createdAt")),
        )
        for item in raw_deployments
    ]
    if not parsed:
        return None
    with_timestamp = [d for d in parsed if d.created_at is not None]
    if with_timestamp:
        return max(with_timestamp, key=lambda d: d.created_at)
    # No parseable timestamps: fall back to list order (providers return it
    # oldest-first), so the just-triggered deployment is still found.
    return parsed[-1]


def evaluate_wait_state(
    baseline: Optional[Deployment],
    current: Optional[Deployment],
    elapsed_seconds: float,
    timeout_seconds: float,
) -> Tuple[str, Optional[Deployment]]:
    """Pure decision for a single wait-loop tick.

    A `current` deployment counts as "new" (the one just triggered) when
    there was no baseline, or its id differs from the baseline's, or its
    timestamp is strictly newer. Terminal states (`done`/`error`) on the new
    deployment end the loop immediately; otherwise the loop continues until
    `elapsed_seconds` reaches `timeout_seconds`.
    """
    is_new = current is not None and (
        baseline is None
        or current.deployment_id != baseline.deployment_id
        or (
            current.created_at is not None
            and baseline.created_at is not None
            and current.created_at > baseline.created_at
        )
    )
    if is_new:
        if current.status in TERMINAL_SUCCESS_STATUSES:
            return WaitOutcome.DONE, current
        if current.status in TERMINAL_FAILURE_STATUSES:
            return WaitOutcome.ERROR, current
    if elapsed_seconds >= timeout_seconds:
        return WaitOutcome.TIMEOUT, current
    return WaitOutcome.CONTINUE, current


def deploy_request_for(service: Service) -> Tuple[str, str, Dict[str, str]]:
    """Returns (method, path, json_body) to trigger `service`'s deploy."""
    if service.kind == "compose":
        return "POST", "/api/compose.deploy", {"composeId": service.service_id}
    return "POST", "/api/application.deploy", {"applicationId": service.service_id}


def status_request_for(service: Service) -> Tuple[str, str]:
    """Returns (method, path) to read `service`'s current status/history."""
    if service.kind == "compose":
        query = urllib.parse.urlencode({"composeId": service.service_id})
        return "GET", f"/api/compose.one?{query}"
    query = urllib.parse.urlencode({"applicationId": service.service_id})
    return "GET", f"/api/application.one?{query}"


# ---------------------------------------------------------------------------
# HTTP transport (I/O)
# ---------------------------------------------------------------------------


def make_urllib_transport(api_url: str, api_key: str, timeout: float = 30.0) -> Transport:
    """Builds a Transport backed by urllib against a live Dokploy API.

    The api_key is only ever placed in the `x-api-key` request header; any
    error text derived from the response (HTTPError body, URLError reason)
    is sanitized with `_sanitize()` before being wrapped in a
    DokployUsageError, so it can never leak through an exception message.
    """

    def transport(method: str, path: str, body: Optional[Dict[str, Any]]) -> Any:
        url = f"{api_url}{path}" if path.startswith("/") else f"{api_url}/{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"x-api-key": api_key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            try:
                body_text = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body_text = ""
            raise DokployUsageError(
                f"Dokploy API returned HTTP {exc.code} for {method} {path}: "
                f"{_sanitize(body_text, api_key)[:500]}"
            ) from None
        except urllib.error.URLError as exc:
            raise DokployUsageError(
                f"Dokploy API request failed for {method} {path}: {_sanitize(str(exc.reason), api_key)}"
            ) from None

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": _sanitize(raw, api_key)[:500]}

    return transport


def fetch_project_all(transport: Transport) -> List[Dict[str, Any]]:
    result = transport("GET", "/api/project.all", None)
    if isinstance(result, list):
        return result
    if isinstance(result, dict) and isinstance(result.get("data"), list):
        return result["data"]
    raise DokployUsageError("Unexpected /api/project.all response shape (expected a list of projects).")


def fetch_service_status(transport: Transport, service: Service) -> Dict[str, Any]:
    method, path = status_request_for(service)
    result = transport(method, path, None)
    if not isinstance(result, dict):
        raise DokployUsageError(f"Unexpected status response shape for service {service.name!r}.")
    return result


def trigger_deploy(transport: Transport, service: Service) -> None:
    method, path, body = deploy_request_for(service)
    transport(method, path, body)


def wait_for_service(
    transport: Transport,
    service: Service,
    baseline: Optional[Deployment],
    timeout_seconds: float,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
) -> ServiceWaitResult:
    """Polls `service`'s status until the deployment triggered after
    `baseline` reaches a terminal state or `timeout_seconds` elapses.
    `sleep_fn`/`clock_fn` are injectable so tests run this loop instantly
    with a fake clock instead of sleeping for real."""
    start = clock_fn()
    while True:
        payload = fetch_service_status(transport, service)
        current = latest_deployment(payload)
        elapsed = clock_fn() - start
        outcome, considered = evaluate_wait_state(baseline, current, elapsed, timeout_seconds)
        if outcome != WaitOutcome.CONTINUE:
            return ServiceWaitResult(service=service, outcome=outcome, deployment=considered, elapsed_seconds=elapsed)
        sleep_fn(poll_interval_seconds)


# ---------------------------------------------------------------------------
# Informational: local git state (never affects exit code)
# ---------------------------------------------------------------------------


def get_local_origin_main_subject(repo_root: Path = REPO_ROOT) -> Optional[str]:
    """Best-effort `git log -1 --format=%s origin/main` subject, for the
    informational "matches local origin/main" note. Returns None on any
    failure (git missing, no such ref, not a repo, etc.)."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s", "origin/main"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    subject = result.stdout.strip()
    return subject or None


def get_origin_main_sha(repo_root: Path = REPO_ROOT) -> Optional[str]:
    """Read the exact full commit that a DarkHub deployment must expose."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "origin/main"], cwd=repo_root,
            capture_output=True, text=True, timeout=10, check=False,
        )
    except Exception:
        return None
    sha = result.stdout.strip().lower()
    return sha if result.returncode == 0 and SHA_PATTERN.fullmatch(sha) else None


def get_origin_main_compose(sha: str, repo_root: Path = REPO_ROOT) -> str:
    """Read the hub compose file from the exact commit selected for deployment."""
    if not SHA_PATTERN.fullmatch(sha):
        raise DokployUsageError("Darkhub build requires a full origin/main SHA")
    try:
        result = safe_show(sha, "deploy/dokploy/docker-compose.hub.yml", cwd=repo_root)
    except Exception:
        raise DokployUsageError("Cannot read Darkhub compose from origin/main") from None
    if result.returncode != 0 or not result.stdout:
        raise DokployUsageError("Cannot read Darkhub compose from origin/main")
    return result.stdout


def render_darkhub_compose(source: str, sha: str) -> str:
    """Bind both the Git build context and container environment to one SHA."""
    if not SHA_PATTERN.fullmatch(sha):
        raise DokployUsageError("Darkhub build requires a full origin/main SHA")
    context = "DarkFactory.git#${DARKFAC_GIT_SHA}"
    placeholder = "${DARKFAC_GIT_SHA}"
    if context not in source or source.count(placeholder) != 3 or "no_cache: true" not in source or "pull_policy: build" not in source:
        raise DokployUsageError("Darkhub compose lacks the pinned Git build and SHA contract")
    return source.replace(placeholder, sha)


def set_compose_git_sha(transport: Transport, service: Service, payload: Dict[str, Any], sha: str, source: str) -> None:
    """Install a raw compose file from main; preserve Dokploy's existing env."""
    if service.kind != "compose" or service.name.lower() != "darkhub":
        return
    if not isinstance(payload.get("env"), str):
        raise DokployUsageError("Darkhub compose.one has no string env; refusing to replace its settings")
    compose_file = render_darkhub_compose(source, sha)
    transport("POST", "/api/compose.update", {
        "composeId": service.service_id,
        "sourceType": "raw",
        "composePath": "docker-compose.yml",
        "composeFile": compose_file,
    })


def fetch_darkhub_health(url: str = DARKHUB_HEALTH_URL) -> Dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(request, timeout=5) as response:
        data = json.load(response)
    return data if isinstance(data, dict) else {}


def wait_for_darkhub_sha(
    expected_sha: str, *, health: Callable[[], Dict[str, Any]], timeout: float,
    interval: float, sleep_fn: Callable[[float], None], clock_fn: Callable[[], float],
) -> bool:
    deadline = clock_fn() + timeout
    while True:
        try:
            observed = health().get("git_sha")
            if isinstance(observed, str) and observed.lower() == expected_sha:
                return True
        except Exception:
            pass
        remaining = deadline - clock_fn()
        if remaining <= 0:
            return False
        sleep_fn(min(interval, remaining))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dokploy_redeploy.py",
        description=(
            "Redeploy every compose/application service of a Dokploy project/environment "
            f"(default: {DEFAULT_PROJECT}/{DEFAULT_ENVIRONMENT}) and wait for the result."
        ),
    )
    parser.add_argument(
        "--project",
        default=DEFAULT_PROJECT,
        help=(
            f"Dokploy project name (default: {DEFAULT_PROJECT}). Hard-allowlisted: only "
            f"{', '.join(sorted(ALLOWED_PROJECTS))} may be deployed by this tool."
        ),
    )
    parser.add_argument(
        "--environment", default=DEFAULT_ENVIRONMENT, help=f"Dokploy environment name (default: {DEFAULT_ENVIRONMENT})"
    )
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        metavar="NAME",
        help="Restrict to this service name (repeatable, case-insensitive). Default: all services.",
    )
    parser.add_argument("--list", action="store_true", help="List discovered services and exit; deploys nothing.")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would be deployed and exit; triggers nothing."
    )
    parser.add_argument(
        "--wait", dest="wait", action="store_true", default=True, help="Wait for each deployment to finish (default)."
    )
    parser.add_argument("--no-wait", dest="wait", action="store_false", help="Trigger deploys and exit without waiting.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-service wait timeout in seconds (default: {int(DEFAULT_TIMEOUT_SECONDS)}).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--skip-backup",
        action="store_true",
        help="Skip autonomous post-deploy 3-tier backup and restore drill.",
    )
    parser.add_argument(
        "--skip-node-sync",
        action="store_true",
        help="Skip the post-deploy 3-node verification/sync (used by node_sync itself to avoid re-entry).",
    )
    parser.add_argument(
        "--skip-check-main",
        action="store_true",
        help="Skip post-deploy check-main verification.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging.")
    return parser


def default_check_main_runner() -> Tuple[int, Dict[str, Any]]:
    """Probe base branch CI and open defect ticket if red (USR-85)."""
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from core.git.autonomy import run_check_main

        return run_check_main(open_ticket=True, notify=True)
    except Exception as exc:
        return 2, {"state": "unknown", "error": str(exc)}


def default_backup_runner(project_id: str = "darkfac") -> Dict[str, Any]:
    """Execute the real autonomous 3-tier backup cycle."""
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from core.infra.backup_cron import run_autonomous_backup_cycle

    return run_autonomous_backup_cycle(project_id=project_id)




def _print_service_list(transport: Transport, services: Sequence[Service], out: Any) -> None:
    header = f"{'NAME':<20} {'TYPE':<12} {'ID':<24} {'STATUS':<10} {'LAST_DEPLOY':<12} TITLE"
    print(header, file=out)
    for svc in services:
        try:
            payload = fetch_service_status(transport, svc)
            latest = latest_deployment(payload)
        except DokployUsageError as exc:
            print(
                f"{svc.name:<20} {svc.kind:<12} {svc.service_id:<24} {str(svc.status or ''):<10} "
                f"{'(error)':<12} {exc}",
                file=out,
            )
            continue
        last_status = latest.status if latest else "(none)"
        title = latest.title if latest else ""
        print(
            f"{svc.name:<20} {svc.kind:<12} {svc.service_id:<24} {str(svc.status or ''):<10} "
            f"{last_status:<12} {title}",
            file=out,
        )


def _resolve_services(
    transport: Transport, project: str, environment: str, only: Optional[Sequence[str]]
) -> List[Service]:
    check_project_allowed(project)  # defense in depth; main() already checked before this is reached
    projects = fetch_project_all(transport)
    services = normalize_project_services(projects, project, environment)
    if not services:
        raise DokployUsageError(
            f"No compose/application services found for project={project!r} environment={environment!r}. "
            "Check the names (project.all lists real values) or use --list on the unfiltered default project."
        )
    return select_services(services, only)


def _run_main(
    *,
    argv: Optional[Sequence[str]],
    env: Dict[str, str],
    registry_reader: Callable[[str], Optional[str]],
    transport_factory: Optional[Callable[[str, str], Transport]],
    health_client: Optional[Callable[[], Dict[str, Any]]],
    sleep_fn: Callable[[float], None],
    clock_fn: Callable[[], float],
    backup_runner: Optional[Callable[..., Any]],
    check_main_runner: Optional[Callable[[], Tuple[int, Dict[str, Any]]]],
    out: Any,
    err: Any,
) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(message)s")

    check_project_allowed(args.project)
    api_url, api_key = resolve_credentials(env, registry_reader)

    factory = transport_factory or make_urllib_transport
    transport = factory(api_url, api_key)

    selected = _resolve_services(transport, args.project, args.environment, args.only)

    if args.list:
        _print_service_list(transport, selected, out)
        return EXIT_OK

    if args.dry_run:
        print(
            f"Dry run: would deploy {len(selected)} service(s) in {args.project}/{args.environment}:",
            file=out,
            flush=True,
        )
        for svc in selected:
            print(f"  - {svc.name} ({svc.kind}, id={svc.service_id})", file=out, flush=True)
        return EXIT_OK

    local_subject = get_local_origin_main_subject()
    darkhub_selected = any(svc.name.lower() == "darkhub" and svc.kind == "compose" for svc in selected)
    expected_sha = get_origin_main_sha() if darkhub_selected else None
    if darkhub_selected and expected_sha is None:
        print("Cannot determine full origin/main SHA; Darkhub deploy refused", file=err, flush=True)
        return EXIT_DEPLOY_FAILED
    if darkhub_selected and not args.wait:
        print("Darkhub deploy requires --wait so /health can prove the origin/main SHA", file=err, flush=True)
        return EXIT_USAGE_ERROR
    darkhub_compose = None
    if darkhub_selected:
        try:
            darkhub_compose = get_origin_main_compose(expected_sha)
            render_darkhub_compose(darkhub_compose, expected_sha)
        except DokployUsageError as exc:
            print(f"Darkhub deploy refused: {exc}", file=err, flush=True)
            return EXIT_DEPLOY_FAILED
    if local_subject:
        print(f"Local origin/main HEAD subject: {local_subject}", file=out, flush=True)

    print(
        f"Triggering redeploy for {len(selected)} service(s) in {args.project}/{args.environment}...",
        file=out,
        flush=True,
    )

    all_ok = True
    other_failure = False
    node_sync_dirty = False
    triggered: List[Service] = []
    baselines: Dict[str, Optional[Deployment]] = {}
    for svc in selected:
        try:
            baseline_payload = fetch_service_status(transport, svc)
            baseline = latest_deployment(baseline_payload)
            if svc.name.lower() == "darkhub" and svc.kind == "compose":
                set_compose_git_sha(transport, svc, baseline_payload, expected_sha, darkhub_compose)
            trigger_deploy(transport, svc)
        except DokployUsageError as exc:
            print(f"[{svc.name}] FAILED to trigger deploy: {exc}", file=err, flush=True)
            all_ok = False
            other_failure = True
            continue
        print(f"[{svc.name}] deploy triggered ({svc.kind}, id={svc.service_id})", file=out, flush=True)
        triggered.append(svc)
        baselines[svc.name] = baseline

    if not args.wait:
        print("Not waiting (--no-wait): triggered deploy(s) may still be in progress on Dokploy.", file=out, flush=True)
        return EXIT_OK if all_ok else EXIT_DEPLOY_FAILED

    completed: List[ServiceWaitResult] = []
    for svc in triggered:
        result = wait_for_service(
            transport,
            svc,
            baselines.get(svc.name),
            args.timeout,
            poll_interval_seconds=args.poll_interval,
            sleep_fn=sleep_fn,
            clock_fn=clock_fn,
        )
        completed.append(result)
        # Git-sourced deployments are titled with the full commit message;
        # compare and print only its subject line.
        title = result.deployment.title.splitlines()[0] if result.deployment and result.deployment.title else "(unknown)"
        match_note = ""
        if local_subject and result.deployment and result.deployment.title:
            match_note = (
                " [matches local origin/main]"
                if title.strip() == local_subject.strip()
                else " [does not match local origin/main]"
            )
        print(
            f"[{svc.name}] status={result.outcome} title={title!r} elapsed={result.elapsed_seconds:.1f}s{match_note}",
            file=out,
            flush=True,
        )
        if result.outcome != WaitOutcome.DONE:
            all_ok = False
            other_failure = True

    if darkhub_selected and any(svc.name.lower() == "darkhub" for svc in triggered):
        if wait_for_darkhub_sha(
            expected_sha, health=health_client or fetch_darkhub_health, timeout=args.timeout,
            interval=args.poll_interval, sleep_fn=sleep_fn, clock_fn=clock_fn,
        ):
            print(f"[NODE SYNC] VPS: converged sha={expected_sha}", file=out, flush=True)
        else:
            print(f"[NODE SYNC] VPS: divergent; /health did not report origin/main sha={expected_sha}", file=err, flush=True)
            all_ok = False
            other_failure = True

    # A completed Dokploy job is not evidence that all running nodes use main.
    # Converge and verify the three execution nodes (Notebook ff-only when
    # clean on main, Desktop update+restart, VPS = the deploy just done). The
    # sync must not trigger another redeploy, and node_sync's own redeploy
    # passes --skip-node-sync, so there is no re-entry loop.
    if getattr(args, "skip_node_sync", False):
        print("[NODE SYNC] skipped (--skip-node-sync)", file=out, flush=True)
    else:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from core.infra import node_sync

        sync_kwargs: Dict[str, Any] = {
            "desktop_url": env.get("DARKFAC_DESKTOP_URL") or node_sync.DESKTOP_URL,
            "vps_url": env.get("DARKFAC_VPS_HEALTH_URL") or node_sync.VPS_URL,
            "token": env.get("DARKFAC_WORKER_TOKEN"),
            "vps_redeploy": lambda: True,  # VPS was just deployed above; never redeploy again from here
            "clock": clock_fn,
            "sleep": sleep_fn,
        }
        node_sync_dirty = False
        sync_report = node_sync.sync(**sync_kwargs)
        for node in sync_report.nodes:
            if node.state == "converged":
                continue
            all_ok = False
            if getattr(node, "sync_classification", None) == "dirty" or "dirty worktree" in (node.reason or ""):
                node_sync_dirty = True
            else:
                other_failure = True
            print(
                f"[NODE SYNC] {node.name}: {node.state} "
                f"sha={node.git_sha or '(unknown)'} expected={sync_report.expected_sha or '(unknown)'} "
                f"reason={node.reason or '(none)'}",
                file=err,
                flush=True,
            )
    if local_subject:
        for svc in triggered:
            result = next((r for r in completed if r.service.name == svc.name), None)
            title = ""
            if result is not None and result.deployment is not None and result.deployment.title:
                title = result.deployment.title.splitlines()[0].strip()
            matches = title == local_subject.strip()
            if svc.name in CODE_BEARING_SERVICES:
                # Weaker than a runtime SHA, so a missing/different title fails closed.
                if not matches:
                    all_ok = False
                    other_failure = True
                    print(f"[NODE SYNC] VPS service {svc.name}: does not match local origin/main", file=err, flush=True)
            elif not matches:
                print(f"[{svc.name}] info: deploy title {title!r} is not a commit subject (not code-bearing; ignored)", file=out, flush=True)

    # Post-deploy check-main verification (USR-85 scope in USR-72)
    if getattr(args, "skip_check_main", False):
        print("[CHECK-MAIN] skipped (--skip-check-main)", file=out, flush=True)
    elif check_main_runner is not None:
        try:
            cm_code, cm_payload = check_main_runner()
            cm_state = cm_payload.get("state", "unknown") if isinstance(cm_payload, dict) else "unknown"
            print(f"[CHECK-MAIN] state={cm_state} code={cm_code} payload={json.dumps(cm_payload)}", file=out, flush=True)
        except Exception as exc:
            print(f"[CHECK-MAIN] failed: {exc}", file=err, flush=True)
    elif not getattr(args, "skip_node_sync", False) and not args.list and not args.dry_run:
        try:
            cm_code, cm_payload = default_check_main_runner()
            cm_state = cm_payload.get("state", "unknown") if isinstance(cm_payload, dict) else "unknown"
            print(f"[CHECK-MAIN] state={cm_state} code={cm_code} payload={json.dumps(cm_payload)}", file=out, flush=True)
        except Exception as exc:
            print(f"[CHECK-MAIN] failed: {exc}", file=err, flush=True)

    if getattr(args, "skip_backup", False):
        print("[BACKUP] skipped: --skip-backup flag provided", file=out, flush=True)
    elif not all_ok:
        print("[BACKUP] skipped: deploy did not converge (all_ok is False)", file=out, flush=True)
    else:
        try:
            print("\n[AUTONOMOUS POST-DEPLOY BACKUP] Executing 3-tier backup & restore drill...", file=out, flush=True)
            runner = backup_runner or default_backup_runner
            summary = runner(project_id="darkfac")
            drill_v = summary.get("drill_verified", False) if isinstance(summary, dict) else True
            snap_id = summary.get("snapshot_id", "done") if isinstance(summary, dict) else "done"
            print(f"[BACKUP] ran: snapshot={snap_id}, drill_verified={drill_v}", file=out, flush=True)
            print(f"[AUTONOMOUS POST-DEPLOY BACKUP] Done: snapshot={snap_id}, drill_verified={drill_v}\n", file=out, flush=True)
        except Exception as exc:
            print(f"[BACKUP] failed: {exc}", file=err, flush=True)
            print(f"[AUTONOMOUS POST-DEPLOY BACKUP] Warning: autonomous backup cycle failed: {exc}", file=err, flush=True)

    if all_ok:
        return EXIT_OK
    if node_sync_dirty and not other_failure:
        return EXIT_NODE_DIRTY
    return EXIT_DEPLOY_FAILED


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    env: Optional[Dict[str, str]] = None,
    registry_reader: Callable[[str], Optional[str]] = _read_windows_user_env,
    transport_factory: Optional[Callable[[str, str], Transport]] = None,
    health_client: Optional[Callable[[], Dict[str, Any]]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
    backup_runner: Optional[Callable[..., Any]] = None,
    check_main_runner: Optional[Callable[[], Tuple[int, Dict[str, Any]]]] = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    env = dict(os.environ) if env is None else dict(env)

    if hasattr(out, "reconfigure"):
        try:
            out.reconfigure(line_buffering=True)
        except Exception:
            pass
    if hasattr(err, "reconfigure"):
        try:
            err.reconfigure(line_buffering=True)
        except Exception:
            pass

    try:
        return _run_main(
            argv=argv,
            env=env,
            registry_reader=registry_reader,
            transport_factory=transport_factory,
            health_client=health_client,
            sleep_fn=sleep_fn,
            clock_fn=clock_fn,
            backup_runner=backup_runner,
            check_main_runner=check_main_runner,
            out=out,
            err=err,
        )
    except DokployUsageError as exc:
        print(str(exc), file=err, flush=True)
        payload = {"error": str(exc), "error_type": "DokployUsageError", "exit_code": EXIT_USAGE_ERROR}
        print(f"[STRUCTURED_ERROR] {json.dumps(payload)}", file=err, flush=True)
        return EXIT_USAGE_ERROR
    except Exception as exc:
        print(f"Redeploy failed: {exc}", file=err, flush=True)
        payload = {"error": str(exc), "error_type": type(exc).__name__, "exit_code": EXIT_DEPLOY_FAILED}
        print(f"[STRUCTURED_ERROR] {json.dumps(payload)}", file=err, flush=True)
        return EXIT_DEPLOY_FAILED


if __name__ == "__main__":
    sys.exit(main())
