"""Quota-aware harness routing for the DarkFac production line (HF-27-03).

Implements the "subscription first, OpenRouter only under critical
pressure" policy from `docs/PRODUCTION_LINE_PLAN_2026-09-22.md`
(section 6). `pick()` walks a per-stage cascade of (harness, model)
pairs from `.factory/config/line_routing.json`, skipping accounts the
host cannot run, accounts in reactive cooldown (set by `record_result`
after a `rate_limited` `AgentResult`), and accounts whose quota
headroom is CRITICAL. OpenRouter is only offered as a fallback when the
stage explicitly allows it (`openrouter_ok`) or when every cascade
account is exhausted (cooldown or CRITICAL), and only within
`run_caps.openrouter_usd`.
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

REPO_ROOT = Path(__file__).resolve().parents[2]

# v0 thresholds mirrored from core.router.token_budget._pressure_for
# (guarded <= 50%, stressed <= 25%, critical <= 10% remaining). Kept here as
# the config default so `.factory/config/line_routing.json` can override
# them without touching code.
_DEFAULT_PRESSURE_THRESHOLDS: dict[str, float] = {"guarded": 50.0, "stressed": 25.0, "critical": 10.0}

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
        stages={
            "grill": StageRoute(
                cascade=[("claude", "sonnet"), ("codex", None)],
                openrouter_ok=True,
                openrouter_model=cheap_model,
            ),
            "planning": StageRoute(
                cascade=[("claude", "opus"), ("codex", None)],
                effort="high",
                openrouter_ok=False,
            ),
            "development": StageRoute(
                cascade=[("claude", "sonnet"), ("codex", None), ("grok", None), ("antigravity", None)],
                openrouter_ok=False,
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
) -> None:
    """Put `result.harness` in cooldown when the CLI reported rate_limited."""
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
# Quota headroom (optional; unknown counts as eligible)
# --------------------------------------------------------------------------


def _default_quota_headroom(provider_id: str) -> Optional[float]:
    """Best-effort real quota probe via core.usage.adapters. Never raises."""
    try:
        from core.router.token_budget import _quota_headroom
        from core.usage.adapters import build_default_adapters

        snapshot_dir = REPO_ROOT / ".factory" / "usage" / "snapshots"
        for adapter in build_default_adapters(snapshot_dir):
            if adapter.spec.provider_id == provider_id:
                return _quota_headroom(adapter.inspect())
    except Exception as exc:  # pragma: no cover - defensive, quota probing is best-effort
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
            return [("codex", None), ("grok", None)]
        if implementing_harness == "codex":
            return [("claude", None), ("grok", None)]
        # grok/antigravity/unknown implementer: default to the two Tier-1 families, then grok.
        return [("claude", None), ("codex", None), ("grok", None)]
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
) -> Optional[tuple[str, Optional[str]]]:
    """Pick a (harness, model) for `stage`, or None if nothing is eligible right now.

    Walks the stage cascade in order, skipping harnesses missing from
    `host_caps` (as `harness:<name>`), accounts in cooldown, and accounts at
    CRITICAL quota pressure (a lookup failure / unknown quota counts as
    eligible). Falls back to OpenRouter only when the stage allows it
    (`openrouter_ok`) or every cascade account considered is exhausted
    (cooldown or CRITICAL), and only while `spent_usd < run_caps.openrouter_usd`.
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

    cascade = _resolve_cascade(stage_cfg, implementing_harness)

    considered: list[tuple[str, Optional[str], bool, bool]] = []  # harness, model, in_cooldown, critical
    winner: Optional[tuple[str, Optional[str]]] = None
    for harness, model in cascade:
        if harness in excluded_harnesses or (harness, model) in excluded_pairs:
            continue
        if f"harness:{harness}" not in caps:
            continue
        cooling = _in_cooldown(harness, cooldowns)
        remaining = None if cooling else lookup(_HARNESS_TO_PROVIDER.get(harness, harness))
        critical = remaining is not None and remaining <= critical_threshold
        considered.append((harness, model, cooling, critical))
        if winner is None and not cooling and not critical:
            winner = (harness, model)

    if winner is not None:
        return winner

    all_exhausted = bool(considered) and all(cooling or critical for _, _, cooling, critical in considered)
    if (
        stage_cfg.openrouter_model
        and (stage_cfg.openrouter_ok or all_exhausted)
        and spent_usd < cfg.run_caps.openrouter_usd
    ):
        return "openrouter", stage_cfg.openrouter_model

    return None
