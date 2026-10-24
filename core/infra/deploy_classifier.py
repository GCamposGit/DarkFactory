"""USR-207: classify changed paths to decide whether a Hub merge needs an image rebuild.

The Hub image (deploy/dokploy/Dockerfile.hub) embeds core/, hub/, scripts/, deploy/,
requirements.txt and harness.config.json at runtime. Everything else (ledger under
.factory/, docs/, tests/, CI config, root markdown) does not change the running Hub,
so a merge that touches only those paths must not rebuild or recreate the container.
Pure functions only; fail closed (unknown or empty diff => rebuild).
"""

from __future__ import annotations

import fnmatch
import re
import shlex
from pathlib import Path
from typing import Iterable

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "deploy" / "dokploy" / "Dockerfile.hub"
_SEED_ONLY_PREFIXES = ("docs/", ".factory/")

REASON_NO_DIFF = "diff desconhecido"
REASON_RUNTIME = "toca caminho embutido na imagem"
REASON_DATA_ONLY = "somente caminhos fora da imagem (imagem inalterada)"
REASON_SAME_SHA = "sha ja implantado"


def _norm(path: str) -> str:
    p = path.strip().replace(chr(92), "/")
    return p[2:] if p.startswith("./") else p


def runtime_image_paths(dockerfile_path: Path | None = None) -> tuple[str, ...]:
    """Return canonical trigger patterns for sources copied as Hub runtime.

    The Dockerfile is the source of truth. ``docs/`` and ``.factory/`` are
    copied only as image seeds; they are read fresh from the mounted/runtime
    sources and do not require an image rebuild. Nested COPY directories are
    collapsed under their parent pattern (for example ``hub/data`` under
    ``hub/**``).
    """
    dockerfile = dockerfile_path or _DOCKERFILE
    sources: set[str] = set()
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*COPY\s+(.+?)\s+\S+\s*$", line, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            source = shlex.split(match.group(1))[0]
        except (ValueError, IndexError):
            continue
        source = _norm(source).rstrip("/")
        if (
            not source
            or source in {prefix.rstrip("/") for prefix in _SEED_ONLY_PREFIXES}
            or source.startswith(_SEED_ONLY_PREFIXES)
        ):
            continue
        sources.add(source)

    directories = {source for source in sources if (_REPO_ROOT / source).is_dir()}
    paths = {
        source if source not in directories else f"{source}/**"
        for source in sources
        if not any(source.startswith(f"{parent}/") for parent in directories if parent != source)
    }
    return tuple(sorted(paths))


def runtime_paths(paths: Iterable[str]) -> list[str]:
    """Return changed paths that match a Dockerfile-derived runtime pattern."""
    patterns = runtime_image_paths()
    return [
        path
        for raw in paths
        if (path := _norm(raw)) and any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)
    ]


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
