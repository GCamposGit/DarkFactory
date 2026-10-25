"""Resumo diario ao owner pelo bot do Telegram (USR-376).

Manha: plano do dia (tickets planned, em voo) e decisoes/acoes do owner abertas.
Noite: entregas das ultimas 24 h com links diretos para PR, learning pack e manual.

``build_morning`` e ``build_evening`` sao puros: as fontes entram por ``DigestSources``.
``send`` usa o mesmo envio do bot ja usado pela linha (``default_telegram_sender``).
``maybe_send_due`` e o gancho do despachante: no maximo um envio por tipo por dia.

Rotas reais do Hub usadas nos links (frontend por hash, sem pagina por ticket):
``/#owner-actions`` (fila do owner), ``/#grill=<ticket>`` (grill), ``/#demands`` (demandas),
``/api/learning-packs/<pack_id>/html`` (learning pack). O PR aponta para o GitHub.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from core.paths import project_root, state_root

logger = logging.getLogger(__name__)

DEFAULT_HUB_URL = "https://darkhub.ggcampos.com"
TELEGRAM_LIMIT = 4096
MAX_PLANNED = 8
MORNING_HOUR = 8
EVENING_HOUR = 21
MORNING_WINDOW_END = 12  # depois disso o plano do dia ja nao serve
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_TICKET_RE = re.compile(r"\[([A-Za-z]+-\d+)\]")
_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
_BLOCK_RE = re.compile(r"^bloco-([a-z0-9]+)$")
_EXCLUDED_TAGS = frozenset({"congelado-v0", "not-authorized-for-execution", "planning-only", "waiting_human"})


def _tz() -> Any:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("America/Sao_Paulo")
    except Exception:  # pragma: no cover - sem tzdata no Windows
        return timezone(timedelta(hours=-3))


def hub_url() -> str:
    return (os.getenv("DARKHUB_BASE_URL") or DEFAULT_HUB_URL).rstrip("/")


@dataclass
class DigestSources:
    planned: list[dict[str, Any]] = field(default_factory=list)  # dicts de demanda (id, title, tags, dependencies, status)
    completed_ids: set[str] = field(default_factory=set)
    in_flight: dict[str, str] = field(default_factory=dict)  # host -> ticket
    owner_actions: list[dict[str, Any]] = field(default_factory=list)  # abertas: id, title, priority, kind, blocks
    deliveries: list[dict[str, Any]] = field(default_factory=list)  # ticket, title, pr, pr_url, pack_url, manual_pages


# --- formatacao -----------------------------------------------------------------------------------


def _cut(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3].rstrip() + "..."


def _fit(lines: list[str], limit: int = TELEGRAM_LIMIT) -> str:
    """Junta linhas sem passar do limite; o excedente vira um aviso curto."""
    out: list[str] = []
    size = 0
    for i, line in enumerate(lines):
        extra = len(line) + 1
        if size + extra > limit - 40:
            out.append(f"... (+{len(lines) - i} linhas omitidas)")
            break
        out.append(line)
        size += extra
    return "\n".join(out)[:limit]


def _block(tags: list[str]) -> str:
    for t in tags:
        m = _BLOCK_RE.match(t)
        if m:
            return m.group(1)
    return "~"


def _day_label(now: datetime) -> str:
    return now.astimezone(_tz()).strftime("%d/%m")


def build_morning(now: datetime, sources: DigestSources) -> str:
    base = hub_url()
    eligible = [
        d for d in sources.planned
        if d.get("status", "planned") == "planned"
        and not (set(d.get("tags") or []) & _EXCLUDED_TAGS)
        and all(x in sources.completed_ids for x in (d.get("dependencies") or []))
    ]
    eligible.sort(key=lambda d: (_block(list(d.get("tags") or [])), str(d.get("created_at", "")), str(d.get("id", ""))))
    lines = [f"Bom dia! Plano de {_day_label(now)}", ""]
    lines.append(f"Na fila ({len(eligible)} prontos, mostrando {min(len(eligible), MAX_PLANNED)}):")
    for d in eligible[:MAX_PLANNED]:
        blk = _block(list(d.get("tags") or []))
        tag = f"[{blk.upper()}] " if blk != "~" else ""
        lines.append(f"- {d.get('id')} {tag}{_cut(d.get('title', ''), 80)}")
    if not eligible:
        lines.append("- nada pronto para executar")
    lines.append("")
    if sources.in_flight:
        lines.append("Em voo: " + ", ".join(f"{t} ({h})" for h, t in sorted(sources.in_flight.items())))
    else:
        lines.append("Em voo: nada")
    lines.append("")
    acts = sorted(
        sources.owner_actions,
        key=lambda a: (PRIORITY_RANK.get(str(a.get("priority", "medium")), 9), str(a.get("id", ""))),
    )
    lines.append(f"Decisoes e acoes suas abertas ({len(acts)}):")
    for a in acts[:10]:
        kind = "decisao" if a.get("kind") == "decision" else "acao"
        blocks = ", ".join(a.get("blocks") or [])
        suffix = f" - bloqueia {blocks}" if blocks else ""
        lines.append(f"- {a.get('priority', 'medium').upper()} {a.get('id')} ({kind}) {_cut(a.get('title', ''), 70)}{suffix}")
    if len(acts) > 10:
        lines.append(f"... e mais {len(acts) - 10}")
    if not acts:
        lines.append("- nenhuma")
    lines += ["", f"Fila do owner: {base}/#owner-actions", f"Demandas: {base}/#demands"]
    return _fit(lines)


def build_evening(now: datetime, sources: DigestSources) -> str:
    base = hub_url()
    lines = [f"Boa noite! Entregas de {_day_label(now)} (ultimas 24 h)", ""]
    if not sources.deliveries:
        lines.append("Nenhuma entrega nas ultimas 24 h.")
    for d in sources.deliveries:
        lines.append(f"{d.get('ticket')} - {_cut(d.get('title', ''), 80)}")
        if d.get("pr_url"):
            lines.append(f"  PR: {d['pr_url']}")
        if d.get("pack_url"):
            lines.append(f"  Learning pack: {d['pack_url']}")
        else:
            lines.append("  Learning pack: ainda nao gerado")
        for page in (d.get("manual_pages") or [])[:3]:
            lines.append(f"  Manual: {page}")
    open_acts = len(sources.owner_actions)
    lines += ["", f"Pendente com voce: {open_acts} item(ns) - {base}/#owner-actions"]
    return _fit(lines)


# --- fontes reais ---------------------------------------------------------------------------------


def _load_demands() -> list[dict[str, Any]]:
    path = project_root() / ".factory" / "demands" / "demands.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else list(data.get("demands", []))


def _load_in_flight() -> dict[str, str]:
    try:
        from core.line.fleet_dispatcher import STATE_PATH

        state = json.loads(Path(STATE_PATH).read_text(encoding="utf-8"))
        return dict(state.get("in_flight") or {})
    except Exception:  # noqa: BLE001
        return {}


def _load_owner_actions() -> list[dict[str, Any]]:
    from core.owner_actions.store import OwnerActionStore

    out = []
    for a in OwnerActionStore().list_actions(include_done=False):
        out.append({
            "id": a.id, "title": a.title, "priority": a.priority.value,
            "kind": a.kind.value, "blocks": list(a.blocks),
        })
    return out


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=project_root(), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60, check=False,
    ).stdout


def _load_packs() -> list[dict[str, Any]]:
    try:
        path = state_root() / "learning_packs" / "packs_index.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def load_deliveries(now: datetime, packs: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
    since = (now - timedelta(hours=24)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    remote = _git("remote", "get-url", "origin").strip().removesuffix(".git")
    sep = "\x1f"
    raw = _git("log", "origin/main", f"--since={since}", f"--format=%H{sep}%s", "--name-only")
    packs = _load_packs() if packs is None else packs
    deliveries: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for line in raw.splitlines():
        if sep in line:
            sha, subject = line.split(sep, 1)
            tm = _TICKET_RE.search(subject)
            current = None
            if tm:
                pm = _PR_RE.search(subject)
                title = _TICKET_RE.sub("", _PR_RE.sub("", subject)).strip()
                current = {
                    "ticket": tm.group(1), "title": title, "sha": sha[:8], "manual_pages": [],
                    "pr": pm.group(1) if pm else None,
                    "pr_url": f"{remote}/pull/{pm.group(1)}" if pm and remote else None,
                    "pack_url": pack_url_for(tm.group(1), packs),
                }
                deliveries.append(current)
        elif current is not None and line.startswith("docs/manual/") and line.endswith(".md"):
            current["manual_pages"].append(line)
    return deliveries


def pack_url_for(ticket: str, packs: list[dict[str, Any]]) -> Optional[str]:
    for p in packs:
        hay = " ".join(str(p.get(k, "")) for k in ("pack_id", "session_id", "title"))
        if re.search(rf"\b{re.escape(ticket)}\b", hay):
            return f"{hub_url()}/api/learning-packs/{p['pack_id']}/html"
    return None


def default_sources(now: datetime) -> DigestSources:
    demands = _load_demands()
    try:
        actions = _load_owner_actions()
    except Exception:  # noqa: BLE001
        logger.exception("falha ao ler fila do owner")
        actions = []
    try:
        deliveries = load_deliveries(now)
    except Exception:  # noqa: BLE001
        logger.exception("falha ao ler entregas")
        deliveries = []
    return DigestSources(
        planned=[d for d in demands if d.get("status") == "planned"],
        completed_ids={str(d.get("id")) for d in demands if d.get("status") == "completed"},
        in_flight=_load_in_flight(),
        owner_actions=actions,
        deliveries=deliveries,
    )


# --- envio e agendamento --------------------------------------------------------------------------


def build(kind: str, now: datetime, sources: DigestSources) -> str:
    if kind == "morning":
        return build_morning(now, sources)
    if kind == "evening":
        return build_evening(now, sources)
    raise ValueError(f"tipo desconhecido: {kind}")


def send(kind: str, *, now: Optional[datetime] = None, sources: Optional[DigestSources] = None,
         sender: Optional[Callable[[str, list[list[dict[str, str]]]], bool]] = None) -> bool:
    now = now or datetime.now(timezone.utc)
    text = build(kind, now, sources or default_sources(now))
    if sender is None:
        from core.line.stage_grill import default_telegram_sender

        sender = default_telegram_sender()
    if sender is None:
        logger.warning("Telegram nao configurado; resumo %s nao enviado", kind)
        return False
    return bool(sender(html.escape(text, quote=False), []))


def _state_path() -> Path:
    return state_root() / "daily_digest_state.json"


def _read_state(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def due_kind(now: datetime, state: dict[str, str]) -> Optional[str]:
    """Qual resumo esta devido agora (hora de Sao Paulo), ou None se ja enviado hoje."""
    local = now.astimezone(_tz())
    today = local.strftime("%Y-%m-%d")
    if EVENING_HOUR <= local.hour and state.get("evening") != today:
        return "evening"
    if MORNING_HOUR <= local.hour < MORNING_WINDOW_END and state.get("morning") != today:
        return "morning"
    return None


def maybe_send_due(
    now: Optional[datetime] = None,
    *,
    send_fn: Optional[Callable[[str], bool]] = None,
    state_path: Optional[Path] = None,
) -> Optional[str]:
    """Gancho do despachante: envia o resumo devido, no maximo uma vez por tipo por dia."""
    now = now or datetime.now(timezone.utc)
    path = state_path or _state_path()
    state = _read_state(path)
    kind = due_kind(now, state)
    if kind is None:
        return None
    ok = (send_fn or (lambda k: send(k, now=now)))(kind)
    if not ok:
        return None
    state[kind] = now.astimezone(_tz()).strftime("%Y-%m-%d")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.exception("falha ao gravar marcador do resumo diario")
    return kind


def main(argv: Optional[list[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Resumo diario ao owner (USR-376)")
    ap.add_argument("kind", choices=["morning", "evening"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    now = datetime.now(timezone.utc)
    if args.dry_run:
        print(build(args.kind, now, default_sources(now)))
        return 0
    return 0 if send(args.kind, now=now) else 1


if __name__ == "__main__":
    sys.exit(main())
