"""Model lifecycle: promotion, rollback and date-based retirement (USR-185, slice 7).

Transitions (all validated by ``parse_registry`` before anything is written, all audited):
* ``promote``     candidate -> active, only with an approved qualification; optionally binds a role to the promoted
                  model and remembers the previous binding in ``role_previous`` (registry key used by rollback).
* ``rollback``    on a regression signal (gate_failure, cost, quota) the role goes back to its previous model; the
                  regressed model becomes ``deprecated`` unless another role still routes to it (then it stays active).
* ``retire_due``  deprecated -> retired when ``retirement_date`` <= today (idempotent).

A forbidden-autonomous model (fable, astra, ...) is never promoted nor bound to a role here; that is a Grill matter.
Time is always injected (``now``/``today``); the registry write is atomic (tmp + ``os.replace``); the audit trail is an
append-only JSONL ledger. Docs are regenerated after a write when ``docs_root`` is given. Grill thresholds are slice 8
and are not decided here: ``promote(..., precheck=...)`` is a neutral hook that may veto a promotion.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

from core.models.grill_policy import ModelChange, requires_grill
from core.models.registry import (
    DEFAULT_REGISTRY_PATH,
    QUALIFIED_STATUSES,
    REPO_ROOT,
    ModelRegistry,
    clear_cache,
    parse_registry,
)

logger = logging.getLogger(__name__)

LEDGER_PATH = REPO_ROOT / ".factory" / "models" / "lifecycle_ledger.jsonl"
ROLLBACK_REASONS = ("gate_failure", "cost", "quota")
ROLE_PREVIOUS_KEY = "role_previous"

# Neutral hook for slice 8: receives (registry_raw, model_entry_raw, role) and returns a veto message or None.
Precheck = Callable[[dict[str, Any], dict[str, Any], Optional[str]], Optional[str]]


class LifecycleError(ValueError):
    """A transition was refused; nothing was written."""


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _entry(raw: dict[str, Any], model_id: str) -> dict[str, Any]:
    for m in raw["models"]:
        if m["id"] == model_id:
            return m
    raise LifecycleError(f"unknown model {model_id!r}")


def _is_forbidden(raw: dict[str, Any], entry: dict[str, Any]) -> bool:
    markers = [m.strip().lower() for m in raw.get("forbidden_autonomous_markers", []) if m.strip()]
    return bool(entry.get("forbidden_autonomous")) or any(mk in entry["id"].lower() for mk in markers)


def _commit(raw: dict[str, Any], path: Path, docs_root: Optional[Path]) -> ModelRegistry:
    try:
        reg = parse_registry(raw)  # fail before writing anything invalid
    except ValueError as exc:
        raise LifecycleError(f"resulting registry is invalid: {exc}") from exc
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    clear_cache()
    if docs_root is not None:
        from core.models.docgen import process

        process(root=docs_root, reg=reg, write=True)
    return reg


def _audit(ledger: Path, now: datetime, actor: str, action: str, **fields: Any) -> dict[str, Any]:
    record = {"ts": now.isoformat(), "actor": actor, "action": action, **fields}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def read_ledger(ledger_path: Optional[Path] = None) -> list[dict[str, Any]]:
    path = ledger_path or LEDGER_PATH
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def promote(
    model_id: str,
    *,
    now: datetime,
    role: Optional[str] = None,
    actor: str = "factory",
    registry_path: Optional[Path] = None,
    ledger_path: Optional[Path] = None,
    docs_root: Optional[Path] = None,
    precheck: Optional[Precheck] = None,
) -> dict[str, Any]:
    """candidate -> active (qualification approved/grandfathered required), optionally binding ``role``."""
    path = registry_path or DEFAULT_REGISTRY_PATH
    ledger = ledger_path or LEDGER_PATH
    raw = _load(path)
    entry = _entry(raw, model_id)
    if _is_forbidden(raw, entry):
        raise LifecycleError(f"{model_id!r} is forbidden for autonomous use; it needs the Grill, never promoted here")
    if entry.get("status") != "candidate":
        raise LifecycleError(f"{model_id!r} is not a candidate (status={entry.get('status')})")
    qual = entry.get("qualification") or {}
    if qual.get("status") not in QUALIFIED_STATUSES:
        raise LifecycleError(f"{model_id!r} has no approved qualification (status={qual.get('status')})")
    if role is not None and role not in raw.get("roles", {}):
        raise LifecycleError(f"unknown role {role!r}")

    # Grill is a mandatory policy gate for promotions.  Keep the caller hook
    # below as an additional, domain-specific veto; it must not be possible to
    # bypass the registry thresholds by supplying a custom precheck.
    decision = requires_grill(
        ModelChange(kind="promote_candidate", model_id=model_id, role=role),
        parse_registry(raw),
    )
    if decision.required:
        reasons = "; ".join(decision.reasons)
        raise LifecycleError(f"promotion requires WAITING_HUMAN (Grill): {reasons}")
    if precheck is not None:
        veto = precheck(raw, entry, role)
        if veto:
            raise LifecycleError(f"promotion vetoed: {veto}")
    entry["status"] = "active"
    previous: Optional[str] = None
    if role is not None:
        previous = raw["roles"][role]
        raw["roles"][role] = model_id
        raw.setdefault(ROLE_PREVIOUS_KEY, {})[role] = previous
    _commit(raw, path, docs_root)
    return _audit(
        ledger, now, actor, "promote",
        model_id=model_id, from_status="candidate", to_status="active",
        role=role, from_model=previous, to_model=model_id if role else None,
        reason="qualification_approved",
    )


def rollback(
    target: str,
    reason: str,
    *,
    now: datetime,
    actor: str = "factory",
    registry_path: Optional[Path] = None,
    ledger_path: Optional[Path] = None,
    docs_root: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Revert ``target`` (a role, or a model id = every role bound to it) to the previous model.

    The regressed model becomes ``deprecated`` unless another role still routes to it. Returns the audit records.
    """
    if reason not in ROLLBACK_REASONS:
        raise LifecycleError(f"reason must be one of {ROLLBACK_REASONS}, got {reason!r}")
    path = registry_path or DEFAULT_REGISTRY_PATH
    ledger = ledger_path or LEDGER_PATH
    raw = _load(path)
    roles: dict[str, str] = raw.get("roles", {})
    history: dict[str, str] = raw.setdefault(ROLE_PREVIOUS_KEY, {})
    affected = [target] if target in roles else [r for r, m in roles.items() if m == target]
    if not affected:
        raise LifecycleError(f"{target!r} is neither a role nor a model bound to a role")
    moves: list[dict[str, str]] = []
    for r in affected:
        prev = history.get(r)
        if not prev:
            raise LifecycleError(f"role {r!r} has no previous model to roll back to")
        moves.append({"role": r, "from_model": roles[r], "to_model": prev})
        roles[r] = prev
        del history[r]
    if not history:
        del raw[ROLE_PREVIOUS_KEY]
    deprecated: list[str] = []
    for bad in dict.fromkeys(m["from_model"] for m in moves):
        if bad in roles.values():
            continue  # still serving another role: stays active
        entry = _entry(raw, bad)
        if entry["status"] == "active":
            entry["status"] = "deprecated"
            deprecated.append(bad)
    _commit(raw, path, docs_root)
    audits = [
        _audit(ledger, now, actor, "rollback", model_id=m["from_model"], role=m["role"],
               from_model=m["from_model"], to_model=m["to_model"], reason=reason)
        for m in moves
    ]
    audits += [
        _audit(ledger, now, actor, "deprecate", model_id=bad, from_status="active", to_status="deprecated", reason=reason)
        for bad in deprecated
    ]
    return audits


def retire_due(
    today: date,
    *,
    now: datetime,
    actor: str = "factory",
    registry_path: Optional[Path] = None,
    ledger_path: Optional[Path] = None,
    docs_root: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """deprecated -> retired when ``retirement_date`` <= ``today``. Idempotent: a second run changes nothing."""
    path = registry_path or DEFAULT_REGISTRY_PATH
    ledger = ledger_path or LEDGER_PATH
    raw = _load(path)
    due: list[dict[str, Any]] = []
    for m in raw["models"]:
        if m.get("status") != "deprecated" or not m.get("retirement_date"):
            continue
        try:
            when = date.fromisoformat(str(m["retirement_date"])[:10])
        except ValueError:
            logger.warning("model %s has an unparseable retirement_date %r", m["id"], m["retirement_date"])
            continue
        if when <= today:
            m["status"] = "retired"
            due.append(m)
    if not due:
        return []
    _commit(raw, path, docs_root)
    return [
        _audit(ledger, now, actor, "retire", model_id=m["id"], from_status="deprecated", to_status="retired",
               reason=f"retirement_date {m['retirement_date']} <= {today.isoformat()}")
        for m in due
    ]
