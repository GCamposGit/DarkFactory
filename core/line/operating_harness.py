"""Which harness the owner is operating the factory through right now (USR-109).

Owner decision: when the factory is driven from a specific harness (Claude Code, Codex, Grok Build or
Antigravity), that harness is the primary developer of the `development` stage as long as its quota is
above the critical floor. `core.line.routing.pick` asks this module for that harness; with no signal at all
(pump, worker, cloud) there is no operating harness and routing behaves exactly as before.

Source, in precedence order:

1. an explicit argument (`pick(operating_harness=...)`), handled by `resolve_operating_harness`;
2. the `DARKFAC_OPERATING_HARNESS` variable (`claude|codex|grok|antigravity`; invalid values are ignored);
3. autodetection from the variables the parent harness exports (`AUTODETECT_ENV_SIGNALS`);
4. nothing (`None`).

Autodetection never guesses: signals that point at more than one harness are ambiguous and yield `None`.
"""

from __future__ import annotations

import logging
import os
from typing import Mapping, Optional

logger = logging.getLogger(__name__)

OPERATING_HARNESS_ENV = "DARKFAC_OPERATING_HARNESS"
KNOWN_HARNESSES: tuple[str, ...] = ("claude", "codex", "grok", "antigravity")

# (environment variable, harness). A harness is detected when any of its variables is set and non-empty.
# VERIFIED on real sessions (2026-10-01):
#   * Claude Code exports CLAUDECODE=1 and CLAUDE_CODE_ENTRYPOINT (e.g. "claude-desktop") to the processes
#     it spawns (the pair core/harness/suite_lock.py already relies on).
#   * Grok Build 1.0.46: `grok -p ... --permission-mode bypassPermissions` asked to list the variable NAMES
#     containing GROK/CURSOR/SAND/AGENT/XAI reported GROK_AGENT and GROK_SESSION_ID (plus names inherited
#     from the Claude Code parent). GROK_CLI, which suite_lock assumes, is NOT exported.
# NOT CONFIRMED (best guess, no real session checked yet): CODEX_SANDBOX / CODEX_THREAD_ID for Codex and
# ANTIGRAVITY_SESSION for Antigravity. Correct this table, not the callers, once a real session of each
# harness shows what it exports. Until then the explicit DARKFAC_OPERATING_HARNESS override is the
# reliable way to declare the harness for those two.
AUTODETECT_ENV_SIGNALS: tuple[tuple[str, str], ...] = (
    ("CLAUDECODE", "claude"),
    ("CLAUDE_CODE_ENTRYPOINT", "claude"),
    ("CODEX_SANDBOX", "codex"),
    ("CODEX_THREAD_ID", "codex"),
    ("GROK_AGENT", "grok"),
    ("GROK_SESSION_ID", "grok"),
    ("GROK_CLI", "grok"),  # legacy name from suite_lock; not exported by Grok Build 1.0.46
    ("ANTIGRAVITY_SESSION", "antigravity"),
)


def normalize_harness(value: Optional[str]) -> Optional[str]:
    """`value` as a known harness name (lowercase, trimmed), or None when empty or not a known harness."""
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in KNOWN_HARNESSES else None


def detect_operating_harness(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The harness the factory is being operated through, per the environment, or None."""
    env = os.environ if environ is None else environ

    override = (env.get(OPERATING_HARNESS_ENV) or "").strip()
    if override:
        normalized = normalize_harness(override)
        if normalized is not None:
            return normalized
        logger.warning(
            "Ignoring invalid %s=%r (expected one of: %s)",
            OPERATING_HARNESS_ENV, override, ", ".join(KNOWN_HARNESSES),
        )

    signalled = {harness for name, harness in AUTODETECT_ENV_SIGNALS if (env.get(name) or "").strip()}
    if len(signalled) > 1:
        logger.info(
            "Operating harness is ambiguous (signals for %s); not preferring any harness",
            ", ".join(sorted(signalled)),
        )
        return None
    return next(iter(signalled), None)


def resolve_operating_harness(
    explicit: Optional[str] = None, environ: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """The operating harness: `explicit` if given, else `detect_operating_harness(environ)`.

    A non-empty `explicit` that is not a known harness is a caller mistake: it is logged and no harness is
    preferred (the stage keeps the plain headroom rule) rather than silently falling back to the
    environment.
    """
    if explicit is not None and explicit.strip():
        normalized = normalize_harness(explicit)
        if normalized is None:
            logger.warning(
                "Ignoring invalid operating harness %r (expected one of: %s)",
                explicit, ", ".join(KNOWN_HARNESSES),
            )
        return normalized
    return detect_operating_harness(environ)
