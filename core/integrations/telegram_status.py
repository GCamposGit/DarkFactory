"""Real factory status for the Telegram ``/status`` command (USR-197).

The old reply was a fixed sentence plus ``tickets[-1]`` of an unordered list. This module builds a short,
true summary from data providers injected by the Hub. Every block is computed in isolation: a failing
provider only turns its own block into ``indisponivel`` and never breaks the command.

The text is Telegram HTML (``parse_mode=HTML``): every dynamic value is escaped, and the message is cut
on a line boundary (each ``<b>`` pair lives on a single line), so tags always stay balanced.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("darkfac.integrations.telegram_status")

UNAVAILABLE = "indisponivel"
DEFAULT_HUB_URL = "https://darkhub.ggcampos.com"
MAX_MESSAGE_CHARS = 3600  # Telegram caps a message at 4096; keep headroom for the caller prefix.
MAX_TITLE_CHARS = 70
MAX_OWNER_ACTIONS_SHOWN = 3
MAX_RUNS_SHOWN = 5

# Real ledger tickets are USR-<number>; USR-AUTO, AUTO, USR-DRAFT and similar are synthetic/test rows.
_REAL_TICKET_ID = re.compile(r"^USR-\d+$")
_HORIZON_RANK = {"now": 0, "next": 1, "later": 2, "exploratory": 3, "unscheduled": 4}
_IN_PROGRESS = frozenset({"implementing", "validating"})


def _esc(value: object, limit: int = MAX_TITLE_CHARS) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 3].rstrip() + "..."
    return html.escape(text, quote=False)


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value) or "").lower()


def _hub_link(hub_url: str | None) -> str:
    base = (hub_url or os.getenv("DARKHUB_BASE_URL") or DEFAULT_HUB_URL).rstrip("/")
    return f"{base}/#owner-actions"


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "idade desconhecida"
    total = int(max(0, seconds))
    if total < 90:
        return f"ha {total} s"
    minutes = total // 60
    if minutes < 90:
        return f"ha {minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"ha {hours} h"
    return f"ha {hours // 24} d"


# ----------------------------------------------------------------------------- blocks


def _owner_actions_lines(provider: Callable[[], list[dict[str, Any]]], link: str) -> list[str]:
    items = list(provider())
    total = len(items)
    critical = sum(1 for i in items if _enum_value(i.get("priority")) == "critical")
    high = sum(1 for i in items if _enum_value(i.get("priority")) == "high")
    if total == 0:
        return ["<b>Acoes do owner</b>: nenhuma acao aberta"]
    noun = "acao aberta" if total == 1 else "acoes abertas"
    lines = [f"<b>Acoes do owner</b>: {total} {noun} ({critical} critical, {high} high)"]
    for item in items[:MAX_OWNER_ACTIONS_SHOWN]:
        prio = _enum_value(item.get("priority"))
        lines.append(f"- {_esc(item.get('id'), 12)} [{_esc(prio, 10)}] {_esc(item.get('title'))}")
    lines.append(html.escape(link, quote=False))
    return lines


def _ticket_sort_key(ticket: Any) -> tuple[int, str, str]:
    horizon = _enum_value(getattr(ticket, "horizon", ""))
    created = getattr(ticket, "created_at", None)
    created_text = created.isoformat() if isinstance(created, datetime) else str(created or "")
    tid = str(getattr(ticket, "id", ""))
    number = re.sub(r"\D", "", tid).zfill(8)
    return (_HORIZON_RANK.get(horizon, 4), created_text, number)


def _tickets_lines(provider: Callable[[], list[Any]], waiting_human_ids: set[str] | None) -> list[str]:
    real = [
        t
        for t in provider()
        if _enum_value(getattr(t, "project_id", "darkfac")) == "darkfac"
        and _REAL_TICKET_ID.match(str(getattr(t, "id", "")))
    ]
    by_state = {"planned": 0, "in_progress": 0, "completed": 0}
    planned: list[Any] = []
    for t in real:
        state = _enum_value(getattr(t, "status", ""))
        if state == "planned":
            by_state["planned"] += 1
            planned.append(t)
        elif state in _IN_PROGRESS:
            by_state["in_progress"] += 1
        elif state == "completed":
            by_state["completed"] += 1
    waiting = len(waiting_human_ids) if waiting_human_ids is not None else UNAVAILABLE
    lines = [
        "<b>Tickets darkfac</b>: "
        f"planned {by_state['planned']} | em andamento {by_state['in_progress']} | "
        f"aguardando humano {waiting} | completed {by_state['completed']}"
    ]
    if planned:
        nxt = sorted(planned, key=_ticket_sort_key)[0]
        lines.append(f"Proximo: {_esc(getattr(nxt, 'id', ''), 12)} - {_esc(getattr(nxt, 'title', ''))}")
    else:
        lines.append("Proximo: nenhum ticket planned")
    return lines


def _runs_lines(runs: list[dict[str, Any]]) -> list[str]:
    active = [r for r in runs if str(r.get("state", "")).lower() in {"running", "queued", "attention"}]
    if not active:
        return ["<b>Linha</b>: nenhum run ativo"]
    noun = "run ativo" if len(active) == 1 else "runs ativos"
    lines = [f"<b>Linha</b>: {len(active)} {noun}"]
    for run in active[:MAX_RUNS_SHOWN]:
        stage = _esc(run.get("stage") or "-", 30)
        state = "aguardando humano" if run.get("waiting_human") else _esc(run.get("state"), 20)
        lines.append(f"- {_esc(run.get('ticket_id'), 14)} {stage} ({state})")
    if len(active) > MAX_RUNS_SHOWN:
        lines.append(f"- ... e mais {len(active) - MAX_RUNS_SHOWN}")
    return lines


def _convergence_lines(provider: Callable[[], dict[str, Any]]) -> list[str]:
    snap = provider()
    nodes: dict[str, Any] = dict(snap.get("nodes") or {})
    if not nodes:
        return [f"<b>Nos</b>: {UNAVAILABLE} (sem ciclo de deploy registrado)"]
    parts: list[str] = []
    for name in ("Notebook", "Desktop", "VPS"):
        if name not in nodes:
            continue
        cycles = int(nodes[name] or 0)
        parts.append(f"{_esc(name, 12)} " + ("sem divergencia" if cycles == 0 else f"divergente ({cycles} ciclos)"))
    for name, cycles in nodes.items():
        if name not in ("Notebook", "Desktop", "VPS"):
            n = int(cycles or 0)
            parts.append(f"{_esc(name, 12)} " + ("sem divergencia" if n == 0 else f"divergente ({n} ciclos)"))
    lines = [f"<b>Nos</b> (ultimo ciclo, {_format_age(snap.get('age_seconds'))}): " + " | ".join(parts)]
    pending = snap.get("pending")
    if isinstance(pending, dict) and pending.get("node"):
        lines.append(f"Convergencia pendente: {_esc(pending.get('node'), 12)} - {_esc(pending.get('reason'), 80)}")
    return lines


# ----------------------------------------------------------------------------- public API


def build_status_message(
    *,
    owner_actions: Callable[[], list[dict[str, Any]]],
    tickets: Callable[[], list[Any]],
    runs: Callable[[], list[dict[str, Any]]],
    convergence: Callable[[], dict[str, Any]],
    now: datetime | None = None,
    hub_url: str | None = None,
) -> str:
    """Compose the ``/status`` text. Each provider fails alone into ``indisponivel``."""
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    link = _hub_link(hub_url)
    blocks: list[list[str]] = []

    def _isolated(name: str, build: Callable[[], list[str]], title: str) -> None:
        try:
            blocks.append(build())
        except Exception as exc:  # one block must never take the whole command down
            logger.warning("Telegram /status block %s unavailable: %s", name, type(exc).__name__)
            blocks.append([f"<b>{title}</b>: {UNAVAILABLE}"])

    _isolated("owner_actions", lambda: _owner_actions_lines(owner_actions, link), "Acoes do owner")

    run_rows: list[dict[str, Any]] | None
    try:
        run_rows = list(runs())
    except Exception as exc:
        logger.warning("Telegram /status block runs unavailable: %s", type(exc).__name__)
        run_rows = None
    waiting_ids = (
        {str(r.get("ticket_id")) for r in run_rows if r.get("waiting_human")} if run_rows is not None else None
    )
    _isolated("tickets", lambda: _tickets_lines(tickets, waiting_ids), "Tickets darkfac")
    if run_rows is None:
        blocks.append([f"<b>Linha</b>: {UNAVAILABLE}"])
    else:
        _isolated("runs", lambda: _runs_lines(run_rows), "Linha")
    _isolated("convergence", lambda: _convergence_lines(convergence), "Nos")

    lines: list[str] = []
    for block in blocks:
        lines.extend(block)
    footer = f"Gerado em {stamp}"
    budget = MAX_MESSAGE_CHARS - len(footer) - 1
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            kept.append("... (resumo truncado)")
            break
        kept.append(line)
        used += len(line) + 1
    kept.append(footer)
    return "\n".join(kept)


def read_convergence_snapshot(
    state_dir: Path,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Cheap, read-only convergence snapshot from the files the deploy cycle already writes.

    ``node_sync_divergence.json`` holds consecutive divergent cycles per node (0 = no divergence at the
    last cycle); ``node_sync_pending.json`` an unfinished convergence. No network and no git: running
    ``node_sync.verify`` would probe the Desktop and VPS over HTTP, which is too slow for a chat reply.
    Raises ``FileNotFoundError`` when no deploy cycle ever recorded a snapshot.
    """
    divergence_file = Path(state_dir) / "node_sync_divergence.json"
    raw = json.loads(divergence_file.read_text(encoding="utf-8"))  # FileNotFoundError propagates
    if not isinstance(raw, dict):
        raise ValueError("node_sync_divergence.json is not an object")
    nodes = {str(k): int(v or 0) for k, v in raw.items()}
    reference = now or datetime.now(timezone.utc)
    modified = datetime.fromtimestamp(divergence_file.stat().st_mtime, tz=timezone.utc)
    age = max(0.0, (reference - modified).total_seconds())
    pending: dict[str, Any] | None = None
    try:
        loaded = json.loads((Path(state_dir) / "node_sync_pending.json").read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            pending = loaded
    except (OSError, ValueError):
        pending = None
    return {"nodes": nodes, "age_seconds": age, "pending": pending}
