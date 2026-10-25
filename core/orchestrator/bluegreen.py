"""Blue-green deploy of the DarkHub compose behind Traefik, no Swarm (USR-339, OA-057 option A).

Design (see docs/proposals/USR-207-zero-downtime.md section 2.2):

* The raw compose has two services, ``darkhub-blue`` and ``darkhub-green``. Both carry the SAME
  Traefik router/service labels, so Traefik load-balances them as one service and, because the
  containers have a Docker healthcheck, only routes to a container that is ``healthy``.
* The inactive colour is kept at ``deploy.replicas: 0`` (no container). A deploy runs two phases:
  1. ``overlap``: render the inactive colour with the new sha and ``replicas: 1``; the active colour
     is untouched, so ``docker compose up`` only creates the new container. Wait until ``/health``
     answers the new sha (Traefik only routes to it once healthy).
  2. ``cutover``: render with the new colour active and the old colour at ``replicas: 0``; compose
     removes only the old container. The new one stays untouched.
* State lives in the rendered compose itself (a ``# darkfac-bluegreen:`` marker line that is stored
  in Dokploy), so the next deploy reads it back through the Dokploy API. No local file.
* If the new colour never reports the expected sha, the previous compose is restored (the old
  colour never stopped) and :class:`BlueGreenError` is raised.
"""

from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from core.orchestrator.compose_render import (
    IMAGE_SOURCE_GHCR,
    SHA_PATTERN,
    ComposeRenderError,
    render_darkhub_compose,
)

logger = logging.getLogger(__name__)

COLORS = ("blue", "green")
_MARKER_RE = re.compile(
    r"^# darkfac-bluegreen: active=(?P<active>blue|green) blue=(?P<blue>[0-9a-f]{40}) green=(?P<green>[0-9a-f]{40})\s*$",
    re.M,
)
_SERVICE_RE = re.compile(r"^  darkhub:\n(?P<body>(?:(?:    .*)?\n)+?)(?=^\S)", re.M)
_HEALTH_RETRIES = 3
LEGACY_CONTAINER = "darkhub-cloud-247"
MODE_ENV = "DARKHUB_DEPLOY_MODE"


def is_bluegreen(compose_file: Optional[str], env: Optional[Mapping[str, str]] = None) -> bool:
    """True when the stored compose is blue-green or ``DARKHUB_DEPLOY_MODE=bluegreen`` is set."""
    mode = ((env if env is not None else os.environ).get(MODE_ENV) or "").strip().lower()
    return mode == "bluegreen" or parse_state(compose_file) is not None


class BlueGreenError(RuntimeError):
    """The blue-green switch failed; the previous colour was kept."""


@dataclass(frozen=True)
class BlueGreenState:
    active: str
    blue_sha: str
    green_sha: str

    def sha_of(self, color: str) -> str:
        return self.blue_sha if color == "blue" else self.green_sha


def other_color(color: str) -> str:
    return "green" if color == "blue" else "blue"


def parse_state(compose_file: Optional[str]) -> Optional[BlueGreenState]:
    """State recorded by a previous blue-green render, or ``None`` (legacy single-service compose)."""
    match = _MARKER_RE.search(compose_file or "")
    if not match:
        return None
    return BlueGreenState(match["active"], match["blue"], match["green"])


def _service_block(rendered: str, color: str, sha: str, replicas: int) -> str:
    match = _SERVICE_RE.search(rendered)
    if not match:
        raise ComposeRenderError("Darkhub compose has no 'darkhub' service block to split")
    body = match["body"]
    if "    deploy:\n" not in body:
        raise ComposeRenderError("Darkhub compose service lacks a deploy block")
    body = body.replace("    deploy:\n", f"    deploy:\n      replicas: {replicas}\n", 1)
    body = body.replace("    restart: unless-stopped\n", f"    restart: unless-stopped\n    # colour={color} sha={sha[:12]}\n", 1)
    return f"  darkhub-{color}:\n{body}"


def render_bluegreen_compose(source: str, state: BlueGreenState, running: tuple[str, ...]) -> str:
    """Two-service compose. ``running`` lists the colours with ``replicas: 1`` (others get 0)."""
    if state.active not in COLORS:
        raise ComposeRenderError("active colour must be 'blue' or 'green'")
    for color in COLORS:
        if not SHA_PATTERN.fullmatch(state.sha_of(color)):
            raise ComposeRenderError("blue-green state requires full 40-character shas")
    base = render_darkhub_compose(source, state.blue_sha, IMAGE_SOURCE_GHCR)
    match = _SERVICE_RE.search(base)
    if not match:
        raise ComposeRenderError("Darkhub compose has no 'darkhub' service block to split")
    blocks = []
    for color in COLORS:
        rendered = render_darkhub_compose(source, state.sha_of(color), IMAGE_SOURCE_GHCR)
        blocks.append(_service_block(rendered, color, state.sha_of(color), 1 if color in running else 0))
    marker = (
        f"# darkfac-bluegreen: active={state.active} blue={state.blue_sha} green={state.green_sha}\n"
    )
    return marker + base[: match.start()] + "".join(blocks) + base[match.end():]


def plan_next(current: Optional[BlueGreenState], new_sha: str) -> tuple[str, BlueGreenState]:
    """(colour to start, state with that colour carrying ``new_sha``). Legacy compose => start blue."""
    if not SHA_PATTERN.fullmatch(new_sha):
        raise BlueGreenError("a full 40-character sha is required")
    if current is None:
        return "blue", BlueGreenState(active="green", blue_sha=new_sha, green_sha=new_sha)
    target = other_color(current.active)
    if target == "blue":
        return target, BlueGreenState(current.active, new_sha, current.green_sha)
    return target, BlueGreenState(current.active, current.blue_sha, new_sha)


def run_bluegreen_deploy(
    *,
    source: str,
    new_sha: str,
    read_compose: Callable[[], Optional[str]],
    apply_compose: Callable[[str], None],
    sha_is_serving: Callable[[str], bool],
    wait_ghcr: Optional[Callable[[str], None]] = None,
    health_attempts: int = 30,
    sleep_fn: Callable[[float], None] = lambda _s: None,
    interval: float = 5.0,
    remove_legacy: Optional[Callable[[], bool]] = None,
    restore_allowed: Optional[Callable[[str], bool]] = None,
) -> BlueGreenState:
    """Run overlap -> health gate -> cutover. Returns the final state.

    On failure the previous compose is restored only if ``restore_allowed(previous)`` accepts it;
    otherwise both colours are scaled to zero (the Hub goes offline instead of reviving a version
    the caller refuses, e.g. one older than a security fix).

    ``apply_compose`` installs the compose in Dokploy and waits for ``compose up`` to finish.
    ``sha_is_serving(sha)`` is True only after ``_HEALTH_RETRIES`` consecutive probes of the public
    ``/health`` returned ``sha`` (Traefik only routes to healthy containers, so the new sha can only
    be observed once the new colour is healthy).
    """
    previous_compose = read_compose()
    current = parse_state(previous_compose)
    target, planned = plan_next(current, new_sha)
    if current is not None and current.sha_of(current.active) == new_sha:
        return current
    if wait_ghcr is not None:
        wait_ghcr(new_sha)
    if current is None:
        # Legacy single-service compose: the old container keeps serving until the first overlap
        # completes; it is removed by compose when the 'darkhub' service disappears at cutover.
        overlap_state = BlueGreenState("green", new_sha, new_sha)
        running: tuple[str, ...] = (target,)
    else:
        overlap_state = planned
        running = COLORS
    try:
        apply_compose(render_bluegreen_compose(source, overlap_state, running))
        for _ in range(health_attempts):
            if sha_is_serving(new_sha):
                break
            sleep_fn(interval)
        else:
            raise BlueGreenError(
                f"colour {target} did not serve sha {new_sha[:12]} within the health window"
            )
        final = BlueGreenState(target, planned.blue_sha, planned.green_sha)
        if current is None:
            final = BlueGreenState(target, new_sha, new_sha)
            if remove_legacy is not None:
                try:
                    if not remove_legacy():
                        logger.warning("blue-green: legacy container %s was not removed", LEGACY_CONTAINER)
                except Exception as exc:  # noqa: BLE001 - best effort, new colour is already healthy
                    logger.warning("blue-green: legacy removal failed: %s", type(exc).__name__)
        apply_compose(render_bluegreen_compose(source, final, (target,)))
    except Exception as exc:  # noqa: BLE001 - any failure after the first apply restores the previous compose
        logger.error("blue-green: deploy of %s failed (%s); restoring previous compose", new_sha[:12], exc)
        if previous_compose and restore_allowed is not None and not restore_allowed(previous_compose):
            try:
                apply_compose(render_bluegreen_compose(source, planned, ()))
                restore_note = "previous compose refused; both colours scaled to zero (Hub offline)"
            except Exception as offline_exc:  # noqa: BLE001
                restore_note = f"previous compose refused; OFFLINE FAILED ({offline_exc})"
            raise BlueGreenError(f"{exc}; {restore_note}") from exc
        if previous_compose:
            try:
                apply_compose(previous_compose)
                restore_note = "previous compose restored"
            except Exception as restore_exc:  # noqa: BLE001
                logger.error("blue-green: restoring the previous compose failed: %s", restore_exc)
                restore_note = f"RESTORE FAILED ({restore_exc})"
        else:
            restore_note = "no previous compose to restore"
        raise BlueGreenError(f"{exc}; {restore_note}") from exc
    return final


DokployRequest = Callable[[str, str, Optional[dict]], Any]


def run_bluegreen_via_dokploy(
    *,
    request: DokployRequest,
    compose_id: str,
    source: str,
    new_sha: str,
    sha_is_serving: Callable[[str], bool],
    wait_ghcr: Optional[Callable[[str], None]] = None,
    deploy_timeout: float = 900.0,
    poll_interval: float = 5.0,
    sleep_fn: Callable[[float], None] = time.sleep,
    clock_fn: Callable[[], float] = time.monotonic,
    restore_allowed: Optional[Callable[[str], bool]] = None,
) -> BlueGreenState:
    """Blue-green over the Dokploy API (``request(method, path, json_body) -> parsed JSON``)."""
    query = urllib.parse.urlencode({"composeId": compose_id})

    def one() -> dict:
        data = request("GET", f"/api/compose.one?{query}", None)
        return data if isinstance(data, dict) else {}

    def newest(payload: dict) -> Optional[tuple]:
        deployments = payload.get("deployments")
        if isinstance(deployments, list) and deployments and isinstance(deployments[0], dict):
            d = deployments[0]
            return (d.get("deploymentId") or d.get("createdAt"), str(d.get("status") or ""))
        return None

    def read_compose() -> Optional[str]:
        value = one().get("composeFile")
        return value if isinstance(value, str) else None

    def apply_compose(compose_file: str) -> None:
        baseline = newest(one())
        request("POST", "/api/compose.update", {
            "composeId": compose_id, "sourceType": "raw",
            "composePath": "docker-compose.yml", "composeFile": compose_file,
        })
        request("POST", "/api/compose.deploy", {"composeId": compose_id})
        deadline = clock_fn() + deploy_timeout
        while True:
            current = newest(one())
            if current is not None and (baseline is None or current[0] != baseline[0]):
                if current[1] == "done":
                    return
                if current[1] in ("error", "failed"):
                    raise BlueGreenError("Dokploy compose up failed")
            if clock_fn() >= deadline:
                raise BlueGreenError("Dokploy compose up timed out")
            sleep_fn(poll_interval)

    def remove_legacy() -> bool:
        try:
            request("POST", "/api/docker.removeContainer", {"containerId": LEGACY_CONTAINER})
            return True
        except Exception:  # noqa: BLE001 - endpoint may not exist in this Dokploy version
            return False

    return run_bluegreen_deploy(
        source=source, new_sha=new_sha, read_compose=read_compose, apply_compose=apply_compose,
        sha_is_serving=sha_is_serving, wait_ghcr=wait_ghcr, sleep_fn=sleep_fn,
        remove_legacy=remove_legacy, restore_allowed=restore_allowed,
        health_attempts=60,  # ~5 min: a cold Hub pulls the image and refreshes benchmarks before healthy
    )


__all__ = [
    "is_bluegreen",
    "run_bluegreen_via_dokploy",
    "LEGACY_CONTAINER",
    "BlueGreenError",
    "BlueGreenState",
    "COLORS",
    "other_color",
    "parse_state",
    "plan_next",
    "render_bluegreen_compose",
    "run_bluegreen_deploy",
]
