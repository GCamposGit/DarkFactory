"""Daily model autopilot (USR-185, slice 9b): one entry point, ``run_daily``.

Chain: retire due models -> qualified candidates -> Grill policy (blocked ones become a real WAITING_HUMAN pending) ->
promotion -> post-promotion telemetry check (rollback above the registry limits) -> one ``chore(models)`` PR with the
registry, regenerated docs and ledger. Every transition runs inside a dedicated worktree, so the shared checkout is
never edited. Scheduling lives in ``daily_hook`` (once per UTC day, after discovery; see ``core.orchestrator.state``). All collaborators
(clock, telemetry events, Telegram sender, owner-action mirror, worktree factory, PR executor) are injectable.
Re-running is idempotent: once a change is in the registry (or already pending) a second run changes nothing.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from core.models import lifecycle, pending, pr, telemetry
from core.models.registry import QUALIFIED_STATUSES, ModelRegistry, load_registry

logger = logging.getLogger(__name__)

RegistryRel = Path(".factory") / "config" / "model_registry.json"
LedgerRel = Path(".factory") / "models" / "lifecycle_ledger.jsonl"

RoleFor = Callable[[Any, ModelRegistry], Optional[str]]


@dataclass
class DailyReport:
    promoted: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)  # Grill pending registered
    skipped: dict[str, str] = field(default_factory=dict)
    retired: list[str] = field(default_factory=list)
    discovered: list[str] = field(default_factory=list)
    rolled_back: list[str] = field(default_factory=list)
    pr: Optional[pr.ModelPrResult] = None

    @property
    def changed(self) -> bool:
        return bool(self.promoted or self.retired or self.rolled_back or self.discovered)


def default_role_for(entry: Any, registry: ModelRegistry) -> Optional[str]:
    """Role currently served by a model of the same family and tier (the natural successor), else no role."""
    by_id = {m.id: m for m in registry.models}
    for role in sorted(registry.roles):
        cur = by_id.get(registry.roles[role])
        if cur is not None and cur.family == entry.family and cur.tier == entry.tier:
            return role
    return None


def _default_apply_candidates(registry_path: Path, state_dir: Optional[Path] = None) -> list[str]:
    try:
        from core.models.discovery_sources import apply_candidates
    except ImportError:  # slice 9a not merged yet: no-op
        return []
    return apply_candidates(registry_path, state_dir)


def run_daily(
    *,
    now: datetime,
    repo_root: Optional[Path] = None,
    events: Optional[Iterable[Any]] = None,
    role_for: Optional[RoleFor] = None,
    send: Optional[pending.Sender] = None,
    mirror: Optional[pending.Mirror] = None,
    pending_dir: Optional[Path] = None,
    deliver: Optional[Callable[..., pr.ModelPrResult]] = None,
    executor: Optional[pr.Executor] = None,
    workspace_factory: Optional[Callable[..., Any]] = None,
    cleanup_fn: Optional[Callable[..., Any]] = None,
    ticket_id: str = pr.MODEL_TICKET_ID,
    actor: str = "factory-autopilot",
    apply_candidates_func: Optional[Callable[..., list[str]]] = None,
    discovery_state_dir: Optional[Path] = None,
    decision_reader: Optional[pending.DecisionReader] = None,
    qualify_func: Optional[Callable[[Path, list[str]], None]] = None,
    pr_lister: Optional[pr.GhLister] = None,
) -> DailyReport:
    report = DailyReport()
    state = {"audits": 0}
    pick_role = role_for or default_role_for

    def grill(model_id: str, role: Optional[str], reasons: list[str]) -> None:
        pending.request_grill(model_id, role, reasons, now=now, send=send, mirror=mirror, pending_dir=pending_dir)

    def apply(root: Path) -> DailyReport:
        reg_path, ledger, docs = root / RegistryRel, root / LedgerRel, root
        common = {"now": now, "actor": actor, "registry_path": reg_path, "ledger_path": ledger, "docs_root": docs}
        for audit in lifecycle.retire_due(now.date(), **common):
            report.retired.append(audit["model_id"])
        applier = apply_candidates_func or _default_apply_candidates
        found = applier(reg_path, discovery_state_dir) or []  # writes only the worktree registry (slice 9a)
        if found:
            report.discovered.extend(found)
            qualifier = qualify_func
            if qualifier is None:  # default real executor (USR-341): only when OPENROUTER_API_KEY exists
                from core.models.openrouter_qualifier import make_qualify_func

                qualifier = make_qualify_func(now=now)
            if qualifier is not None:  # qualification of the new candidates, still inside the worktree
                qualifier(reg_path, list(found))
            from core.models.docgen import process

            process(root=docs, reg=load_registry(reg_path), write=True)  # docs regenerated before the PR
        registry = load_registry(reg_path)
        for entry in registry.models:
            if entry.status != "candidate" or entry.qualification is None:
                continue
            if entry.qualification.status not in QUALIFIED_STATUSES:
                continue
            verdict = pending.consume_decision(entry.id, now=now, reader=decision_reader, pending_dir=pending_dir)
            if verdict == "rejected":
                report.skipped[entry.id] = "grill_rejected: the owner refused the promotion"
                continue
            try:
                lifecycle.promote(entry.id, role=pick_role(entry, registry), grill_handler=grill,
                                  grill_approved=verdict == "approved", **common)
                report.promoted.append(entry.id)
            except lifecycle.LifecycleError as exc:
                if "WAITING_HUMAN" in str(exc):
                    report.blocked.append(entry.id)
                else:
                    report.skipped[entry.id] = str(exc)
            registry = load_registry(reg_path)
        stats = telemetry.collect_telemetry(registry, now=now, events=events)
        for decision in telemetry.evaluate_post_promotion(
            registry, stats, **{k: v for k, v in common.items() if k != "now"}, now=now,
        ):
            if decision.applied:
                report.rolled_back.append(decision.model_id)
            else:
                report.skipped[decision.model_id] = decision.details
        state["audits"] = len(report.promoted) + len(report.discovered) + len(report.retired) + len(report.rolled_back)
        return report

    def summary_text() -> str:
        parts = [f"descobrir {', '.join(report.discovered)}" if report.discovered else "",
                 f"promover {', '.join(report.promoted)}" if report.promoted else "",
                 f"rollback {', '.join(report.rolled_back)}" if report.rolled_back else "",
                 f"aposentar {', '.join(report.retired)}" if report.retired else ""]
        return "; ".join(p for p in parts if p) or "atualizar registro de modelos"

    if deliver is not None:
        send_pr = deliver
    else:
        def send_pr(summary: str, apply_fn: pr.Apply, **kw: Any) -> pr.ModelPrResult:
            return pr.deliver_model_change(summary, apply_fn, **kw)

    result = send_pr(
        summary_text,
        apply,
        repo_root=repo_root, ticket_id=ticket_id, executor=executor,
        workspace_factory=workspace_factory, cleanup_fn=cleanup_fn,
        has_changes=lambda _p: state["audits"] > 0,
        **({"pr_lister": pr_lister} if pr_lister is not None else {}),
    )
    report.pr = result
    return report


def _marker_path(state_dir: Optional[Path] = None) -> Path:
    from core.paths import state_root

    return (state_dir or state_root()) / "model_autopilot" / "last_run.json"


def already_ran_today(day: str, state_dir: Optional[Path] = None) -> bool:
    try:
        return json.loads(_marker_path(state_dir).read_text(encoding="utf-8")).get("date") == day
    except (OSError, ValueError):
        return False


_HOOK_THREAD: Optional[threading.Thread] = None
_HOOK_LOCK = threading.Lock()


def daily_hook_async(**kwargs: Any) -> Optional[threading.Thread]:
    """Start ``daily_hook`` in a daemon thread (at most one alive per process) so dispatch never waits for it.

    Returns the new thread, or ``None`` when one is still running or today's run is already done.
    """
    global _HOOK_THREAD
    with _HOOK_LOCK:
        if _HOOK_THREAD is not None and _HOOK_THREAD.is_alive():
            return None
        day = (kwargs.get("now") or datetime.now(timezone.utc)).date().isoformat()
        if already_ran_today(day, kwargs.get("state_dir")):
            return None
        thread = threading.Thread(target=daily_hook, kwargs=kwargs, name="model-autopilot", daemon=True)
        _HOOK_THREAD = thread
        thread.start()
        return thread


def daily_hook(
    *, now: Optional[datetime] = None, state_dir: Optional[Path] = None,
    runner: Optional[Callable[..., DailyReport]] = None, **kwargs: Any,
) -> Optional[DailyReport]:
    """Scheduled entry: ``run_daily`` at most once per UTC day, never raising. Returns ``None`` when already done.

    It runs after the daily discovery (``core.orchestrator.state``). The marker lives in the state root (not
    versioned) and is written even when the run failed, so a broken day is not retried on every dispatch; the
    next UTC day tries again. The registry change only ever happens inside ``run_daily``'s own worktree.
    """
    moment = now or datetime.now(timezone.utc)
    day = moment.date().isoformat()
    if already_ran_today(day, state_dir):
        return None
    report: Optional[DailyReport] = None
    status = "ok"
    try:
        report = (runner or run_daily)(now=moment, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the dispatch loop must never die because of the model autopilot
        logger.exception("daily model autopilot failed")
        status = f"error: {type(exc).__name__}"
    try:
        marker = _marker_path(state_dir)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"date": day, "status": status}) + chr(10), encoding="utf-8", newline=chr(10))
    except OSError as exc:
        logger.warning("could not write the autopilot marker: %s", exc)
    return report
