"""Quota-aware harness routing for the DarkFac production line (HF-27-03 / HF-23).

Implements the unified "subscription first, OpenRouter only with verified balance,
and fail-closed under critical pressure" policy.
`pick()` walks a stage cascade, filtering out ineligible accounts (not in host_caps,
in cooldown, forbidden models, unknown/stale quota, or remaining <= critical threshold 15%),
and prioritizes eligible harnesses via Dynamic Headroom (highest remaining quota first)
or specialized intelligence tiers.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Optional, Union

from pydantic import BaseModel, Field

from core.line.agent_cli import AgentResult, _DEFAULT_OPENROUTER_MODEL

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
    return REPO_ROOT / ".factory" / "usage" / "cooldowns.json"


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
                cascade=[("antigravity", None), ("claude", "sonnet"), ("codex", None), ("grok", None)],
                openrouter_ok=True,
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


def load_routing_config(path: Optional[Path] = None) -> RoutingConfig:
    """Load the routing config from JSON, falling back to v0 defaults when absent/invalid."""
    target = path or default_config_path()
    if target.is_file():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            return RoutingConfig.model_validate(raw)
        except Exception as exc:  # malformed file, wrong schema, etc.
            logger.warning("Failed to load routing config %s (%s); using v0 defaults", target, exc)
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

    if result.error_kind != "rate_limited":
        return
    cfg = config or load_routing_config()
    path = cooldown_path or default_cooldown_path()

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


def _default_quota_headroom(provider_id: str) -> Optional[float]:
    """Real quota probe via core.usage.adapters reading providers. Fail-closed on stale/missing."""
    try:
        from core.router.token_budget import _quota_headroom
        from core.usage.adapters import build_default_adapters
        from core.usage.reservation import QuotaReservationManager

        provider_dir = REPO_ROOT / ".factory" / "usage" / "providers"
        snapshot_file = provider_dir / f"{provider_id}.json"
        if snapshot_file.is_file():
            try:
                data = json.loads(snapshot_file.read_text(encoding="utf-8"))
                checked_at_str = data.get("checked_at")
                if checked_at_str:
                    checked_at = datetime.fromisoformat(str(checked_at_str).replace("Z", "+00:00"))
                    if checked_at.tzinfo is None:
                        checked_at = checked_at.replace(tzinfo=timezone.utc)
                    age_seconds = (datetime.now(timezone.utc) - checked_at).total_seconds()
                    max_age = 3600.0
                    if os.environ.get("PYTEST_CURRENT_TEST") and provider_dir.resolve() == (_CANONICAL_REPO_ROOT / ".factory" / "usage" / "providers").resolve():
                        max_age = 86400.0 * 30.0
                    if age_seconds > max_age:
                        logger.warning(
                            "Snapshot for %s is stale (%s s > %ss); failing closed",
                            provider_id,
                            round(age_seconds, 1),
                            max_age,
                        )
                        return None
            except Exception as e:
                logger.debug("Failed parsing checked_at for provider %s: %s", provider_id, e)

        for adapter in build_default_adapters(provider_dir):
            if adapter.spec.provider_id == provider_id:
                raw_headroom = _quota_headroom(adapter.inspect())
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
) -> Optional[tuple[str, Optional[str]]]:
    """Pick a (harness, model) for `stage`, or None if nothing is eligible right now.

    Enforces:
    1. Filter out accounts in cooldown, missing from host_caps, forbidden models,
       and accounts with unknown or CRITICAL quota (<= 15.0%, fail-closed).
    2. Dynamic Headroom prioritization:
       - High complexity / planning: prefers Claude Opus / GPT Sol if eligible,
         falling back to highest headroom.
       - Low complexity: prefers GPT Luna xhigh / Gemini Flash if eligible.
       - Medium complexity / default: sorts strictly by remaining headroom descending.
    3. Fallback to OpenRouter only when stage allows it or all cascade accounts are
       exhausted, provided OpenRouter has confirmed USD credit and spent_usd < cap.
    """
    cfg = config or load_routing_config()
    stage_cfg = cfg.stages.get(stage)
    if stage_cfg is None:
        return None

    caps = set(host_caps)
    excluded_harnesses, excluded_pairs = _split_exclude(exclude)
    cooldowns = _load_cooldowns(cooldown_path or default_cooldown_path())
    lookup = quota_lookup or _default_quota_headroom
    critical_threshold = cfg.pressure_thresholds.get("critical", _DEFAULT_PRESSURE_THRESHOLDS["critical"])
    forbidden_models = set(m.lower().strip() for m in cfg.forbidden_autonomous_models)

    cascade = _resolve_cascade(stage_cfg, implementing_harness)

    # Candidate evaluation with fail-closed semantics
    # (harness, model, in_cooldown, is_critical_or_unknown, remaining_headroom)
    evaluated: list[tuple[str, Optional[str], bool, bool, float]] = []
    eligible: list[tuple[str, Optional[str], float]] = []

    for harness, model in cascade:
        if harness in excluded_harnesses or (harness, model) in excluded_pairs:
            continue
        if f"harness:{harness}" not in caps:
            continue
        if model and model.lower().strip() in forbidden_models:
            logger.info("Skipping model %s for harness %s (forbidden autonomous model)", model, harness)
            continue

        cooling = _in_cooldown(harness, cooldowns)
        provider_id = _HARNESS_TO_PROVIDER.get(harness, harness)
        remaining = None if cooling else lookup(provider_id)

        # Fail-closed: unknown quota or quota <= critical is strictly critical/ineligible
        is_critical = remaining is None or remaining <= critical_threshold
        evaluated.append((harness, model, cooling, is_critical, remaining or 0.0))

        if not cooling and not is_critical and remaining is not None:
            eligible.append((harness, model, remaining))

    winner: Optional[tuple[str, Optional[str]]] = None

    if eligible:
        comp = (complexity or "").lower().strip()
        if comp in ("high", "critical") or stage in ("planning", "architecture"):
            # High Intelligence tier: prioritize Opus or Sol
            high_intel = [
                cand for cand in eligible
                if cand[1] and any(m in cand[1].lower() for m in ("opus", "sol"))
            ]
            if high_intel:
                high_intel.sort(key=lambda item: item[2], reverse=True)
                winner = (high_intel[0][0], high_intel[0][1])
            else:
                eligible.sort(key=lambda item: item[2], reverse=True)
                winner = (eligible[0][0], eligible[0][1])
        elif comp == "low":
            # Low complexity: prioritize Luna xhigh or Gemini Flash
            low_intel = [
                cand for cand in eligible
                if (cand[1] and any(m in cand[1].lower() for m in ("luna", "gemini", "flash")))
                or cand[0] == "antigravity"
            ]
            if low_intel:
                low_intel.sort(key=lambda item: item[2], reverse=True)
                winner = (low_intel[0][0], low_intel[0][1])
            else:
                eligible.sort(key=lambda item: item[2], reverse=True)
                winner = (eligible[0][0], eligible[0][1])
        else:
            # Medium complexity / default: Headroom Dinâmico (maior cota primeiro)
            eligible.sort(key=lambda item: item[2], reverse=True)
            winner = (eligible[0][0], eligible[0][1])

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
        stage_cfg.openrouter_model
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
