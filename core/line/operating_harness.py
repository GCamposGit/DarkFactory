"""Which harness the owner is operating the factory through right now (USR-109).

Owner decision: when the factory is driven from a specific harness (Claude Code, Codex, Grok Build or
Antigravity), that harness is the primary developer of the `development` stage as long as its quota is
above the critical floor. `core.line.routing.pick` asks this module for that harness; with no signal at all
(pump, worker, cloud) there is no operating harness and routing behaves exactly as before.

Source, in precedence order:

1. an explicit argument (`pick(operating_harness=...)`), handled by `resolve_operating_harness`;
2. the `DARKFAC_OPERATING_HARNESS` variable (`claude|codex|grok|antigravity`; invalid values fail closed);
3. autodetection from the variables the parent harness exports (`AUTODETECT_ENV_SIGNALS`);
4. nothing (`None`).

Autodetection never guesses: signals that point at more than one harness are ambiguous and yield `None`.

A fleet-launched ``run_ticket`` does not inherit that session. ``headless_executor_env`` is the
official environment for one executor: operational checkout, shared state root, and fleet context.
Interactive signals and another ticket, worktree or harness identity stay with the parent.

``resolve_process_identity`` (USR-471) is the single identity resolver every headless executor
(the fleet dispatcher, a fleet worker, a headless ``run_ticket``) calls instead of reading
``DARKFAC_OPERATING_HARNESS`` or calling ``detect_operating_harness`` directly: it returns the
harness, how it was decided (``origin``), the resolved roots, and whether this process may write.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, MutableMapping, Optional

logger = logging.getLogger(__name__)

OPERATING_HARNESS_ENV = "DARKFAC_OPERATING_HARNESS"
PROCESS_HARNESS_ENV = "DARKFAC_PROCESS_HARNESS"
KNOWN_HARNESSES: tuple[str, ...] = ("claude", "codex", "grok", "antigravity")
KNOWN_PROCESS_HARNESSES: tuple[str, ...] = (*KNOWN_HARNESSES, "openrouter")

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
    ("ANTIGRAVITY_AGENT", "antigravity"),
    ("ANTIGRAVITY_CONVERSATION_ID", "antigravity"),
    ("ANTIGRAVITY_SESSION", "antigravity"),
)


def normalize_harness(value: Optional[str]) -> Optional[str]:
    """`value` as a known harness name (lowercase, trimmed), or None when empty or not a known harness."""
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in KNOWN_HARNESSES else None


def _resolve_with_origin(
    explicit: Optional[str], environ: Optional[Mapping[str, str]]
) -> tuple[Optional[str], str]:
    """`(harness, origin)` by the USR-109 precedence: `explicit` argument, then the
    `DARKFAC_OPERATING_HARNESS` variable, then autodetection. `origin` is `"explicit"`,
    `"autodetect"` or `"none"`. Shared core of `detect_operating_harness`,
    `resolve_operating_harness` and the interactive branch of `resolve_process_identity`;
    every log message below is part of their documented, test-pinned behavior.
    """
    if explicit is not None and explicit.strip():
        normalized = normalize_harness(explicit)
        if normalized is None:
            logger.warning(
                "Ignoring invalid operating harness %r (expected one of: %s)",
                explicit, ", ".join(KNOWN_HARNESSES),
            )
            return None, "none"
        return normalized, "explicit"

    env = os.environ if environ is None else environ

    override = (env.get(OPERATING_HARNESS_ENV) or "").strip()
    if override:
        normalized = normalize_harness(override)
        if normalized is not None:
            return normalized, "explicit"
        logger.warning(
            "Ignoring invalid %s=%r (expected one of: %s)",
            OPERATING_HARNESS_ENV, override, ", ".join(KNOWN_HARNESSES),
        )
        # A malformed explicit declaration is still a signal. Do not silently replace
        # it with an inferred identity from unrelated variables in the same environment.
        return None, ORIGIN_NONE

    signalled = {harness for name, harness in AUTODETECT_ENV_SIGNALS if (env.get(name) or "").strip()}
    if len(signalled) > 1:
        logger.info(
            "Operating harness is ambiguous (signals for %s); not preferring any harness",
            ", ".join(sorted(signalled)),
        )
        return None, "none"
    harness = next(iter(signalled), None)
    return harness, ("autodetect" if harness is not None else "none")


def detect_operating_harness(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The harness the factory is being operated through, per the environment, or None."""
    harness, _origin = _resolve_with_origin(None, environ)
    return harness


def resolve_operating_harness(
    explicit: Optional[str] = None, environ: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """The operating harness: `explicit` if given, else `detect_operating_harness(environ)`.

    A non-empty `explicit` that is not a known harness is a caller mistake: it is logged and no harness is
    preferred (the stage keeps the plain headroom rule) rather than silently falling back to the
    environment.
    """
    harness, _origin = _resolve_with_origin(explicit, environ)
    return harness


def interactive_identity_env_names() -> tuple[str, ...]:
    """Environment names that identify an interactive harness session.

    The explicit override plus every autodetection signal. Fleet bootstrap and each
    launched executor scrub this list so a parent chat cannot become the child route.
    """
    return (OPERATING_HARNESS_ENV, *(name for name, _harness in AUTODETECT_ENV_SIGNALS))


_FLEET_MODE_ENV = "DARKFAC_FLEET_MODE"
_EXECUTION_CONTEXT_ENV = "DARKFAC_EXECUTION_CONTEXT"
_EXECUTOR_MODE_ENV = "DARKFAC_EXECUTOR_MODE"
_WRITE_CAPABLE_ENV = "DARKFAC_WRITE_CAPABLE"
_FLEET_MODE_TRUE_VALUES = frozenset({"1", "true", "on", "yes", "fleet"})
ORIGIN_EXPLICIT = "explicit"
ORIGIN_AUTODETECT = "autodetect"
ORIGIN_HEADLESS = "headless"
ORIGIN_NONE = "none"


def _fleet_mode_from_env(env: Mapping[str, str]) -> bool:
    """`core.line.line_lock.is_fleet_mode`, evaluated against an injected mapping (tests)."""
    return (env.get(_FLEET_MODE_ENV) or "").strip().lower() in _FLEET_MODE_TRUE_VALUES


@dataclass(frozen=True)
class ProcessHarnessIdentity:
    """One process's resolved harness identity (USR-471): what `resolve_process_identity` returns.

    `origin` tells the caller how the routing preference `harness` was decided: `"explicit"` (an argument this
    executor declared itself, or the `DARKFAC_OPERATING_HARNESS` variable when the process
    is interactive), `"autodetect"` (a single unambiguous interactive environment signal),
    `"headless"` (the process runs outside an interactive session, so a parent's interactive
    preference -- inherited or not -- never applies), or `"none"` (interactive, nothing
    resolved). `process_harness` identifies the actual process and has its own
    `process_harness_origin`; it never falls back to `DARKFAC_OPERATING_HARNESS`. `mode` is
    `"read"` only for Antigravity running headless (AGENTS.md section
    10: Antigravity writes only as the interactive operating harness); every other
    combination is `"write"`.
    """

    harness: Optional[str]
    origin: str
    execution_context: str
    project_root: Path
    state_root: Path
    mode: str
    write_capable: bool
    process_harness: Optional[str] = None
    process_harness_origin: str = ORIGIN_NONE


def resolve_process_identity(
    *,
    explicit: Optional[str] = None,
    environ: Optional[Mapping[str, str]] = None,
    execution_context: Optional[str] = None,
    project_root: Optional[Path | str] = None,
    state_root: Optional[Path | str] = None,
) -> ProcessHarnessIdentity:
    """The harness identity of THIS process -- the single path the fleet dispatcher, the fleet
    worker and a headless `run_ticket` use instead of reading `DARKFAC_OPERATING_HARNESS` or
    calling `detect_operating_harness` directly.

    `execution_context` defaults to `"headless"` when `DARKFAC_FLEET_MODE` is set (same
    convention as `core.line.line_lock.is_fleet_mode`), else `"interactive"`. A headless
    process ignores interactive signals -- the parent chat's preference, inherited or not,
    is not this process's identity -- and honors only `explicit` or `DARKFAC_PROCESS_HARNESS`.
    Without one, `origin` is `"headless"` and `harness` is `None`: the stage router falls back to
    the plain headroom rule (USR-471). An interactive process keeps the USR-109 routing
    preference precedence (`explicit`, then the operating-harness variable, then autodetection),
    while its actual process harness comes from the process variable or autodetection.
    """
    env = os.environ if environ is None else environ
    declared_context = (env.get(_EXECUTION_CONTEXT_ENV) or "").strip().lower()
    if execution_context is not None:
        ctx = execution_context.strip().lower()
        if ctx not in {"headless", "interactive"}:
            raise ValueError(f"invalid execution context: {execution_context!r}")
        if declared_context and declared_context != ctx:
            raise ValueError(
                f"execution context mismatch: argument={ctx!r} environment={declared_context!r}"
            )
    elif declared_context:
        if declared_context not in {"headless", "interactive"}:
            raise ValueError(f"invalid {_EXECUTION_CONTEXT_ENV}={declared_context!r}")
        ctx = declared_context
    else:
        ctx = "headless" if _fleet_mode_from_env(env) else "interactive"

    if ctx == "headless":
        if explicit is not None and explicit.strip():
            normalized = normalize_harness(explicit)
            if normalized is None:
                logger.warning(
                    "Ignoring invalid operating harness %r declared by this headless executor "
                    "(expected one of: %s)",
                    explicit, ", ".join(KNOWN_HARNESSES),
                )
                harness, origin = None, ORIGIN_HEADLESS
            else:
                harness, origin = normalized, ORIGIN_EXPLICIT
                logger.info("headless_process_identity harness=%s origin=explicit", harness)
        else:
            harness, origin = None, ORIGIN_HEADLESS
    else:
        harness, origin = _resolve_with_origin(explicit, env)

    process_harness, process_harness_origin = _resolve_process_harness(
        env, execution_context=ctx, explicit=explicit,
    )

    root = (
        _resolve_root(project_root, "project_root")
        if project_root is not None else _env_project_root(env)
    )
    state = (
        _resolve_root(state_root, "state_root")
        if state_root is not None
        else _env_state_root(env, root)
    )
    effective_process_harness = process_harness or harness
    mode = "read" if (effective_process_harness == "antigravity" and ctx == "headless") else "write"
    write_capable_value = (env.get(_WRITE_CAPABLE_ENV) or "").strip().lower()
    if ctx == "headless" and write_capable_value and write_capable_value not in {"0", "1", "false", "true"}:
        raise ValueError(f"invalid {_WRITE_CAPABLE_ENV}={write_capable_value!r}")
    write_capable = write_capable_value not in {"0", "false"} if write_capable_value else mode == "write"
    if ctx == "headless" and effective_process_harness == "antigravity":
        write_capable = False

    return ProcessHarnessIdentity(
        harness=harness, origin=origin,
        process_harness=process_harness, process_harness_origin=process_harness_origin,
        execution_context=ctx,
        project_root=root, state_root=state, mode=mode,
        write_capable=write_capable,
    )


def _resolve_process_harness(
    env: Mapping[str, str], *, execution_context: str, explicit: Optional[str],
) -> tuple[Optional[str], str]:
    """Resolve this process's actual harness independently from its routing preference.

    Precedence is an explicit caller declaration, the process-scoped environment value, then
    unambiguous harness signals for interactive processes. Headless processes never inspect
    interactive signals or ``DARKFAC_OPERATING_HARNESS``.
    """
    declared = _env_value(env, PROCESS_HARNESS_ENV).strip()
    configured: Optional[str] = None
    if declared:
        cleaned = declared.casefold()
        if cleaned != "none" and cleaned not in KNOWN_PROCESS_HARNESSES:
            raise ValueError(
                f"invalid {PROCESS_HARNESS_ENV}={declared!r} "
                f"(expected one of: {', '.join((*KNOWN_PROCESS_HARNESSES, 'none'))})"
            )
        configured = None if cleaned == "none" else cleaned

    if explicit is not None and explicit.strip():
        normalized = explicit.strip().casefold()
        if normalized in KNOWN_PROCESS_HARNESSES:
            return normalized, ORIGIN_EXPLICIT
        return None, ORIGIN_HEADLESS if execution_context == "headless" else ORIGIN_NONE

    if declared:
        return configured, "declared_none" if configured is None else "process_config"

    if execution_context == "headless":
        return None, ORIGIN_HEADLESS

    signalled = {harness for name, harness in AUTODETECT_ENV_SIGNALS if _env_value(env, name)}
    if len(signalled) > 1:
        logger.info(
            "Process harness identity is ambiguous (signals for %s); not selecting a harness",
            ", ".join(sorted(signalled)),
        )
        return None, ORIGIN_NONE
    process_harness = next(iter(signalled), None)
    return process_harness, (ORIGIN_AUTODETECT if process_harness is not None else ORIGIN_NONE)


def _env_project_root(env: Mapping[str, str]) -> Path:
    configured = _configured_root_value(env, "DARKFAC_PROJECT_ROOT", "PROJECT_ROOT")
    if configured:
        return _resolve_root(configured, "project root")
    logger.warning("PROJECT_ROOT is not configured; falling back to the source checkout")
    return Path(__file__).resolve().parents[2]


def _env_state_root(env: Mapping[str, str], project_root: Path) -> Path:
    configured = _configured_root_value(env, "DARKFAC_STATE_ROOT", "STATE_ROOT")
    if configured:
        return _resolve_root(configured, "state root")
    logger.warning("STATE_ROOT is not configured; falling back to PROJECT_ROOT/.factory")
    return project_root / ".factory"


def _env_value(env: Mapping[str, str], name: str) -> str:
    """Read an environment value case-insensitively, including injected test mappings."""
    folded = name.casefold()
    return next((value for key, value in env.items() if key.casefold() == folded), "")


def _configured_root_value(env: Mapping[str, str], primary: str, fallback: str) -> str:
    primary_value = _env_value(env, primary).strip()
    fallback_value = _env_value(env, fallback).strip()
    if primary_value and fallback_value:
        if _resolve_root(primary_value, primary) != _resolve_root(fallback_value, fallback):
            raise ValueError(
                f"conflicting {primary} and {fallback} values: "
                f"{primary_value!r} != {fallback_value!r}"
            )
    return primary_value or fallback_value


def _resolve_root(value: Path | str, name: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path: {value!r}")
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"invalid {name}: {value!r}") from exc


# Another ticket, run or checkout. Secrets and host policy are not in this list.
# The strings are the constants owned by line_lock, local_cancel, local_progress and execution_registry.
FOREIGN_RUN_ENV: tuple[str, ...] = (
    "DARKFAC_TICKET_ID",
    "DARKFAC_RUN_ID",
    "DARKFAC_EXECUTION_ID",
    "DARKFAC_LOCAL_RUN_ID",
    "DARKFAC_LOCAL_CANCEL_PARENT",
    "DARKFAC_LINE_LOCK_HELD",
    "DARKFAC_LOCAL_PROGRESS_WARNED",
    "DARKFAC_CODEX_SESSION_PATH",
    "DARKFAC_HARNESS_WORKER_JOB",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_PREFIX",
)

# Set again only for the launch that owns them. An inherited value belongs to another run.
LAUNCH_SCOPED_ENV: tuple[str, ...] = (
    "DARKFAC_HOST_ID",
    "DARKFAC_RECOVERY_MODE",
    "DARKFAC_CANDIDATE_DIGEST",
)


def clear_interactive_identity(env: MutableMapping[str, str]) -> None:
    """Remove interactive harness signals from ``env`` (case-insensitive)."""
    _drop_env(env, interactive_identity_env_names())


def declare_headless_process(
    env: MutableMapping[str, str],
    *,
    project_root: Path | str,
    state_root: Path | str,
    fleet: bool = True,
    process_harness: Optional[str] = None,
) -> None:
    """Apply the explicit process contract to an already-running headless executor.

    ``DARKFAC_OPERATING_HARNESS`` and harness autodetection variables belong to an interactive
    parent. The optional ``process_harness`` identifies this child itself; absent means the
    process is a service/launcher with no model harness.
    """
    resolved_project_root = str(_resolve_root(project_root, "project_root"))
    resolved_state_root = str(_resolve_root(state_root, "state_root"))
    normalized_harness = _validate_process_harness(process_harness)
    clear_interactive_identity(env)
    # Empty is the explicit service setting for "no interactive operating harness". Keeping
    # the key present makes the headless contract observable while ensuring an inherited chat
    # preference can never steer this process.
    env[OPERATING_HARNESS_ENV] = ""
    env["PROJECT_ROOT"] = resolved_project_root
    env["STATE_ROOT"] = resolved_state_root
    env["DARKFAC_PROJECT_ROOT"] = resolved_project_root
    env["DARKFAC_STATE_ROOT"] = resolved_state_root
    env[_EXECUTION_CONTEXT_ENV] = "headless"
    declared_harness = _set_process_harness(env, normalized_harness)
    env[_EXECUTOR_MODE_ENV] = "read" if declared_harness == "antigravity" else "write"
    env[_WRITE_CAPABLE_ENV] = "0" if declared_harness == "antigravity" else "1"
    if fleet:
        env[_FLEET_MODE_ENV] = "1"
    else:
        _drop_env(env, (_FLEET_MODE_ENV,))


def declare_interactive_process(
    env: MutableMapping[str, str], *, project_root: Path | str, state_root: Path | str,
    process_harness: str,
) -> None:
    """Declare an interactive harness child without changing its routing preference."""
    resolved_project_root = str(_resolve_root(project_root, "project_root"))
    resolved_state_root = str(_resolve_root(state_root, "state_root"))
    normalized_harness = _validate_process_harness(process_harness)
    env["PROJECT_ROOT"] = resolved_project_root
    env["STATE_ROOT"] = resolved_state_root
    env["DARKFAC_PROJECT_ROOT"] = resolved_project_root
    env["DARKFAC_STATE_ROOT"] = resolved_state_root
    env[_EXECUTION_CONTEXT_ENV] = "interactive"
    _set_process_harness(env, normalized_harness)
    env[_EXECUTOR_MODE_ENV] = "write"
    env[_WRITE_CAPABLE_ENV] = "1"
    _drop_env(env, (_FLEET_MODE_ENV,))


def _validate_process_harness(process_harness: Optional[str]) -> Optional[str]:
    if process_harness is None:
        return None
    normalized = process_harness.strip().casefold()
    if normalized not in KNOWN_PROCESS_HARNESSES:
        raise ValueError(
            f"invalid process harness {process_harness!r} "
            f"(expected one of: {', '.join(KNOWN_PROCESS_HARNESSES)})"
        )
    return normalized


def _set_process_harness(
    env: MutableMapping[str, str], process_harness: Optional[str],
) -> Optional[str]:
    normalized = _validate_process_harness(process_harness)
    _drop_env(env, (PROCESS_HARNESS_ENV,))
    if normalized is None:
        env[PROCESS_HARNESS_ENV] = "none"
        return None
    env[PROCESS_HARNESS_ENV] = normalized
    return normalized


def harness_executor_env(
    base: Optional[Mapping[str, str]] = None, *, process_harness: str,
    project_root: Path | str, state_root: Path | str, execution_context: str,
) -> dict[str, str]:
    """Build a harness CLI's independent process environment with explicit identity and roots."""
    env = dict(os.environ if base is None else base)
    context = execution_context.strip().casefold()
    if context == "headless":
        declare_headless_process(
            env, project_root=project_root, state_root=state_root,
            fleet=_fleet_mode_from_env(env), process_harness=process_harness,
        )
    elif context == "interactive":
        declare_interactive_process(
            env, project_root=project_root, state_root=state_root,
            process_harness=process_harness,
        )
    else:
        raise ValueError(f"invalid execution context: {execution_context!r}")
    return env


def headless_executor_env(
    base: Optional[Mapping[str, str]] = None,
    *,
    project_root: Path | str,
    state_root: Path | str,
    host_id: Optional[str] = None,
    run_id: Optional[str] = None,
    fleet: bool = True,
    updates: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Environment for one ``run_ticket`` launched by the fleet.

    Copies secrets and operational variables. ``DARKFAC_PROJECT_ROOT`` is this launch's
    checkout, ``DARKFAC_STATE_ROOT`` is the shared root already resolved by the parent, and
    ``DARKFAC_FLEET_MODE=1`` is the official fleet context (``line_lock.is_fleet_mode``).
    A resume that must keep the canonical line lock passes ``fleet=False`` and does not
    receive that variable. Interactive identity and another ticket, worktree or harness
    do not cross the boundary. The stage ``pick`` chooses harness and model afterwards.
    """
    env = dict(os.environ if base is None else base)
    _drop_env(env, (*interactive_identity_env_names(), *FOREIGN_RUN_ENV, *LAUNCH_SCOPED_ENV))
    declare_headless_process(
        env, project_root=project_root, state_root=state_root, fleet=fleet,
    )
    if host_id:
        env["DARKFAC_HOST_ID"] = host_id
    else:
        _drop_env(env, ("DARKFAC_HOST_ID",))
    if run_id:
        env["DARKFAC_RUN_ID"] = run_id
    if updates:
        env.update(updates)
    # Caller updates carry operational knobs (remote workers, recovery, candidate digest),
    # not process identity or another run's ownership markers. Apply the boundary after
    # merging them so a caller cannot accidentally reintroduce the parent's chat identity
    # or overwrite this launch's roots and fleet context.
    _drop_env(env, (*interactive_identity_env_names(), *FOREIGN_RUN_ENV))
    declare_headless_process(
        env, project_root=project_root, state_root=state_root, fleet=fleet,
    )
    if host_id:
        env["DARKFAC_HOST_ID"] = host_id
    else:
        _drop_env(env, ("DARKFAC_HOST_ID",))
    if run_id:
        env["DARKFAC_RUN_ID"] = run_id
    return env


def _drop_env(env: MutableMapping[str, str], names: Iterable[str]) -> None:
    """Delete ``names`` from ``env`` regardless of key case."""
    blocked = {name.casefold() for name in names}
    for key in [key for key in env if key.casefold() in blocked]:
        env.pop(key, None)
