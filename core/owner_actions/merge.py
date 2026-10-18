"""Three-way merge of ``owner_actions.json`` by action ``id`` (USR-312).

Used by the git autonomy sync when a branch and ``origin/main`` both touched the backlog (for
example the queue watchdog appended automatic OAs while a registration PR was open).

Rules (per ``id`` in ``actions``):

* added on one side only -> kept;
* changed on one side only -> the change wins;
* changed on both sides to different content -> real conflict (``ok=False``);
* added on both sides with different content (id collision) -> both rows are kept in the output,
  the caller renumbers ours with :func:`core.owner_actions.dedupe.renumber_duplicate_owner_actions`.

Order: ``theirs`` (main) order first, then rows only ``ours`` has.
"""

from __future__ import annotations

import json
from typing import Any

_MISSING = object()


def _parse(raw: str, label: str) -> tuple[dict[str, Any] | None, str]:
    if not raw.strip():
        return {"actions": []}, ""
    try:
        doc = json.loads(raw.lstrip("﻿"))
    except json.JSONDecodeError as exc:
        return None, f"{label}: JSON invalido ({exc})"
    if not isinstance(doc, dict) or not isinstance(doc.get("actions", []), list):
        return None, f"{label}: formato inesperado"
    return doc, ""


def _by_id(doc: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    rows: dict[str, Any] = {}
    order: list[str] = []
    for row in doc.get("actions", []):
        key = str(row.get("id", "")) if isinstance(row, dict) else ""
        if not key or key in rows:
            continue
        rows[key] = row
        order.append(key)
    return rows, order


def _pick(base: Any, ours: Any, theirs: Any) -> tuple[bool, Any]:
    """Generic 3-way choice; returns (ok, value)."""
    if ours == theirs:
        return True, theirs
    if theirs == base:
        return True, ours
    if ours == base:
        return True, theirs
    return False, None


def merge_owner_actions_3way(base_raw: str, ours_raw: str, theirs_raw: str) -> tuple[bool, str, str]:
    """Return ``(ok, merged_text, error)``; ``merged_text`` is empty when ``ok`` is False."""
    base, err = _parse(base_raw, "base")
    if base is None:
        return False, "", err
    ours, err = _parse(ours_raw, "ours")
    if ours is None:
        return False, "", err
    theirs, err = _parse(theirs_raw, "theirs")
    if theirs is None:
        return False, "", err

    b_rows, _ = _by_id(base)
    o_rows, o_order = _by_id(ours)
    t_rows, t_order = _by_id(theirs)

    merged_rows: list[Any] = []
    for key in t_order:
        t = t_rows[key]
        b = b_rows.get(key, _MISSING)
        o = o_rows.get(key, _MISSING)
        if b is _MISSING:
            merged_rows.append(t)
            if o is not _MISSING and o != t:
                merged_rows.append(o)  # id collision: renumbered by the caller
            continue
        ok, value = _pick(b, o, t)
        if not ok:
            return False, "", f"owner_actions.json: {key} alterado nos dois lados com conteudo diferente"
        if value is not _MISSING:
            merged_rows.append(value)
    for key in o_order:
        if key in t_rows:
            continue
        o = o_rows[key]
        b = b_rows.get(key, _MISSING)
        if b is _MISSING:
            merged_rows.append(o)
        elif o != b:
            return False, "", f"owner_actions.json: {key} removido em main e alterado na branch"

    result: dict[str, Any] = {}
    for key in dict.fromkeys([*theirs, *ours, *base]):
        if key == "actions":
            result[key] = merged_rows
            continue
        ok, value = _pick(base.get(key, _MISSING), ours.get(key, _MISSING), theirs.get(key, _MISSING))
        if not ok:
            return False, "", f"owner_actions.json: campo '{key}' alterado nos dois lados"
        if value is not _MISSING:
            result[key] = value
    if "actions" not in result:
        result["actions"] = merged_rows
    return True, json.dumps(result, indent=2, ensure_ascii=False) + "\n", ""


__all__ = ["merge_owner_actions_3way"]
