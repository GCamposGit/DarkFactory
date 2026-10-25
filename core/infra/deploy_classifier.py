"""USR-207: classify changed paths to decide whether a Hub merge needs an image rebuild.

The Hub image (deploy/dokploy/Dockerfile.hub) embeds core/, hub/, scripts/, deploy/,
requirements.txt and harness.config.json at runtime. Everything else (ledger under
.factory/, docs/, tests/, CI config, root markdown) does not change the running Hub,
so a merge that touches only those paths must not rebuild or recreate the container.
Pure functions only; fail closed (unknown or empty diff => rebuild).
"""

from __future__ import annotations

from typing import Iterable

REBUILD_PREFIXES: tuple[str, ...] = ("core/", "hub/", "scripts/", "deploy/", ".factory/config/")
# USR-397: .factory/config/ is read at runtime (prices, routing, model registry); the ledger
# (.factory/demands, .factory/owner_actions) stays data-only.
# Keep this aligned with COPY sources in deploy/dokploy/Dockerfile.hub. In particular,
# run_hub.py is not copied into the image and therefore cannot require a Hub rebuild.
REBUILD_FILES: frozenset[str] = frozenset({"requirements.txt", "harness.config.json"})

REASON_NO_DIFF = "diff desconhecido"
REASON_RUNTIME = "toca caminho embutido na imagem"
REASON_DATA_ONLY = "somente caminhos fora da imagem (imagem inalterada)"
REASON_SAME_SHA = "sha ja implantado"


def _norm(path: str) -> str:
    p = path.strip().replace(chr(92), "/")
    return p[2:] if p.startswith("./") else p


def runtime_paths(paths: Iterable[str]) -> list[str]:
    """Paths among ``paths`` that the Hub image embeds."""
    out: list[str] = []
    for raw in paths:
        p = _norm(raw)
        if not p:
            continue
        if p in REBUILD_FILES or p.startswith(REBUILD_PREFIXES):
            out.append(p)
    return out


def needs_rebuild(changed_paths: Iterable[str] | None) -> bool:
    """True when the image must be rebuilt. ``None``/empty list => True (fail closed)."""
    if changed_paths is None:
        return True
    cleaned = [p for p in (_norm(x) for x in changed_paths) if p]
    if not cleaned:
        return True
    return bool(runtime_paths(cleaned))


def classify(deployed_sha: str | None, target_sha: str, changed_paths: Iterable[str] | None) -> tuple[bool, str]:
    """Return (rebuild, reason) for moving the Hub from ``deployed_sha`` to ``target_sha``."""
    if deployed_sha and deployed_sha.lower() == target_sha.lower():
        return False, REASON_SAME_SHA
    if changed_paths is None:
        return True, REASON_NO_DIFF
    paths = [p for p in (_norm(x) for x in changed_paths) if p]
    if not paths:
        return True, REASON_NO_DIFF
    if runtime_paths(paths):
        return True, REASON_RUNTIME
    return False, REASON_DATA_ONLY
