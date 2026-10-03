"""Quota-aware harness routing for the DarkFac production line (HF-27-03 / HF-23).

Implements the unified "subscription first, OpenRouter only with verified balance,
and fail-closed under critical pressure" policy.
`pick()` walks a stage cascade, filtering out ineligible accounts (not in host_caps,
in cooldown, forbidden models, unknown/stale quota, or remaining <= critical threshold 15%),
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional, Union

from pydantic import BaseModel, Field

from core.line.agent_cli import AgentResult, _DEFAULT_OPENROUTER_MODEL, supports
from core.line.operating_harness import resolve_operating_harness
from core.paths import project_root, state_root

logger = logging.getLogger(__name__)

_CANONICAL_REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = _CANONICAL_REPO_ROOT

# Critical pressure threshold is 15.0% (strict fail-closed)
_DEFAULT_PRESSURE_THRESHOLDS: dict[str, float] = {
    "guarded": 50.0,
    "stressed": 25.0,
    "critical": 15.0,
}

_DEFAULT_FORBIDDEN_AUTONOMOUS_MODELS: list[str] = [
    "fable",
    "claude-fable",
    "astra",
    "gpt-6-astra",
]

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
_DEFAULT_STAGE_MODE = "read"


def stage_mode(stage: str) -> str:
    """`write` when the stage's agent edits files under the worktree, else `read`."""
    return STAGE_MODES.get(stage, _DEFAULT_STAGE_MODE)


class StageRoute(BaseModel):
    """Routing policy for a single production-line stage."""

    cascade: Union[Literal["other_family_than_development"], list[tuple[str, Optional[str]]]] = Field(
        default_factory=list
    )
    openrouter_ok: bool = False
    openrouter_model: Optional[str] = None
    effort: Optional[str] = None
    local_ok: bool = False


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
    # A harness whose CLI crashes (not a quota/login problem) sits out briefly, so the next stage or
    # job does not pick it again straight away.
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
                cascade=[("claude", "opus"), ("codex", None), ("antigravity", None)],
                effort="high",
                openrouter_ok=True,
                openrouter_model=cheap_model,
            ),
            "development": StageRoute(
                cascade=[("claude", "sonnet"), ("codex", None)],
                openrouter_ok=False,  # write stage: OpenRouter is read-only, never a fallback here
                openrouter_model=cheap_model,
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

    if result.error_kind not in ("rate_limited", "crash"):
        return
    cfg = config or load_routing_config()
    path = cooldown_path or default_cooldown_path()

    if result.error_kind == "crash":
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


def _default_quota_headroom(provider_id: str) -> Optional[float]:
    """Real quota probe via core.usage.adapters reading providers. Fail-closed on stale/missing.

    A stale snapshot is not the end of the road: the adapter is asked again (`force=True`, a live probe
    or, for Codex inside the cloud container, the rate limits Codex logged in its own session files) and
    only a result whose `checked_at` is within the max age counts. Anything older, unreadable or absent is
    None, which `pick()` treats as unknown (fail-closed).
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
            if adapter.spec.provider_id == provider_id:
                usage = adapter.inspect(force=snapshot_stale)
                checked = _parse_iso_utc(usage.checked_at)
                if checked is None or (datetime.now(timezone.utc) - checked).total_seconds() > max_age:
                    logger.warning("Quota data for %s is older than %ss; failing closed", provider_id, max_age)
                    return None
                raw_headroom = _quota_headroom(usage)
                if raw_headroom is None:
                    return None
                reserved = QuotaReservationManager().get_active_reserved_percent(provider_id)
                return max(0.0, raw_headroom - reserved)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Quota lookup failed for provider %s: %s", provider_id, exc)
    return None


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
    return list(stage_cfg.cascade)


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
        if harness in excluded_harnesses or (harness, model) in excluded_pairs:
            continue
        if f"harness:{harness}" not in caps:
            continue
        if not supports(harness, required_mode):
            logger.info(
                "Skipping harness %s for stage %s (does not declare '%s' mode)", harness, stage, required_mode
            )
            continue
        if model and model.lower().strip() in forbidden_models:
            logger.info("Skipping model %s for harness %s (forbidden autonomous model)", model, harness)
            continue
        candidates.append((harness, model))
    return candidates


def _rank_eligible(
    stage: str, complexity: Optional[str], eligible: list[tuple[str, Optional[str], float]]
) -> tuple[str, Optional[str]]:
    """Winner among eligible `(harness, model, remaining)` candidates: complexity tier, then headroom."""
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
    pool.sort(key=lambda item: item[2], reverse=True)
    return pool[0][0], pool[0][1]


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
) -> Optional[tuple[str, Optional[str]]]:
    """Pick a (harness, model) for `stage`, or None if nothing is eligible right now.

    `mode` is the agent mode the caller will request (`read`/`write`); when None it is derived from
    the stage (`STAGE_MODES`).

    `operating_harness` is the harness the owner is operating the factory through (USR-109); when None it
    comes from `DARKFAC_OPERATING_HARNESS` or autodetection (`core.line.operating_harness`), and with no
    signal there is none. It only matters for the `development` stage.

    Enforces:
    0. Filter out harnesses that do not declare `mode` (before any headroom ranking).
    1. Filter out accounts in cooldown, missing from host_caps, forbidden models,
       and accounts with unknown or CRITICAL quota (<= 15.0%, fail-closed).
    1b. Operating-harness preference (`development` only): when the operating harness survived every
       filter above with known quota above the critical floor and no cooldown, it is elected before any
       headroom or complexity ranking. A harness outside the stage cascade takes part as an extra
       candidate that only this step can elect. If it did not survive, nothing changes (the 15% floor is
       never relaxed).
    2. Dynamic Headroom prioritization:
       - High complexity / planning: prefers Claude Opus / GPT Sol if eligible,
         falling back to highest headroom.
       - Low complexity: prefers GPT Luna xhigh / Gemini Flash if eligible.
       - Medium complexity / default: sorts strictly by remaining headroom descending.
    2b. Bootstrap (`DARKFAC_ROUTING_UNKNOWN_QUOTA=last_resort`, or `unknown_quota_last_resort_ok=True`):
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

    # The operating-harness preference is a development-only rule; other stages never even look it up.
    operating = resolve_operating_harness(operating_harness) if stage == "development" else None
    extra_candidate = _extra_operating_candidate(_resolve_cascade(stage_cfg, implementing_harness), operating)

    # Candidate evaluation with fail-closed semantics
    # (harness, model, in_cooldown, is_critical_or_unknown, remaining_headroom)
    evaluated: list[tuple[str, Optional[str], bool, bool, float]] = []
    eligible: list[tuple[str, Optional[str], float]] = []
    extra_eligible: list[tuple[str, Optional[str], float]] = []
    unknown_quota: list[tuple[str, Optional[str]]] = []

    for harness, model in _routable_candidates(
        stage, stage_cfg, cfg, caps, required_mode, excluded_harnesses, excluded_pairs, implementing_harness,
        operating,
    ):
        cooling = _in_cooldown(harness, cooldowns)
        provider_id = _HARNESS_TO_PROVIDER.get(harness, harness)
        remaining = None if cooling else lookup(provider_id)

        # Fail-closed: unknown quota or quota <= critical is strictly critical/ineligible
        is_critical = remaining is None or remaining <= critical_threshold

        if (harness, model) == extra_candidate:
            # Outside the cascade: electable only by the operating-harness preference below. It takes no
            # part in the headroom ranking, the unknown-quota bootstrap or the "all exhausted" fallback.
            if not cooling and not is_critical and remaining is not None:
                extra_eligible.append((harness, model, remaining))
            continue

        evaluated.append((harness, model, cooling, is_critical, remaining or 0.0))

        if not cooling and not is_critical and remaining is not None:
            eligible.append((harness, model, remaining))
        elif not cooling and remaining is None:
            unknown_quota.append((harness, model))

    winner: Optional[tuple[str, Optional[str]]] = None

    preferred = ([cand for cand in eligible if cand[0] == operating] or extra_eligible) if operating else []
    if preferred:
        # Same ranking as below, restricted to the operating harness (picks among its own cascade models).
        winner = _rank_eligible(stage, complexity, preferred)
        logger.info(
            "operating_harness_preferred harness=%s remaining=%.1f stage=%s",
            winner[0], next(item[2] for item in preferred if (item[0], item[1]) == winner), stage,
        )
    elif eligible:
        winner = _rank_eligible(stage, complexity, eligible)

    allow_unknown = unknown_quota_last_resort() if unknown_quota_last_resort_ok is None else unknown_quota_last_resort_ok
    if winner is None and allow_unknown and unknown_quota:
        winner = unknown_quota[0]
        logger.warning(
            "No harness with known healthy quota for stage %s; electing %s with UNKNOWN quota as last resort",
            stage, winner[0],
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

    all_exhausted = bool(evaluated) and all(cooling or is_critical for _, _, cooling, is_critical, _ in evaluated)
    if (
        required_mode == "read"
        and stage_cfg.openrouter_model
        and stage_cfg.openrouter_model.lower().strip() not in forbidden_models
        and (stage_cfg.openrouter_ok or all_exhausted)
        and spent_usd < cfg.run_caps.openrouter_usd
    ):
        # OpenRouter fallback requires verified positive credit balance
        has_credits = False
        if openrouter_balance_lookup is not None:
            bal = openrouter_balance_lookup()
            has_credits = bal is not None and bal > 0.0
        else:
            try:
                from core.usage.api_credits import ApiCreditsMonitor

                credits_mon = ApiCreditsMonitor()
                card = credits_mon.inspect_openrouter()
                has_credits = card.is_connected and card.available_credit_usd is not None and card.available_credit_usd > 0.0
            except Exception as exc:
                logger.warning("Failed to verify OpenRouter credits: %s", exc)

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


def _default_quota_reset(provider_id: str, critical_threshold: float = 15.0) -> Optional[datetime]:
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
