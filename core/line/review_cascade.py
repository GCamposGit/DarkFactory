"""Review route cascade (USR-488): an independent reviewer when possible, a recorded contingency otherwise.

Levels, first available wins:
1. another family through a harness (``pick``);
2. another family through OpenRouter (``pick``; the registry role ``review.openrouter`` backs a Claude writer);
3. same family, another model (registry roles ``review.<harness>`` / ``planning.<harness>``);
4. same model, adversarial persona and a different temperature (recorded as unsupported when the CLI
   cannot set it, which never blocks).

Only when no level has a route does the caller wait. The review always runs as a fresh read-only agent
call over the diff and task context, so it never sees the implementer's session.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import Any, Callable, Collection, Iterable, Optional

logger = logging.getLogger(__name__)

LEVEL_OTHER_HARNESS = 1
LEVEL_OTHER_OPENROUTER = 2
LEVEL_OTHER_MODEL = 3
LEVEL_OTHER_PERSONA = 4
CONTINGENCY_LEVELS = (LEVEL_OTHER_MODEL, LEVEL_OTHER_PERSONA)

REASONS = {
    LEVEL_OTHER_HARNESS: "other_family_harness",
    LEVEL_OTHER_OPENROUTER: "other_family_openrouter",
    LEVEL_OTHER_MODEL: "same_family_other_model",
    LEVEL_OTHER_PERSONA: "same_model_adversarial_persona",
}
DEFAULT_PERSONA = "standard"
ADVERSARIAL_PERSONA = "adversarial_distinct"
ADVERSARIAL_TEMPERATURE = 0.7
ADVERSARIAL_PROMPT = (
    "PERSONA: you are an adversarial reviewer, distinct from whoever wrote this change. Assume the "
    "implementation is wrong until the diff and tests prove otherwise; look for contract breaks, "
    "unverified claims and missing negative cases before approving.\n\n"
)

PickFn = Callable[..., Optional[tuple[str, Optional[str]]]]


@dataclass(frozen=True)
class ReviewRoute:
    level: int
    harness: str
    model: Optional[str]
    reason: str
    persona: str = DEFAULT_PERSONA
    temperature: Optional[dict[str, Any]] = None

    @property
    def contingency(self) -> bool:
        return self.level in CONTINGENCY_LEVELS

    def record(self) -> dict[str, Any]:
        return asdict(self)


def valid_contingency(record: Any) -> bool:
    """A recorded contingency counts only with level 3 or 4 plus persona and reason."""
    return (isinstance(record, dict) and record.get("level") in CONTINGENCY_LEVELS
            and bool(record.get("persona")) and bool(record.get("reason")))


def _family(harness: str, model: Optional[str]) -> Optional[str]:
    from core.line.recovery_receipt import route_family

    try:
        return route_family(harness, model)
    except ValueError:
        return None


def _paid_independent(model: Optional[str], families: Collection[str], fallbacks: Iterable[str]) -> Optional[str]:
    from core.models.registry import resolve

    own = _family("openrouter", model) if model else None
    if own and own not in families:
        return model
    for candidate in fallbacks:
        try:
            resolved = resolve(candidate)
        except KeyError:
            continue
        family = _family("openrouter", resolved)
        if family and family not in families:
            return resolved
    return None


def _other_model(harness: str, writer_model: Optional[str]) -> Optional[str]:
    from core.models.registry import resolve

    for role in (f"review.{harness}", f"planning.{harness}"):
        try:
            candidate = resolve(role)
        except KeyError:
            continue
        if candidate != writer_model:
            return candidate
    return None


def select_review_route(
    pick: PickFn,
    caps: Iterable[str],
    *,
    implementing_harness: Optional[str],
    families: Collection[str],
    independent_caps: Optional[Iterable[str]] = None,
    exclude: Iterable[Any] = (),
    openrouter_fallbacks: Iterable[str] = ("review.openrouter", "distill.openrouter"),
    **pick_kwargs: Any,
) -> Optional[ReviewRoute]:
    """Walk the cascade. ``None`` only when no level has a route."""
    all_caps = list(caps)
    excluded = list(exclude)
    route = pick(
        "review", list(independent_caps if independent_caps is not None else all_caps),
        implementing_harness=implementing_harness, exclude=excluded, **pick_kwargs,
    )
    if route and route[0]:
        harness, model = route[0], (route[1] if len(route) > 1 else None)
        if harness == "openrouter":
            model = _paid_independent(model, families, openrouter_fallbacks)
            if model:
                return ReviewRoute(LEVEL_OTHER_OPENROUTER, harness, model, REASONS[LEVEL_OTHER_OPENROUTER])
        elif _family(harness, model) not in families:
            return ReviewRoute(LEVEL_OTHER_HARNESS, harness, model, REASONS[LEVEL_OTHER_HARNESS])

    from core.models.registry import resolve

    writer_model: Optional[str] = None
    if implementing_harness:
        try:
            writer_model = resolve(f"development.{implementing_harness}")
        except KeyError:
            writer_model = None
    tried = [*excluded, "openrouter"]
    fallback: Optional[tuple[str, Optional[str]]] = None
    for _ in range(4):
        found = pick("review", all_caps, implementing_harness=None, exclude=tried, **pick_kwargs)
        if not found or not found[0] or found[0] in tried:
            break
        harness, model = found[0], (found[1] if len(found) > 1 else None)
        tried.append(harness)
        alternative = _other_model(harness, writer_model) if harness == implementing_harness else model
        if alternative is None:
            fallback = fallback or (harness, model if model else writer_model)
            continue
        return ReviewRoute(LEVEL_OTHER_MODEL, harness, alternative, REASONS[LEVEL_OTHER_MODEL])
    if fallback is None:
        return None
    return ReviewRoute(
        LEVEL_OTHER_PERSONA, fallback[0], fallback[1], REASONS[LEVEL_OTHER_PERSONA],
        persona=ADVERSARIAL_PERSONA,
        temperature={"requested": ADVERSARIAL_TEMPERATURE, "applied": False, "reason": "unsupported_by_agent_cli"},
    )
