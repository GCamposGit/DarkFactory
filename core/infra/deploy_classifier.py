"""Classify changed paths to decide whether a Hub merge needs an image rebuild.

``DOCKERFILE_RUNTIME_COPY_PREFIXES`` and ``DOCKERFILE_RUNTIME_COPY_FILES`` are the
explicit mirror of runtime inputs copied by deploy/dokploy/Dockerfile.hub. Static
tests keep that mirror and the GHCR workflow trigger aligned with the Dockerfile.
The Dockerfile also copies docs/ and .factory/ as image metadata/seeds; those paths
are deliberately excluded because they do not change the running Hub. Pure
functions only; fail closed (unknown or empty diff => rebuild).
"""

from __future__ import annotations

from typing import Iterable

DOCKERFILE_RUNTIME_COPY_PREFIXES: tuple[str, ...] = (
    "core/",
    "hub/",
    "scripts/",
    "deploy/",
)
DOCKERFILE_RUNTIME_COPY_FILES: frozenset[str] = frozenset(
    {"requirements.txt", "harness.config.json"}
)
# Compatibility names used by deploy planning and external callers.
REBUILD_PREFIXES = DOCKERFILE_RUNTIME_COPY_PREFIXES
REBUILD_FILES = DOCKERFILE_RUNTIME_COPY_FILES

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
        if p in DOCKERFILE_RUNTIME_COPY_FILES or p.startswith(DOCKERFILE_RUNTIME_COPY_PREFIXES):
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
