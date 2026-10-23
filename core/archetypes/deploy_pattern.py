"""Zero-downtime deploy pattern for every new factory product (USR-360).

Generates, parameterised by the app name, the same pattern the Hub uses
(``core/orchestrator/bluegreen.py``, ``.github/workflows/hub-image.yml``):

* a GHCR image workflow tagged by commit sha;
* a blue-green compose behind Traefik (both colours share router/service labels, so Traefik routes
  only to a ``healthy`` container; the old colour is removed only after the new one is healthy),
  using the same ``# darkfac-bluegreen:`` state marker as the Hub;
* the acceptance criterion (1 s probe during a real deploy) for the PRD/handoff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from core.orchestrator.bluegreen import COLORS, BlueGreenState
from core.orchestrator.compose_render import SHA_PATTERN, ComposeRenderError

DEPLOY_TARGETS = ("dokploy", "ghcr_bluegreen")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")

PROBE_ACCEPTANCE_CRITERION = (
    "Deploy sem queda: durante um deploy real (blue-green, troca so apos healthcheck), a sonda externa de "
    "1 s em /health fica com non200 = 0 e observa os dois SHAs (antigo e novo). Prova simulada ou falha "
    "de pull nao conta como aceite (ver docs/proposals/USR-207-zero-downtime.md)."
)


@dataclass(frozen=True)
class DeploySpec:
    app_name: str
    host: str
    ghcr_owner: str
    port: int = 8000
    health_path: str = "/health"
    dockerfile: str = "Dockerfile"

    def __post_init__(self) -> None:
        if not _NAME_RE.fullmatch(self.app_name):
            raise ComposeRenderError("app_name must be lowercase letters, digits and hyphens")
        if not _OWNER_RE.fullmatch(self.ghcr_owner):
            raise ComposeRenderError("invalid GHCR owner")
        if not _HOST_RE.fullmatch(self.host):
            raise ComposeRenderError("invalid host")
        if not 1 <= self.port <= 65535:
            raise ComposeRenderError("invalid port")
        if not self.health_path.startswith("/") or any(c in self.health_path for c in " '\"`\\"):
            raise ComposeRenderError("invalid health path")

    @property
    def image(self) -> str:
        return f"ghcr.io/{self.ghcr_owner.lower()}/{self.app_name}"


def render_image_workflow(spec: DeploySpec) -> str:
    return f"""name: {spec.app_name} image (GHCR)

# Builds on GitHub and publishes {spec.image}:<sha>; the deploy waits for this tag (USR-360).
on:
  push:
    branches: [main]

permissions:
  contents: read
  packages: write

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{{{ github.actor }}}}
          password: ${{{{ secrets.GITHUB_TOKEN }}}}
      - uses: docker/build-push-action@v6
        with:
          context: .
          file: {spec.dockerfile}
          push: true
          build-args: |
            GIT_SHA=${{{{ github.sha }}}}
          tags: |
            {spec.image}:${{{{ github.sha }}}}
            {spec.image}:main
          cache-from: type=gha
          cache-to: type=gha,mode=max
"""


def _service(spec: DeploySpec, color: str, sha: str, replicas: int) -> str:
    name = spec.app_name
    probe = (
        "import urllib.request; "
        f"urllib.request.urlopen('http://localhost:{spec.port}{spec.health_path}', timeout=4)"
    )
    return f"""  {name}-{color}:
    image: {spec.image}:{sha}
    pull_policy: always
    restart: unless-stopped
    # colour={color} sha={sha[:12]}
    stop_grace_period: 20s
    healthcheck:
      test: ["CMD", "python", "-c", "{probe}"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 20s
    environment:
      - PORT={spec.port}
      - GIT_SHA={sha}
    networks:
      - dokploy-network
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.{name}.rule=Host(`{spec.host}`)"
      - "traefik.http.routers.{name}.entrypoints=websecure"
      - "traefik.http.routers.{name}.tls.certresolver=letsencrypt"
      - "traefik.http.services.{name}.loadbalancer.server.port={spec.port}"
    deploy:
      replicas: {replicas}
"""


def render_product_bluegreen_compose(
    spec: DeploySpec, state: BlueGreenState, running: tuple[str, ...]
) -> str:
    """Two-service compose; ``running`` lists the colours with ``replicas: 1`` (others get 0)."""
    if state.active not in COLORS:
        raise ComposeRenderError("active colour must be 'blue' or 'green'")
    for color in COLORS:
        if not SHA_PATTERN.fullmatch(state.sha_of(color)):
            raise ComposeRenderError("blue-green state requires full 40-character shas")
    marker = f"# darkfac-bluegreen: active={state.active} blue={state.blue_sha} green={state.green_sha}\n"
    services = "".join(_service(spec, c, state.sha_of(c), 1 if c in running else 0) for c in COLORS)
    return f"{marker}services:\n{services}\nnetworks:\n  dokploy-network:\n    external: true\n"


def render_initial_compose(spec: DeploySpec) -> str:
    """First compose of a new product: both colours point at the placeholder sha, blue runs."""
    seed = "0" * 40
    state = BlueGreenState(active="blue", blue_sha=seed, green_sha=seed)
    return render_product_bluegreen_compose(spec, state, ("blue",))


def write_deploy_pattern(target: Path, spec: DeploySpec) -> list[str]:
    """Write the workflow, compose and criterion into a new product tree; return relative paths."""
    files = {
        ".github/workflows/image.yml": render_image_workflow(spec),
        "deploy/docker-compose.bluegreen.yml": render_initial_compose(spec),
        "docs/DEPLOY_ZERO_DOWNTIME.md": (
            f"# Deploy sem queda ({spec.app_name})\n\nCriterio de aceite obrigatorio:\n\n"
            f"- {PROBE_ACCEPTANCE_CRITERION}\n"
        ),
    }
    created: list[str] = []
    for rel, content in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created.append(rel)
    return created
