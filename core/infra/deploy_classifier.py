"""USR-408 / USR-515: classify changed paths to decide whether a Hub merge needs an image.

The single source of "which paths build a Hub image" is `on.push.paths` of
.github/workflows/hub-image.yml (read by `hub_image_paths`). A merge touching none of them
must not rebuild or recreate the container. Fail closed (unknown or empty diff, or an
unreadable workflow => rebuild).

`classify_image_lag` (USR-515) is pure: it separates the tip commit from the accumulated
lag between the live SHA and the target, and it never calls GHCR, Dokploy or git. A pin
is either the target (the workflow publishes that SHA) or the latest ancestor the workflow
already publishes. A SHA that does not strictly advance live is never chosen.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Optional, Sequence

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "hub-image.yml"

REASON_NO_DIFF = "diff desconhecido"
REASON_RUNTIME = "toca caminho embutido na imagem"
REASON_DATA_ONLY = "somente caminhos fora da imagem (imagem inalterada)"
REASON_SAME_SHA = "sha ja implantado"
REASON_NO_IMAGE_LAG = "intervalo sem caminho de imagem; nada a implantar"
REASON_PIN_PUBLISHED = "ultimo merge fora da imagem; pin no ultimo SHA publicado pelo workflow"
REASON_PIN_TARGET = "alvo toca caminho de imagem"
REASON_LIVE_ABSENT = "live ausente; nenhum pin avanca o ambiente"
REASON_NOT_AHEAD = "alvo nao e descendente do live; pin recusado"
REASON_UNPROVEN = "ancestralidade nao comprovada; pin recusado"

ACTION_SKIP = "skip"
ACTION_PIN_PUBLISHED = "pin_published"
ACTION_PIN_TARGET = "pin_target"
ACTION_HOLD = "hold"

ImageAction = Literal["skip", "pin_published", "pin_target", "hold"]


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


def _sha(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower()
    return text or None


@dataclass(frozen=True, slots=True)
class CommitTouch:
    """One commit on the first-parent line (oldest first) and the paths that commit itself changed."""

    sha: str
    paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        sha = (self.sha or "").strip().lower()
        seen: set[str] = set()
        paths: list[str] = []
        for raw in self.paths:
            path = _norm(raw)
            if path and path not in seen:
                seen.add(path)
                paths.append(path)
        object.__setattr__(self, "sha", sha)
        object.__setattr__(self, "paths", tuple(paths))


@dataclass(frozen=True, slots=True)
class ImageLagDecision:
    """Actionable image choice: skip, pin a published ancestor, or pin the target.

    ``published_sha`` is the image identity to keep or to roll forward to.
    ``reach`` is the paths changed strictly after that SHA and at or before the target:
    the lag the chosen image does not cover. ``pin_sha`` is set only when the action
    deploys a SHA that strictly advances live.
    """

    action: ImageAction
    published_sha: str | None
    reach: tuple[str, ...]
    reason: str
    live_sha: str | None = None
    target_sha: str = ""

    @property
    def pin_sha(self) -> str | None:
        if self.action in (ACTION_PIN_PUBLISHED, ACTION_PIN_TARGET):
            return self.published_sha
        return None


def _find_sha(commits: Sequence[CommitTouch], sha: str) -> int | None:
    for index, commit in enumerate(commits):
        if commit.sha == sha:
            return index
    return None


def _collect_paths(commits: Sequence[CommitTouch]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for commit in commits:
        for path in commit.paths:
            if path not in seen:
                seen.add(path)
                out.append(path)
    return tuple(out)


def _publishes_image(paths: Sequence[str], patterns: Sequence[str]) -> bool:
    """True when this commit's own diff is one ``hub-image.yml`` publishes.

    An unreadable workflow (no patterns) fails closed: any non-empty diff publishes.
    An empty diff does not.
    """
    if not paths:
        return False
    if not patterns:
        return True
    return touches_hub_image(paths, patterns)


def _decision(
    action: ImageAction,
    published_sha: str | None,
    reach: tuple[str, ...],
    reason: str,
    live_sha: str | None,
    target_sha: str,
) -> ImageLagDecision:
    return ImageLagDecision(
        action=action,
        published_sha=published_sha,
        reach=reach,
        reason=reason,
        live_sha=live_sha,
        target_sha=target_sha,
    )


def classify_image_lag(
    *,
    live_sha: str | None,
    target_sha: str,
    commits: Sequence[CommitTouch],
) -> ImageLagDecision:
    """Choose skip, pin the latest published ancestor, or pin the target.

    ``commits`` is the first-parent line, oldest first, and must contain ``live_sha``
    strictly before ``target_sha``. Only commits in ``(live, target]`` are eligible.
    Image membership comes from ``hub_image_paths()``; this function does not query
    GHCR or Dokploy and does not run git.

    The tip commit is classified on its own. When it publishes an image, the pin is
    the target. When it does not, the pin is the latest image commit strictly after
    live and strictly before the target (ancestor of the target, descendant of live).
    No other SHA is selected: a commit behind live, live itself, or a missing live
    never becomes the pin.
    """
    target = _sha(target_sha) or ""
    live = _sha(live_sha)
    line = tuple(commits)
    if live is None:
        return _decision(ACTION_HOLD, None, (), REASON_LIVE_ABSENT, None, target)
    if not target:
        return _decision(ACTION_HOLD, None, (), REASON_UNPROVEN, live, "")

    live_at = _find_sha(line, live)
    target_at = _find_sha(line, target)
    if live_at is None or target_at is None:
        return _decision(ACTION_HOLD, None, (), REASON_UNPROVEN, live, target)
    if target_at < live_at:
        return _decision(ACTION_HOLD, live, (), REASON_NOT_AHEAD, live, target)
    if target_at == live_at:
        return _decision(ACTION_SKIP, live, (), REASON_SAME_SHA, live, target)

    patterns = hub_image_paths()
    window = line[live_at + 1 : target_at + 1]
    if _publishes_image(window[-1].paths, patterns):
        return _decision(ACTION_PIN_TARGET, target, (), REASON_PIN_TARGET, live, target)

    published_at: int | None = None
    for index, commit in enumerate(window[:-1]):
        if _publishes_image(commit.paths, patterns):
            published_at = index
    if published_at is None:
        return _decision(ACTION_SKIP, live, _collect_paths(window), REASON_NO_IMAGE_LAG, live, target)

    chosen = window[published_at]
    reach = _collect_paths(window[published_at + 1 :])
    return _decision(ACTION_PIN_PUBLISHED, chosen.sha, reach, REASON_PIN_PUBLISHED, live, target)
