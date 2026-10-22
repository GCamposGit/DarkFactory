"""Per-model telemetry and post-promotion rollback decisions (USR-185, slice 9b).

Source choice: the model-call ledger (``core.usage.ledger.ModelUsageLedger``, one ``ModelCallEvent`` per attempt with
``model``, ``success``, ``cost_usd``, ``ticket_id`` and a timestamp, de-duplicated by ``invocation_id``). It is the only
store that records the model, the outcome AND the cost of every call. ``core.workflow.execution_registry`` records the
model per execution but no cost or per-attempt outcome, and ``.factory/telemetry`` / ``telemetry.db`` carry harness
level data, not the model id per run, so they are not used here.

Escalation: on one ticket, a failed call on model A followed by a later call on a different model B counts as one
escalation charged to A. Verdicts need ``rollback_thresholds.min_samples`` calls in the window, so one failure never
reverts a promotion. Rollback is delegated to ``lifecycle.rollback`` (audited, docs regenerated); the executor of the
rollback is injectable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from core.models import lifecycle
from core.models.registry import ModelRegistry, id_of

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelStats:
    model_id: str
    calls: int = 0
    failures: int = 0
    escalations: int = 0
    cost_usd: float = 0.0

    @property
    def failure_rate(self) -> float:
        return self.failures / self.calls if self.calls else 0.0

    @property
    def escalation_rate(self) -> float:
        return self.escalations / self.calls if self.calls else 0.0

    @property
    def cost_per_call_usd(self) -> float:
        return self.cost_usd / self.calls if self.calls else 0.0


Telemetry = Mapping[str, ModelStats]


@dataclass(frozen=True)
class RollbackDecision:
    model_id: str
    reason: str  # one of lifecycle.ROLLBACK_REASONS
    details: str
    stats: ModelStats
    applied: bool = False
    audits: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def _ts(value: Any) -> datetime:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _get(event: Any, key: str) -> Any:
    if isinstance(event, Mapping):
        return event.get(key)
    return getattr(event, key, None)


def aggregate_events(
    events: Iterable[Any],
    *,
    since: Optional[datetime] = None,
    registry: Optional[ModelRegistry] = None,
) -> dict[str, ModelStats]:
    """Aggregate ledger events (mappings or ``ModelCallEvent``) by canonical model id."""
    rows: list[tuple[datetime, str, str, bool, float, Optional[str]]] = []
    for ev in events:
        raw_model = str(_get(ev, "model") or "").strip()
        if not raw_model:
            continue
        when = _ts(_get(ev, "timestamp"))
        if since is not None and when < since:
            continue
        try:
            model = id_of(raw_model, registry) if registry is not None else raw_model
        except Exception:  # noqa: BLE001 - unknown aliases keep their raw name
            model = raw_model
        ok = _get(ev, "success")
        rows.append((when, model, str(_get(ev, "invocation_id") or ""), True if ok is None else bool(ok),
                     float(_get(ev, "cost_usd") or 0.0), _get(ev, "ticket_id") or None))
    calls: dict[str, int] = {}
    fails: dict[str, int] = {}
    cost: dict[str, float] = {}
    esc: dict[str, int] = {}
    by_ticket: dict[str, list[tuple[datetime, str, bool]]] = {}
    for when, model, _inv, ok, c, ticket in rows:
        calls[model] = calls.get(model, 0) + 1
        fails[model] = fails.get(model, 0) + (0 if ok else 1)
        cost[model] = cost.get(model, 0.0) + c
        if ticket:
            by_ticket.setdefault(str(ticket), []).append((when, model, ok))
    for seq in by_ticket.values():
        seq.sort(key=lambda r: r[0])
        for i, (_w, model, ok) in enumerate(seq):
            if ok:
                continue
            if any(later_model != model for _lw, later_model, _lo in seq[i + 1:]):
                esc[model] = esc.get(model, 0) + 1
    return {
        m: ModelStats(m, calls[m], fails.get(m, 0), esc.get(m, 0), round(cost.get(m, 0.0), 8)) for m in calls
    }


def load_usage_events(storage_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """Durable invocations of the model-call ledger (read only)."""
    from core.usage.ledger import ModelUsageLedger

    data = ModelUsageLedger(storage_dir)._store.read()  # noqa: SLF001 - same package family, read only
    return list(dict(data.get("invocations") or {}).values())


def collect_telemetry(
    registry: ModelRegistry,
    *,
    now: datetime,
    events: Optional[Iterable[Any]] = None,
    storage_dir: Optional[Path] = None,
) -> dict[str, ModelStats]:
    """Telemetry for the registry window (``rollback_thresholds.window_days``); ``events`` is injectable."""
    since = now - timedelta(days=registry.rollback_thresholds.window_days)
    source = events if events is not None else load_usage_events(storage_dir)
    return aggregate_events(source, since=since, registry=registry)


RollbackFn = Callable[..., list[dict[str, Any]]]


def _verdict(stats: ModelStats, registry: ModelRegistry) -> Optional[tuple[str, str]]:
    th = registry.rollback_thresholds
    if stats.calls < th.min_samples:
        return None
    if stats.failure_rate > th.max_failure_rate:
        return "gate_failure", f"failure rate {stats.failure_rate:.0%} > {th.max_failure_rate:.0%} ({stats.failures}/{stats.calls})"
    if stats.escalation_rate > th.max_escalation_rate:
        return "gate_failure", f"escalation rate {stats.escalation_rate:.0%} > {th.max_escalation_rate:.0%}"
    if th.max_cost_per_call_usd is not None and stats.cost_per_call_usd > th.max_cost_per_call_usd:
        return "cost", f"cost per call ${stats.cost_per_call_usd:.4f} > ${th.max_cost_per_call_usd:.4f}"
    return None


def evaluate_post_promotion(
    registry: ModelRegistry,
    telemetry: Telemetry,
    *,
    now: datetime,
    role_previous: Optional[Mapping[str, str]] = None,
    registry_path: Optional[Path] = None,
    ledger_path: Optional[Path] = None,
    docs_root: Optional[Path] = None,
    rollback_fn: Optional[RollbackFn] = None,
    actor: str = "factory",
) -> list[RollbackDecision]:
    """Roll back every promoted model (a role with a remembered previous model) whose telemetry breaches the limits.

    ``role_previous`` defaults to the registry file's ``role_previous`` map. Models below ``min_samples`` are left alone.
    """
    if role_previous is None:
        path = registry_path or lifecycle.DEFAULT_REGISTRY_PATH
        role_previous = lifecycle._load(path).get(lifecycle.ROLE_PREVIOUS_KEY, {})  # noqa: SLF001
    do_rollback = rollback_fn or lifecycle.rollback
    decisions: list[RollbackDecision] = []
    seen: set[str] = set()
    for role, model_id in registry.roles.items():
        if role not in role_previous or model_id in seen:
            continue
        seen.add(model_id)
        stats = telemetry.get(model_id)
        verdict = _verdict(stats, registry) if stats else None
        if verdict is None:
            continue
        reason, details = verdict
        try:
            audits = do_rollback(
                model_id, reason, now=now, actor=actor,
                registry_path=registry_path, ledger_path=ledger_path, docs_root=docs_root,
            )
            decisions.append(RollbackDecision(model_id, reason, details, stats, True, tuple(audits)))
        except lifecycle.LifecycleError as exc:
            logger.warning("rollback of %s refused: %s", model_id, exc)
            decisions.append(RollbackDecision(model_id, reason, f"{details}; refused: {exc}", stats, False))
    return decisions
