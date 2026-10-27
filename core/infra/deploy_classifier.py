"""USR-408: classify changed paths to decide whether a Hub merge needs an image rebuild.

The single source of "which paths build a Hub image" is `on.push.paths` of
.github/workflows/hub-image.yml (read by `hub_image_paths`). A merge touching none of them
must not rebuild or recreate the container. Fail closed (unknown or empty diff, or an
unreadable workflow => rebuild).
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Sequence

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "hub-image.yml"

REASON_NO_DIFF = "diff desconhecido"
REASON_RUNTIME = "toca caminho embutido na imagem"
REASON_DATA_ONLY = "somente caminhos fora da imagem (imagem inalterada)"
REASON_SAME_SHA = "sha ja implantado"


def _norm(path: str) -> str:
    p = path.strip().replace(chr(92), "/")
    return p[2:] if p.startswith("./") else p


def hub_image_paths(workflow: Optional[Path] = None) -> list[str]:
    """`on.push.paths` of hub-image.yml (unica fonte dos paths que geram imagem)."""
    try:
        lines = (workflow or WORKFLOW).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[str] = []
    in_paths = False
    for line in lines:
        stripped = line.strip()
        if stripped == "paths:":
            in_paths = True
        elif in_paths and stripped.startswith("- "):
            out.append(stripped[2:].strip().strip("\"'"))
        elif in_paths and stripped:
            break
    return out


def touches_hub_image(changed: Sequence[str], patterns: Sequence[str]) -> bool:
    """True quando algum path alterado casa com um padrao do hub-image.yml."""
    for raw in changed:
        path = _norm(raw)
        for pat in patterns:
            if pat.endswith("/**"):
                if path.startswith(pat[:-2]):
                    return True
            elif path == pat:
                return True
    return False


def runtime_paths(paths: Iterable[str]) -> list[str]:
    """Paths among ``paths`` that the Hub image embeds."""
    cleaned = [p for p in (_norm(raw) for raw in paths) if p]
    patterns = hub_image_paths()
    if not patterns:  # workflow unreadable: fail closed
        return cleaned
    return [p for p in cleaned if touches_hub_image([p], patterns)]


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
