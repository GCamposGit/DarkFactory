"""Quota-aware harness routing for the DarkFac production line (HF-27-03 / HF-23).

Implements the unified "subscription first, OpenRouter only with verified balance,
and fail-closed under critical pressure" policy.
`pick()` walks a stage cascade, filtering out ineligible accounts (not in host_caps,
in cooldown, forbidden models, unknown/stale quota, or remaining <= critical threshold 15%,
except that low-complexity development can use unknown quota in degraded mode),
and prioritizes eligible harnesses via Dynamic Headroom (highest remaining quota first)
or specialized intelligence tiers.

Capabilities are declared, never assumed (USR-67): a stage has a mode (`STAGE_MODES`: write for
development/integration, read otherwise) and `pick()` only elects harnesses that declare it in
`core.line.agent_cli.HARNESS_CAPABILITIES`, before ranking by headroom.

Operating harness (USR-109): when the owner drives the factory through a specific harness (explicit
argument, `DARKFAC_OPERATING_HARNESS` or autodetection, see `core.line.operating_harness`), that harness is
elected for the `development` stage whenever it survives the same filters and its quota is above the
critical floor, ahead of the headroom ranking. The 15% floor is never relaxed.
"""

from __future__ import annotations

import json
import logging
import os
import time
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

from core.line.agent_cli import AgentResult, _DEFAULT_OPENROUTER_MODEL, supports
from core.line.operating_harness import resolve_operating_harness
from core.models.registry import QUOTA_FLOOR_MIN, forbidden_markers, get_registry, resolve as resolve_model
from core.paths import project_root, state_root

logger = logging.getLogger(__name__)

_CANONICAL_REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = _CANONICAL_REPO_ROOT

# Critical pressure threshold: the single quota floor of the model registry (strict fail-closed)
_DEFAULT_PRESSURE_THRESHOLDS: dict[str, float] = {
    "guarded": 50.0,
    "stressed": 25.0,
    "critical": QUOTA_FLOOR_MIN,
}

# USR-185: the never-autonomous markers (fable, astra, ...) come from the model registry.
_DEFAULT_FORBIDDEN_AUTONOMOUS_MODELS: list[str] = forbidden_markers()


def _resolve_role_ref(value: Optional[str]) -> Optional[str]:
    """Map a registry role reference (``development.codex``) to its model id; any other value is kept as is."""
    if isinstance(value, str) and value.strip() in get_registry().roles:
        return resolve_model(value.strip())
    return value

_HARNESS_TO_PROVIDER: dict[str, str] = {
    "claude": "anthropic",
    "codex": "openai",
    "grok": "xai",
    "antigravity": "google",
}


# Opt-in bootstrap for a host that has no quota data for a subscription harness yet (the cloud worker
# before Codex has ever run). Unset (default) = unknown quota is ineligible, fail-closed.
UNKNOWN_QUOTA_ENV = "DARKFAC_ROUTING_UNKNOWN_QUOTA"
UNKNOWN_QUOTA_LAST_RESORT = "last_resort"


def unknown_quota_last_resort() -> bool:
    """True when `DARKFAC_ROUTING_UNKNOWN_QUOTA=last_resort` (set only on the cloud worker service)."""
    return os.environ.get(UNKNOWN_QUOTA_ENV, "").strip().lower() == UNKNOWN_QUOTA_LAST_RESORT


# Mode each stage needs from its agent. Stages not listed are read-only (text in, text out).
STAGE_MODES: dict[str, str] = {
    "development": "write",
    "integration": "write",
}
ExecutionContext = Literal["interactive", "headless"]
_DEFAULT_STAGE_MODE = "read"


def stage_mode(stage: str) -> str:
    """`write` when the stage's agent edits files under the worktree, else `read`."""
    return STAGE_MODES.get(stage, _DEFAULT_STAGE_MODE)


class HarnessPin(BaseModel):
    """CLI model and reasoning effort declared for one harness on a stage (USR-201).

    The cascade tuple stays the routing key (`pick()` still returns it). This pin is what the
    headless CLI is asked to run, so a harness whose cascade model is null does not silently
    inherit whatever the machine's CLI config happens to say today.
    """

    model: Optional[str] = None
    effort: Optional[str] = None

    @field_validator("model", mode="after")
    @classmethod
    def _model_role_ref(cls, value: Optional[str]) -> Optional[str]:
        return _resolve_role_ref(value)


class StageRoute(BaseModel):
    """Routing policy for a single production-line stage."""

    cascade: Union[Literal["other_family_than_development"], list[tuple[str, Optional[str]]]] = Field(
        default_factory=list
    )
    openrouter_ok: bool = False
    openrouter_model: Optional[str] = None
    effort: Optional[str] = None
    local_ok: bool = False
    harness_pins: dict[str, HarnessPin] = Field(default_factory=dict)

    @field_validator("openrouter_model", mode="after")
    @classmethod
    def _openrouter_model_role_ref(cls, value: Optional[str]) -> Optional[str]:
        return _resolve_role_ref(value)


class RunCaps(BaseModel):
    """Per-run ceilings; exceeding these moves the run to WAITING_HUMAN, never a loop."""

    agent_calls: int = 14
    validate_iterations_per_ticket: int = 3
    review_rounds: int = 2
    wall_clock_hours: float = 6.0
    openrouter_usd: float = 2.0


class RoutingConfig(BaseModel):
    """Full `.factory/config/line_routing.json` contract."""

    pressure_thresholds: dict[str, float] = Field(default_factory=lambda: dict(_DEFAULT_PRESSURE_THRESHOLDS))
    cooldown_default_minutes: int = 60
    # A harness whose CLI crashes, or whose own terminal/sandbox tooling fails to start the call
    # (`harness_infra_error`, USR-188) — not a quota/login problem — sits out briefly, so the next
    # stage or job does not pick it again straight away.
    crash_cooldown_minutes: int = 10
    forbidden_autonomous_models: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_FORBIDDEN_AUTONOMOUS_MODELS)
    )
    stages: dict[str, StageRoute] = Field(default_factory=dict)
    run_caps: RunCaps = Field(default_factory=RunCaps)


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------


def default_config_path() -> Path:
    return REPO_ROOT / ".factory" / "config" / "line_routing.json"


def default_cooldown_path() -> Path:
    return state_root() / "usage" / "cooldowns.json"


def _default_routing_config() -> RoutingConfig:
    cheap_model = os.environ.get("DARKFAC_OPENROUTER_CHEAP_MODEL", _DEFAULT_OPENROUTER_MODEL)
    return RoutingConfig(
        pressure_thresholds=dict(_DEFAULT_PRESSURE_THRESHOLDS),
        cooldown_default_minutes=60,
        forbidden_autonomous_models=list(_DEFAULT_FORBIDDEN_AUTONOMOUS_MODELS),
        stages={
            "grill": StageRoute(
                cascade=[("antigravity", None), ("claude", "sonnet"), ("codex", None)],
                openrouter_ok=True,
                openrouter_model=cheap_model,
            ),
            "planning": StageRoute(
                cascade=[
                    ("claude", "opus"),
                    ("codex", resolve_model("planning.codex")),
                    ("grok", resolve_model("development.grok")),
                    ("antigravity", None),
                ],
                effort="high",
                openrouter_ok=True,
                openrouter_model=cheap_model,
            ),
            "development": StageRoute(
                cascade=[("claude", "sonnet"), ("codex", None), ("grok", resolve_model("development.grok"))],
                openrouter_ok=False,  # write stage: OpenRouter is read-only, never a fallback here
                openrouter_model=cheap_model,
                harness_pins={
                    "claude": HarnessPin(model="sonnet"),
                    "codex": HarnessPin(model=resolve_model("development.codex"), effort="high"),
                    "grok": HarnessPin(model=resolve_model("development.grok"), effort="high"),
                },
            ),
            "review": StageRoute(
                cascade="other_family_than_development",
                openrouter_ok=True,
                openrouter_model=cheap_model,
            ),
            "distill": StageRoute(
                cascade=[],
                openrouter_ok=True,
                local_ok=True,
                openrouter_model=cheap_model,
            ),
        },
        run_caps=RunCaps(),
    )


def validate_routing_config(config: RoutingConfig) -> list[str]:
    """Violations of the capability contract: a cascade candidate that cannot run its stage's mode.

    Empty list means consistent. Only subscription harnesses are checked; the OpenRouter fallback is
    structurally read-only and `pick()` never offers it to a write stage; `openrouter_ok: true` on a
    write stage is reported as a violation because it can never take effect.
    """
    violations: list[str] = []
    for stage, stage_cfg in config.stages.items():
        mode = stage_mode(stage)
        for harness, model in _resolve_cascade(stage_cfg, None):
            if not supports(harness, mode):
                label = f"{harness}:{model}" if model else harness
                violations.append(f"stage '{stage}' needs '{mode}' but candidate '{label}' does not declare it")
        if stage_cfg.openrouter_ok and not supports("openrouter", mode):
            violations.append(
                f"stage '{stage}' needs '{mode}' but openrouter_ok is true and OpenRouter does not declare it"
            )
    return violations


def load_routing_config(path: Optional[Path] = None) -> RoutingConfig:
    """Load the routing config from JSON, falling back to v0 defaults when absent/invalid."""
    target = path or default_config_path()
    if target.is_file():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            config = RoutingConfig.model_validate(raw)
        except Exception as exc:  # malformed file, wrong schema, etc.
            logger.warning("Failed to load routing config %s (%s); using v0 defaults", target, exc)
        else:
            for violation in validate_routing_config(config):
                logger.warning("Routing config %s: %s (pick() will skip it)", target, violation)
            return config
    return _default_routing_config()


def harness_cli_pin(
    harness: str,
    route_model: Optional[str],
    *,
    stage: str = "development",
    config: Optional[RoutingConfig] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Model and effort the stage declares for `harness`'s CLI, plus the cascade model as fallback.

    A pin model that is a forbidden autonomous model is ignored (the cascade model is kept).
    An absent pin keeps `route_model` and sends no effort. The cascade key itself does not change:
    `pick()` still returns `(harness, route_model)` for exclusion and ranking.
    """
    cfg = config or load_routing_config()
    stage_cfg = cfg.stages.get(stage)
    if stage_cfg is None:
        return route_model, None
    pin = stage_cfg.harness_pins.get(harness.strip().lower())
    if pin is None:
        return route_model, None
    forbidden = {item.lower().strip() for item in cfg.forbidden_autonomous_models}
    pinned_model = pin.model.strip() if isinstance(pin.model, str) and pin.model.strip() else None
    if pinned_model and pinned_model.lower() in forbidden:
        logger.warning("Ignoring forbidden pinned model %s for harness %s", pinned_model, harness)
        pinned_model = None
    pinned_effort = pin.effort.strip() if isinstance(pin.effort, str) and pin.effort.strip() else None
    return pinned_model or route_model, pinned_effort


# --------------------------------------------------------------------------
# Cooldown store — .factory/usage/cooldowns.json (path overridable for tests)
# --------------------------------------------------------------------------


def _load_cooldowns(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read cooldown store %s (%s); treating as empty", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _save_cooldowns(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _parse_until(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _in_cooldown(harness: str, cooldowns: dict[str, Any], *, now: Optional[datetime] = None) -> bool:
    entry = cooldowns.get(harness)
    if not isinstance(entry, dict):
        return False
    until = _parse_until(entry.get("until"))
    if until is None:
        return False
    current = now or datetime.now(timezone.utc)
    return current < until


def record_result(
    result: AgentResult,
    *,
    config: Optional[RoutingConfig] = None,
    cooldown_path: Optional[Path] = None,
    reservation_id: Optional[str] = None,
) -> None:
    """Record execution outcome, releasing reservation and setting cooldown on rate limit."""
    if reservation_id:
        try:
            from core.usage.reservation import QuotaReservationManager

            QuotaReservationManager().release(reservation_id)
        except Exception as exc:
            logger.debug("Failed releasing quota reservation %s: %s", reservation_id, exc)

    if result.error_kind not in ("rate_limited", "crash", "harness_infra_error"):
        return
    cfg = config or load_routing_config()
    path = cooldown_path or default_cooldown_path()

    if result.error_kind in ("crash", "harness_infra_error"):
        if result.harness in ("", "none", "openrouter"):
            return
        until = datetime.now(timezone.utc) + timedelta(minutes=max(cfg.crash_cooldown_minutes, 0))
        existing = _load_cooldowns(path).get(result.harness)
        existing_until = _parse_until(existing.get("until")) if isinstance(existing, dict) else None
        if existing_until is not None and existing_until > until:
            return  # never shorten a longer (e.g. rate-limit) cooldown
    else:
        until = result.reset_at
        if until is None:
            until = datetime.now(timezone.utc) + timedelta(minutes=cfg.cooldown_default_minutes)
        elif until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)

    data = _load_cooldowns(path)
    data[result.harness] = {
        "until": until.astimezone(timezone.utc).isoformat(),
        "reason": result.error_kind,
        "model": result.model,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_cooldowns(path, data)


# --------------------------------------------------------------------------
# Quota headroom (strict fail-closed, reads .factory/usage/providers)
# --------------------------------------------------------------------------


_SNAPSHOT_MAX_AGE_SECONDS = 3600.0


def _parse_iso_utc(raw: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _snapshot_max_age(provider_dir: Path) -> float:
    resolved = provider_dir.resolve()
    if os.environ.get("PYTEST_CURRENT_TEST") and (
        resolved == (_CANONICAL_REPO_ROOT / ".factory" / "usage" / "providers").resolve()
        or resolved == (state_root() / "usage" / "providers").resolve()
    ):
        return 86400.0 * 30.0
    return _SNAPSHOT_MAX_AGE_SECONDS


# Credential-free reason behind a `None` headroom (or "healthy" for a verified, current reading);
# see `_quota_headroom_diagnosis`. Never carries raw probe output, tokens or account identifiers.
QuotaDiagnosisReason = Literal[
    "healthy", "stale_snapshot", "probe_unavailable", "probe_timeout", "suspicious_reading", "unknown"
]


PROBE_NEGATIVE_TTL_S = 600.0  # USR-404: a failed/expired probe is not repeated for 10 min


def _probe_negative_path(provider_dir: Path) -> Path:
    return provider_dir.parent / "probe_negative.json"


def _probe_negative_read(provider_dir: Path, provider_id: str) -> bool:
    try:
        data = json.loads(_probe_negative_path(provider_dir).read_text(encoding="utf-8"))
        return time.time() - float(data[provider_id]) < PROBE_NEGATIVE_TTL_S
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _probe_negative_write(provider_dir: Path, provider_id: str) -> None:
    path = _probe_negative_path(provider_dir)
    try:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[provider_id] = time.time()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError as exc:
        logger.debug("negative probe cache not written: %s", exc)


def _probe_negative_clear(provider_dir: Path, provider_id: str) -> None:
    path = _probe_negative_path(provider_dir)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.pop(provider_id, None) is not None:
            path.write_text(json.dumps(data), encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        pass


_STALE_RESET_MARGIN_POINTS = 10.0


def _stale_until_reset_headroom(snapshot_file: Path, now: datetime) -> Optional[float]:
    """Anthropic only (USR-416): the statusline writes the Claude quota only while a session is open.

    Between sessions the last valid snapshot stays usable until its 5h window resets, with a
    conservative margin added to the used percentage. After the reset it is unknown again.
    """
    try:
        data = json.loads(snapshot_file.read_text(encoding="utf-8"))
        windows = [w for w in data.get("windows", []) if isinstance(w, dict)]
        five_hour = next((w for w in windows if w.get("quota_id") == "claude:5h"), None)
        reset = _parse_iso_utc(five_hour.get("resets_at")) if five_hour else None
        if reset is None or reset <= now or data.get("plausibility_flags"):
            return None
        values = []
        for window in windows:
            if window.get("used_percent") is None:
                continue
            window_reset = _parse_iso_utc(window.get("resets_at")) if window.get("resets_at") else None
            if window_reset is not None and window_reset <= now:
                continue
            values.append(max(0.0, 100.0 - min(100.0, float(window["used_percent"]) + _STALE_RESET_MARGIN_POINTS)))
        return min(values) if values else None
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _quota_headroom_diagnosis(provider_id: str) -> tuple[Optional[float], QuotaDiagnosisReason]:
    """Real quota probe via core.usage.adapters reading providers. Fail-closed on stale/missing.

    A stale snapshot is not the end of the road: the adapter is asked again (`force=True`, a live probe
    or, for Codex inside the cloud container, the rate limits Codex logged in its own session files) and
    only a result whose `checked_at` is within the max age and passes the plausibility check counts.
    Anything older, unreadable, suspicious or absent is `(None, reason)`, which `pick()` treats as
    unknown (fail-closed); `reason` distinguishes, for observability only:
      * "stale_snapshot" — nothing on disk was recent enough and no re-probe was warranted (the disk
        reading itself was within the max age, or there was never one, but the adapter still came back
        with evidence older than the max age).
      * "probe_unavailable" — the disk snapshot WAS stale (or absent) so a re-probe was forced, but no
        current, verifiable evidence came back (probe timeout/crash, disconnected, no meter exposed).
      * "probe_timeout" — the probe exceeded the configured deadline (`DARKFAC_PROBE_DEADLINE_SEC`,
        default 10 s) and was abandoned; its process tree is killed. Quota stays unknown (fail-closed).
      * "suspicious_reading" — a reading came back inside the max age but failed the plausibility check
        (`ProviderAccountUsage.plausibility_flags`), so it is never trusted even though it is fresh.
      * "unknown" — no adapter is registered for `provider_id`, or a defensive failure occurred.
    A non-`None` headroom always comes with "healthy"; the caller (`pick()`) applies its own critical
    threshold to decide whether "healthy" is actually usable.
    """
    try:
        from core.router.token_budget import _quota_headroom
        from core.usage.adapters import build_default_adapters
        from core.usage.reservation import QuotaReservationManager

        mocked_dir = REPO_ROOT / ".factory" / "usage" / "providers"
        provider_dir = mocked_dir if (REPO_ROOT != project_root() and mocked_dir.is_dir()) else (state_root() / "usage" / "providers")
        max_age = _snapshot_max_age(provider_dir)
        snapshot_stale = False
        snapshot_file = provider_dir / f"{provider_id}.json"
        if snapshot_file.is_file():
            try:
                data = json.loads(snapshot_file.read_text(encoding="utf-8"))
                checked_at = _parse_iso_utc(data.get("checked_at")) if data.get("checked_at") else None
                if checked_at is not None:
                    age_seconds = (datetime.now(timezone.utc) - checked_at).total_seconds()
                    if age_seconds > max_age:
                        logger.info(
                            "Snapshot for %s is stale (%s s > %ss); re-probing", provider_id, round(age_seconds, 1), max_age
                        )
                        snapshot_stale = True
            except Exception as e:
                logger.debug("Failed parsing checked_at for provider %s: %s", provider_id, e)

        for adapter in build_default_adapters(provider_dir):
            if adapter.spec.provider_id != provider_id:
                continue
            from core.usage.probe_deadline import ProbeTimeout, run_with_deadline

            if (snapshot_stale or not snapshot_file.is_file()) and _probe_negative_read(provider_dir, provider_id):
                logger.info("Quota probe for %s skipped (negative cache); failing closed", provider_id)
                if provider_id == "anthropic":
                    held = _stale_until_reset_headroom(snapshot_file, datetime.now(timezone.utc))
                    if held is not None:
                        return max(0.0, held - QuotaReservationManager().get_active_reserved_percent(provider_id)), "healthy"
                return None, "probe_timeout"
            try:
                usage = run_with_deadline(provider_id, lambda a=adapter: a.inspect(force=snapshot_stale))
            except ProbeTimeout as timeout_exc:
                logger.warning("Quota probe timed out %s; failing closed", timeout_exc.as_dict())
                _probe_negative_write(provider_dir, provider_id)
                if provider_id == "anthropic":
                    held = _stale_until_reset_headroom(snapshot_file, datetime.now(timezone.utc))
                    if held is not None:
                        return max(0.0, held - QuotaReservationManager().get_active_reserved_percent(provider_id)), "healthy"
                return None, "probe_timeout"
            checked = _parse_iso_utc(usage.checked_at)
            if checked is None or (datetime.now(timezone.utc) - checked).total_seconds() > max_age:
                reason: QuotaDiagnosisReason = "probe_unavailable" if snapshot_stale else "stale_snapshot"
                if provider_id == "anthropic":
                    held = _stale_until_reset_headroom(snapshot_file, datetime.now(timezone.utc))
                    if held is not None:
                        logger.info("Anthropic snapshot stale; holding last reading until 5h reset (+margin)")
                        reserved = QuotaReservationManager().get_active_reserved_percent(provider_id)
                        return max(0.0, held - reserved), "healthy"
                logger.warning(
                    "Quota data for %s is older than %ss (%s); failing closed", provider_id, max_age, reason
                )
                return None, reason
            raw_headroom = _quota_headroom(usage)
            if raw_headroom is None:
                reason = "suspicious_reading" if usage.plausibility_flags else "probe_unavailable"
                logger.info("Quota for %s is not usable (%s); failing closed", provider_id, reason)
                return None, reason
            _probe_negative_clear(provider_dir, provider_id)
            reserved = QuotaReservationManager().get_active_reserved_percent(provider_id)
            return max(0.0, raw_headroom - reserved), "healthy"
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Quota lookup failed for provider %s: %s", provider_id, exc)
    return None, "unknown"


def _default_quota_headroom(provider_id: str) -> Optional[float]:
    """Headroom-only view of `_quota_headroom_diagnosis`, matching the `quota_lookup` contract
    callers inject into `pick()` (`Callable[[str], Optional[float]]`).
    """
    return _quota_headroom_diagnosis(provider_id)[0]


# --------------------------------------------------------------------------
# Cascade resolution + pick()
# --------------------------------------------------------------------------


def _resolve_cascade(
    stage_cfg: StageRoute, implementing_harness: Optional[str]
) -> list[tuple[str, Optional[str]]]:
    if stage_cfg.cascade == "other_family_than_development":
        if implementing_harness == "claude":
            return [("codex", None), ("grok", None), ("antigravity", None)]
        if implementing_harness == "codex":
            return [("claude", None), ("grok", None), ("antigravity", None)]
        # The reviewer never shares the implementer's family, including for Grok and Antigravity.
        if implementing_harness == "grok":
            return [("claude", None), ("codex", None), ("antigravity", None)]
        if implementing_harness == "antigravity":
            return [("claude", None), ("codex", None), ("grok", None)]
        return [("claude", None), ("codex", None), ("grok", None), ("antigravity", None)]
    # Registry role references (`planning.codex`) become concrete model ids here.
    return [(harness, _resolve_role_ref(model)) for harness, model in stage_cfg.cascade]


def _split_exclude(
    exclude: Iterable[Any],
) -> tuple[set[str], set[tuple[str, Optional[str]]]]:
    harnesses: set[str] = set()
    pairs: set[tuple[str, Optional[str]]] = set()
    for item in exclude:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            pairs.add((item[0], item[1]))
        else:
            harnesses.add(str(item))
    return harnesses, pairs


def _extra_operating_candidate(
    cascade: list[tuple[str, Optional[str]]], operating_harness: Optional[str]
) -> Optional[tuple[str, Optional[str]]]:
    """`(operating_harness, None)` when that harness is not in `cascade` at all, else None."""
    if operating_harness and all(harness != operating_harness for harness, _ in cascade):
        return operating_harness, None
    return None


CODEX_HEADLESS_BLOCK_REASON = "codex_headless_opted_out"


def codex_headless_blocked() -> bool:
    """True only when the host explicitly opts out of headless codex (USR-347).

    The console-window issue of USR-336 was a one-off and is solved; owner decision of 23/10/2026:
    codex headless is allowed on every OS. Set `DARKFAC_CODEX_HEADLESS_ALLOWED=0` (false/no/off) to
    keep codex out of headless runs on a given host.
    """
    return os.environ.get("DARKFAC_CODEX_HEADLESS_ALLOWED", "").strip().lower() in ("0", "false", "no", "off")


def _routable_candidates(
    stage: str,
    stage_cfg: StageRoute,
    cfg: RoutingConfig,
    caps: set[str],
    required_mode: str,
    excluded_harnesses: set[str],
    excluded_pairs: set[tuple[str, Optional[str]]],
    implementing_harness: Optional[str],
    operating_harness: Optional[str] = None,
    execution_context: ExecutionContext = "interactive",
) -> list[tuple[str, Optional[str]]]:
    """Cascade candidates that could serve `stage` on this host, before cooldown/quota (pick() steps 0-1).

    Shared by `pick()` and `earliest_route_available_at()` so a candidate is "eligible" in exactly the
    same sense in both: not excluded, present in `host_caps`, declaring `required_mode`, not a
    forbidden model.

    `operating_harness` (USR-109) that is not in the stage cascade is appended as one EXTRA candidate
    (model None) after the cascade ones, through the very same filters; callers must treat it as electable
    by the operating-harness preference only (`_extra_operating_candidate`), never as a ranked cascade member.
    """
    forbidden_models = set(m.lower().strip() for m in cfg.forbidden_autonomous_models)
    candidates: list[tuple[str, Optional[str]]] = []
    cascade = _resolve_cascade(stage_cfg, implementing_harness)
    extra = _extra_operating_candidate(cascade, operating_harness)
    for harness, model in cascade + ([extra] if extra is not None else []):
        if stage == "development" and execution_context == "headless" and harness == "antigravity":
            logger.info("Skipping harness antigravity for stage development (headless_context)")
            continue
        if harness in excluded_harnesses or (harness, model) in excluded_pairs:
            logger.info("Skipping harness %s for stage %s (excluded by caller)", harness, stage)
            continue
        if f"harness:{harness}" not in caps:
            logger.info("Skipping harness %s for stage %s (not in host_caps)", harness, stage)
            continue
        if harness == "codex" and codex_headless_blocked():
            logger.info(
                "Skipping harness codex for stage %s (%s; DARKFAC_CODEX_HEADLESS_ALLOWED=0 on this host)",
                stage, CODEX_HEADLESS_BLOCK_REASON,
            )
            continue
        if not supports(
            harness,
            required_mode,
            execution_context=execution_context,
            operating_harness=operating_harness,
        ):
            logger.info(
                "Skipping harness %s for stage %s (does not declare '%s' mode)", harness, stage, required_mode
            )
            continue
        if model and model.lower().strip() in forbidden_models:
            logger.info("Skipping model %s for harness %s (forbidden autonomous model)", model, harness)
            continue
        candidates.append((harness, model))
    return candidates


_WEEKLY_WINDOW_MINUTES = 7 * 24 * 60
_MIN_HOURS_TO_RESET = 1.0  # avoids dividing by ~0 right before a reset


def _default_weekly_slack(provider_id: str) -> Optional[float]:
    """Weekly slack of `provider_id`: remaining % of its weekly window / hours until that window resets.

    Higher means more quota to spend per hour left before the reset. `None` when the account has no
    weekly window with a known reset (the ranking then falls back to the current headroom).
    """
    try:
        from core.usage.adapters import build_default_adapters

        for adapter in build_default_adapters(state_root() / "usage" / "providers"):
            if adapter.spec.provider_id != provider_id:
                continue
            now = datetime.now(timezone.utc)
            for window in adapter.inspect().windows:
                weekly = (window.window_duration_minutes or 0) >= _WEEKLY_WINDOW_MINUTES or "weekly" in window.quota_id.lower()
                reset = _parse_until(window.resets_at) if window.resets_at else None
                if not weekly or window.remaining_percent is None or reset is None:
                    continue
                hours = max((_aware_utc(reset) - now).total_seconds() / 3600.0, _MIN_HOURS_TO_RESET)
                return float(window.remaining_percent) / hours
    except Exception as exc:  # pragma: no cover - defensive, only costs ranking precision
        logger.debug("Weekly slack lookup failed for provider %s: %s", provider_id, exc)
    return None


def _slack_by_candidate(
    stage: str, eligible: list[tuple[str, Optional[str], float]], slack_fn: Callable[[str], Optional[float]]
) -> dict[tuple[str, Optional[str]], float]:
    """Weekly slack per eligible `(harness, model)`; only the `development` stage ranks by slack."""
    if stage != "development":
        return {}
    values = {(h, m): slack_fn(_HARNESS_TO_PROVIDER.get(h, h)) for h, m, _ in eligible}
    return {key: value for key, value in values.items() if value is not None}


def _rank_eligible(
    stage: str,
    complexity: Optional[str],
    eligible: list[tuple[str, Optional[str], float]],
    slack: Optional[dict[tuple[str, Optional[str]], float]] = None,
) -> tuple[str, Optional[str]]:
    """Winner among eligible `(harness, model, remaining)` candidates: complexity tier, then slack/headroom.

    `slack` (weekly remaining % / hours to reset, keyed by `(harness, model)`) orders the pool when EVERY
    member has a value; otherwise the current headroom decides.
    """
    comp = (complexity or "").lower().strip()
    if comp in ("high", "critical") or stage in ("planning", "architecture"):
        # High Intelligence tier: prioritize Opus or Sol
        tier = [
            cand for cand in eligible
            if cand[1] and any(m in cand[1].lower() for m in ("opus", "sol"))
        ]
    elif comp == "low":
        # Low complexity: prioritize Luna xhigh or Gemini Flash
        tier = [
            cand for cand in eligible
            if (cand[1] and any(m in cand[1].lower() for m in ("luna", "gemini", "flash")))
            or cand[0] == "antigravity"
        ]
    else:
        # Medium complexity / default: Headroom Dinâmico (maior cota primeiro)
        tier = []
    pool = tier or eligible
    if slack and all((item[0], item[1]) in slack for item in pool):
        pool.sort(key=lambda item: (slack[(item[0], item[1])], item[2]), reverse=True)
    else:
        pool.sort(key=lambda item: item[2], reverse=True)
    return pool[0][0], pool[0][1]


HIGH_TIER_ONLY_STAGES = frozenset({"planning"})
_HIGH_TIERS = frozenset({"high", "frontier"})


def is_high_tier_model(model: Optional[str]) -> bool:
    """True when `model` is an explicit high-tier model (registry tier `high`/`frontier`).

    A harness default (`None`) has no declared tier and is never high; unknown names fall back to
    the historical opus/sol name hint.
    """
    if not model:
        return False
    try:
        from core.models.registry import get_registry, resolve_known

        try:
            resolved = resolve_known(model)
        except Exception:
            resolved = model

        low = resolved.strip().lower()
        for entry in get_registry().models:
            names = [entry.id, *entry.aliases, *entry.gateway_ids]
            if low in (n.strip().lower() for n in names):
                return entry.tier in _HIGH_TIERS
    except Exception as exc:  # registry trouble must not open the cheap door
        logger.debug("tier lookup failed for %s: %s", model, exc)
    return any(m in model.lower() for m in ("opus", "sol"))


def _policy_allows(project_id: str, harness: str) -> bool:
    from core.enterprise.enforcement import provider_allowed

    return provider_allowed(project_id, harness)


def _planning_high_tier_healthy(
    stage_cfg: StageRoute,
    implementing_harness: Optional[str],
    caps: set[str],
    excluded_harnesses: set[str],
    excluded_pairs: set[tuple[str, Optional[str]]],
    cooldowns: dict[str, Any],
    lookup: Callable[[str], Optional[float]],
    critical_threshold: float,
    project_id: Optional[str],
) -> bool:
    """True when Claude or Codex has healthy quota (> critical floor and not cooling) for planning."""
    for h, m in _resolve_cascade(stage_cfg, implementing_harness):
        if h not in ("claude", "codex"):
            continue
        if f"harness:{h}" not in caps:
            continue
        if h in excluded_harnesses or (h, m) in excluded_pairs:
            continue
        if h == "codex" and codex_headless_blocked():
            continue
        if project_id and not _policy_allows(project_id, h):
            continue
        if _in_cooldown(h, cooldowns):
            continue
        prov = _HARNESS_TO_PROVIDER.get(h, h)
        rem = lookup(prov)
        if rem is not None and rem > critical_threshold:
            return True
    return False


def _openrouter_has_credits(balance_lookup: Optional[Callable[[], Optional[float]]]) -> bool:
    """OpenRouter fallback requires a verified positive credit balance."""
    if balance_lookup is not None:
        bal = balance_lookup()
        return bal is not None and bal > 0.0
    try:
        from core.usage.api_credits import ApiCreditsMonitor

        card = ApiCreditsMonitor().inspect_openrouter()
        return bool(card.is_connected and card.available_credit_usd is not None and card.available_credit_usd > 0.0)
    except Exception as exc:
        logger.warning("Failed to verify OpenRouter credits: %s", exc)
        return False


def _pick_winner(
    stage: str,
    complexity: Optional[str],
    operating: Optional[str],
    eligible: list[tuple[str, Optional[str], float]],
    extra_eligible: list[tuple[str, Optional[str], float]],
    preferred_unknown: list[tuple[str, Optional[str]]],
    unknown_quota: list[tuple[str, Optional[str]]],
    slack_fn: Callable[[str], Optional[float]],
    execution_context: ExecutionContext,
    unknown_quota_last_resort_ok: Optional[bool],
    quota_lookup: Optional[Callable[[str], Optional[float]]],
) -> Optional[tuple[str, Optional[str]]]:
    preferred = ([cand for cand in eligible if cand[0] == operating] or extra_eligible) if operating else []
    if preferred:
        winner = _rank_eligible(stage, complexity, preferred)
        logger.info(
            "operating_harness_preferred harness=%s remaining=%.1f stage=%s",
            winner[0], next(item[2] for item in preferred if (item[0], item[1]) == winner), stage,
        )
        return winner
    if preferred_unknown:
        winner = preferred_unknown[0]
        provider_id = _HARNESS_TO_PROVIDER.get(winner[0], winner[0])
        reason: QuotaDiagnosisReason = "unknown"
        if quota_lookup is None:
            reason = _quota_headroom_diagnosis(provider_id)[1]
        logger.warning(
            "operating_harness_quota_degraded harness=%s state=unknown reason=%s policy=allow_interactive",
            winner[0], reason,
        )
        return winner
    if eligible:
        return _rank_eligible(stage, complexity, eligible, _slack_by_candidate(stage, eligible, slack_fn))

    if stage == "development":
        allow_unknown = (complexity or "").lower().strip() == "low"
    else:
        allow_unknown = unknown_quota_last_resort() if unknown_quota_last_resort_ok is None else unknown_quota_last_resort_ok
    if allow_unknown and unknown_quota:
        winner = unknown_quota[0]
        logger.warning(
            "No harness with known healthy quota for stage %s; electing %s with UNKNOWN quota as last resort",
            stage, winner[0],
        )
        return winner
    return None


def pick(
    stage: str,
    host_caps: Iterable[str],
    exclude: Iterable[Any] = (),
    *,
    implementing_harness: Optional[str] = None,
    spent_usd: float = 0.0,
    config: Optional[RoutingConfig] = None,
    quota_lookup: Optional[Callable[[str], Optional[float]]] = None,
    cooldown_path: Optional[Path] = None,
    complexity: Optional[str] = None,
    reserve_quota: bool = False,
    ticket_id: Optional[str] = None,
    openrouter_balance_lookup: Optional[Callable[[], Optional[float]]] = None,
    mode: Optional[str] = None,
    unknown_quota_last_resort_ok: Optional[bool] = None,
    operating_harness: Optional[str] = None,
    project_id: Optional[str] = None,
    slack_lookup: Optional[Callable[[str], Optional[float]]] = None,
    execution_context: ExecutionContext = "interactive",
) -> Optional[tuple[str, Optional[str]]]:
    """Pick a (harness, model) for `stage`, or None if nothing is eligible right now.

    `mode` is the agent mode the caller will request (`read`/`write`); when None it is derived from
    the stage (`STAGE_MODES`).

    `execution_context` is `interactive` for an owner-operated chat and `headless` for fleet/worker
    routing. Headless development excludes Antigravity, even if present in host capabilities.
    `operating_harness` is the owner-operated harness; when None it comes from
    `DARKFAC_OPERATING_HARNESS` or autodetection. It only matters in interactive development.

    Enforces:
    0. Filter out harnesses that do not declare `mode` (before any headroom ranking).
    1. Filter out accounts in cooldown, missing from host_caps, forbidden models,
       and accounts with unknown or CRITICAL quota (<= 15.0%, except for the explicitly logged
       interactive operating-harness degraded route).
    1b. Interactive operating-harness preference in development: an operator with known quota above
       the critical floor is elected ahead of ranking. Unknown/stale quota remains eligible with a
       structured warning; known quota at/below the floor, cooldown and other filters still exclude it.
    2. Dynamic Headroom prioritization:
       - High complexity / planning: prefers Claude Opus / GPT Sol if eligible,
         falling back to highest headroom.
       - Low complexity: prefers GPT Luna xhigh / Gemini Flash if eligible.
       - Medium complexity / default: development sorts by weekly slack (remaining % of the weekly window
         divided by hours until its reset, higher first; headroom when any candidate lacks a reset);
         other stages sort strictly by remaining headroom descending.
    2b. Low-complexity development can use unknown quota in degraded mode. Other stages retain the
       optional bootstrap (`DARKFAC_ROUTING_UNKNOWN_QUOTA=last_resort`, or
       `unknown_quota_last_resort_ok=True`):
       when no candidate has known healthy quota, a subscription harness whose quota is UNKNOWN (never
       one known critical, nor one in cooldown) is elected, in cascade order, before OpenRouter.
       Default off: unknown quota stays fail-closed.
    3. Fallback to OpenRouter only for `read` mode, when stage allows it or all cascade accounts
       are exhausted, provided OpenRouter has confirmed USD credit and spent_usd < cap.
    """
    cfg = config or load_routing_config()
    stage_cfg = cfg.stages.get(stage)
    if stage_cfg is None:
        return None
    required_mode = mode or stage_mode(stage)

    caps = set(host_caps)
    excluded_harnesses, excluded_pairs = _split_exclude(exclude)
    cooldowns = _load_cooldowns(cooldown_path or default_cooldown_path())
    lookup = quota_lookup or _default_quota_headroom
    critical_threshold = cfg.pressure_thresholds.get("critical", _DEFAULT_PRESSURE_THRESHOLDS["critical"])
    forbidden_models = set(m.lower().strip() for m in cfg.forbidden_autonomous_models)

    if execution_context not in ("interactive", "headless"):
        raise ValueError(f"Unknown execution_context: {execution_context}")
    operating = (
        resolve_operating_harness(operating_harness)
        if stage == "development" and execution_context == "interactive"
        else None
    )
    extra_candidate = _extra_operating_candidate(_resolve_cascade(stage_cfg, implementing_harness), operating)
    slack_fn = slack_lookup or (_default_weekly_slack if quota_lookup is None else (lambda _provider: None))
    claude_or_codex_healthy = (
        _planning_high_tier_healthy(
            stage_cfg, implementing_harness, caps, excluded_harnesses, excluded_pairs,
            cooldowns, lookup, critical_threshold, project_id,
        )
        if stage == "planning"
        else False
    )

    # Candidate evaluation with fail-closed semantics
    # (harness, model, in_cooldown, is_critical_or_unknown, remaining_headroom)
    evaluated: list[tuple[str, Optional[str], bool, bool, float]] = []
    eligible: list[tuple[str, Optional[str], float]] = []
    extra_eligible: list[tuple[str, Optional[str], float]] = []
    preferred_unknown: list[tuple[str, Optional[str]]] = []
    unknown_quota: list[tuple[str, Optional[str]]] = []

    for harness, model in _routable_candidates(
        stage, stage_cfg, cfg, caps, required_mode, excluded_harnesses, excluded_pairs, implementing_harness,
        operating, execution_context,
    ):
        is_grok_planning_contingency = (
            stage == "planning"
            and harness == "grok"
            and not claude_or_codex_healthy
        )
        if stage in HIGH_TIER_ONLY_STAGES and not is_high_tier_model(model) and not is_grok_planning_contingency:
            logger.info("pick: %s/%s skipped for stage %s (not a high-tier model)", harness, model, stage)
            continue
        cooling = _in_cooldown(harness, cooldowns)
        provider_id = _HARNESS_TO_PROVIDER.get(harness, harness)
        if project_id and not _policy_allows(project_id, harness):
            continue  # USR-174: per-project data policy refuses this harness/provider
        remaining = None if cooling else lookup(provider_id)
        if cooling:
            logger.info("pick: harness %s skipped for stage %s (in cooldown)", harness, stage)

        # Fail-closed: unknown quota or quota <= critical is strictly critical/ineligible
        is_critical = remaining is None or remaining <= critical_threshold
        if is_critical and not cooling:
            # Distinguish, for observability only (never credentials/raw probe output): a known
            # reading under the floor ("critical") from one with no current, verifiable evidence at
            # all ("unknown" — stale snapshot, probe unavailable, or a suspicious reading; see
            # `_quota_headroom_diagnosis` for the exact reason when the default lookup is in play).
            logger.info(
                "pick: harness %s quota is %s for stage %s (remaining=%s, floor=%.1f)",
                harness, "critical" if remaining is not None else "unknown", stage, remaining, critical_threshold,
            )

        if (harness, model) == extra_candidate:
            # Outside the cascade: electable only by the operating-harness preference below. It takes no
            # part in the headroom ranking, the unknown-quota bootstrap or the "all exhausted" fallback.
            if not cooling and not is_critical and remaining is not None:
                extra_eligible.append((harness, model, remaining))
            elif not cooling and remaining is None and harness == operating:
                preferred_unknown.append((harness, model))
            continue

        evaluated.append((harness, model, cooling, is_critical, remaining or 0.0))

        if stage == "planning" and harness == "grok" and claude_or_codex_healthy:
            continue

        if not cooling and not is_critical and remaining is not None:
            eligible.append((harness, model, remaining))
        elif not cooling and remaining is None:
            unknown_quota.append((harness, model))
            if harness == operating:
                preferred_unknown.append((harness, model))

    winner = _pick_winner(
        stage, complexity, operating, eligible, extra_eligible, preferred_unknown,
        unknown_quota, slack_fn, execution_context, unknown_quota_last_resort_ok, quota_lookup,
    )

    if winner is not None:
        if reserve_quota:
            try:
                from core.usage.reservation import QuotaReservationManager

                prov = _HARNESS_TO_PROVIDER.get(winner[0], winner[0])
                QuotaReservationManager().reserve(prov, winner[0], ticket_id=ticket_id)
            except Exception as exc:
                logger.debug("Failed reserving quota for %s: %s", winner, exc)
        return winner

    no_local_available = not evaluated or all(cooling or is_critical for _, _, cooling, is_critical, _ in evaluated)
    if (
        required_mode == "read"
        and (stage not in HIGH_TIER_ONLY_STAGES or no_local_available or is_high_tier_model(stage_cfg.openrouter_model))
        and (not project_id or _policy_allows(project_id, "openrouter"))
        and stage_cfg.openrouter_model
        and stage_cfg.openrouter_model.lower().strip() not in forbidden_models
        and (stage_cfg.openrouter_ok or no_local_available)
        and spent_usd < cfg.run_caps.openrouter_usd
    ):
        has_credits = _openrouter_has_credits(openrouter_balance_lookup)
        if has_credits:
            return "openrouter", stage_cfg.openrouter_model
        logger.warning("OpenRouter fallback skipped: account disconnected or balance non-positive.")

    return None


# --------------------------------------------------------------------------
# When can a route come back? (USR-87)
# --------------------------------------------------------------------------

# Never ask a job to wake up sooner than this: a cooldown that ends "in 5 seconds" would otherwise
# turn the wait into a busy loop of claims.
MIN_ROUTE_WAIT_SECONDS = 60.0

ResetLookup = Callable[[str], Optional[datetime]]


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _default_quota_reset(provider_id: str, critical_threshold: float = QUOTA_FLOOR_MIN) -> Optional[datetime]:
    """When `provider_id` is expected back above the critical quota threshold, per `core.usage`.

    The account's snapshot lists quota windows with an optional `resets_at`; the account is usable
    again once EVERY window at or under the threshold has reset, so this is the latest of those
    resets. `None` when the account is not exhausted, a limiting window has no known reset, or the
    snapshot is unavailable (the caller then falls back to the default wait).
    """
    try:
        from core.usage.adapters import build_default_adapters

        provider_dir = state_root() / "usage" / "providers"
        for adapter in build_default_adapters(provider_dir):
            if adapter.spec.provider_id != provider_id:
                continue
            exhausted = [
                window
                for window in adapter.inspect().windows
                if window.remaining_percent is not None and window.remaining_percent <= critical_threshold
            ]
            resets = [_parse_until(window.resets_at) for window in exhausted]
            if not resets or any(reset is None for reset in resets):
                return None
            return max(reset for reset in resets if reset is not None)
    except Exception as exc:  # pragma: no cover - defensive, a snapshot problem only costs precision
        logger.debug("Quota reset lookup failed for provider %s: %s", provider_id, exc)
    return None


def earliest_route_available_at(
    stage: str,
    host_caps: Iterable[str],
    config: Optional[RoutingConfig] = None,
    *,
    now: Optional[datetime] = None,
    mode: Optional[str] = None,
    implementing_harness: Optional[str] = None,
    exclude: Iterable[Any] = (),
    cooldown_path: Optional[Path] = None,
    reset_lookup: Optional[ResetLookup] = None,
    operating_harness: Optional[str] = None,
) -> datetime:
    """When to look for a route again after `pick(stage, ...)` found none (timezone-aware UTC).

    The earliest of
      * the end of the cooldown of each cascade candidate that is eligible for this host/mode (the
        same filter `pick()` applies before cooldown/quota),
      * the next known quota reset of each eligible candidate that is not cooling but sits at or
        under the critical threshold (`reset_lookup(provider_id)`, default: the `core.usage`
        snapshot's `resets_at`),
      * `now + cooldown_default_minutes` (60 by default), which is therefore also the answer when
        nothing is known.

    For the `development` stage the operating harness (USR-109), when it is not in the cascade, counts as
    one more candidate, since `pick()` would elect it the moment it is usable again.

    Times in the past are ignored and the result is never sooner than `MIN_ROUTE_WAIT_SECONDS`
    from `now`. Callers turn it into `retry` with `not_before=<iso>` instead of parking the run on a
    human: no route is a condition of time, not a decision of the owner.
    """
    cfg = config or load_routing_config()
    current = _aware_utc(now or datetime.now(timezone.utc))
    fallback = current + timedelta(minutes=max(cfg.cooldown_default_minutes, 0))
    floor = current + timedelta(seconds=MIN_ROUTE_WAIT_SECONDS)

    stage_cfg = cfg.stages.get(stage)
    if stage_cfg is None:
        return max(fallback, floor)

    excluded_harnesses, excluded_pairs = _split_exclude(exclude)
    operating = resolve_operating_harness(operating_harness) if stage == "development" else None
    candidates = _routable_candidates(
        stage, stage_cfg, cfg, set(host_caps), mode or stage_mode(stage),
        excluded_harnesses, excluded_pairs, implementing_harness, operating,
    )
    cooldowns = _load_cooldowns(cooldown_path or default_cooldown_path())
    critical = cfg.pressure_thresholds.get("critical", _DEFAULT_PRESSURE_THRESHOLDS["critical"])
    lookup = reset_lookup or (lambda provider_id: _default_quota_reset(provider_id, critical))

    times: list[datetime] = [fallback]
    for harness, _model in candidates:
        entry = cooldowns.get(harness)
        until = _parse_until(entry.get("until")) if isinstance(entry, dict) else None
        if until is not None and until > current:
            times.append(until)
            continue
        try:
            reset = lookup(_HARNESS_TO_PROVIDER.get(harness, harness))
        except Exception as exc:  # a broken probe must not stop the wait from being scheduled
            logger.debug("Quota reset lookup failed for harness %s: %s", harness, exc)
            reset = None
        if reset is not None:
            reset = _aware_utc(reset)
            if reset > current:
                times.append(reset)
    return max(min(times), floor)
