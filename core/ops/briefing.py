"""Fast, read-only operational briefing shared by every DarkFac harness."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from core.paths import project_root, state_root

logger = logging.getLogger(__name__)
GH_TIMEOUT_SECONDS = 8
HANDOFF_RELATIVE = Path("docs/operacao/HANDOFF.md")
HARNESS_PROVIDERS = {
    "claude": "anthropic",
    "codex": "openai",
    "grok": "xai",
    "antigravity": "google",
}


def _section(name: str, read: Callable[[], list[str]]) -> list[str]:
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 - each section fails independently
        logger.warning("Briefing section %s failed: %s", name, type(exc).__name__)
        return [f"Indisponivel (causa: {type(exc).__name__})."]


def _queue() -> list[str]:
    from core.demands.models import DeliveryStatus
    from core.demands.store import DemandsStore

    ledger = project_root() / ".factory" / "demands" / "demands.json"
    tickets = DemandsStore(path=ledger).list_tickets()
    active = [t for t in tickets if t.status not in {DeliveryStatus.COMPLETED, DeliveryStatus.CANCELLED}]
    if not active:
        return ["Fila vazia."]
    return [f"{t.id} | {getattr(t.status, 'value', t.status)} | {t.title}" for t in active]


def _owner_actions() -> list[str]:
    from core.owner_actions.store import OwnerActionStore

    root = project_root()
    base = root / ".factory" / "owner_actions" / "owner_actions.json"
    dynamic = root / ".factory" / "owner_actions" / "dynamic.json"
    overlay = root / "hub" / "data" / "owner_actions" / "resolutions.json"
    result = OwnerActionStore(
        path=base,
        dynamic_path=dynamic if dynamic.is_file() else None,
        overlay_path=overlay if overlay.is_file() else None,
    ).load()
    open_actions = [a for a in result.actions if getattr(a.status, "value", a.status) != "done"]
    lines = [f"{a.id} | {getattr(a.status, 'value', a.status)} | {a.title}" for a in open_actions]
    lines.extend(f"Aviso: {warning}" for warning in result.warnings)
    return lines or ["Nenhuma acao aberta."]


def _nodes() -> list[str]:
    # get_node_liveness issues bounded GET /health probes only. It never invokes inventory
    # update/save or any convergence operation; worker inventory is read-only here.
    from core.infra.node_liveness import list_worker_liveness

    nodes = list_worker_liveness()
    return [f"{node.node_id} | {node.status}" for node in nodes] or ["Nenhum no de worker configurado."]


def _pull_requests() -> list[str]:
    gh = shutil.which("gh")
    if not gh:
        return ["Fallback: gh ausente; lista de PRs indisponivel."]
    try:
        result = subprocess.run(
            [gh, "pr", "list", "--state", "open", "--limit", "50", "--json", "number,title,url"],
            cwd=project_root(), capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=GH_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return [f"Fallback: gh excedeu timeout de {GH_TIMEOUT_SECONDS}s."]
    except OSError as exc:
        return [f"Fallback: gh falhou ({type(exc).__name__})."]
    if result.returncode:
        detail = (result.stderr or "").strip().splitlines()
        cause = detail[-1][:180] if detail else f"exit_code={result.returncode}"
        return [f"Fallback: gh indisponivel ou sem autenticacao ({cause})."]
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return ["Fallback: gh retornou JSON invalido."]
    if not isinstance(rows, list):
        return ["Fallback: gh retornou formato inesperado."]
    return [f"#{row.get('number')} | {row.get('title', '')} | {row.get('url', '')}" for row in rows if isinstance(row, dict)] or ["Nenhum PR aberto."]


def _quotas() -> list[str]:
    """Read persisted snapshots only; never run provider probes or mutate snapshots."""
    providers = state_root() / "usage" / "providers"
    now = datetime.now(timezone.utc)
    lines: list[str] = []
    for harness, provider in HARNESS_PROVIDERS.items():
        path = providers / f"{provider}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        except (OSError, json.JSONDecodeError):
            data = None
        if not isinstance(data, dict):
            lines.append(f"{harness}: desconhecida (snapshot ausente ou invalido).")
            continue
        checked = data.get("checked_at")
        freshness = "idade desconhecida"
        try:
            stamp = datetime.fromisoformat(str(checked).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age = max(0, int((now - stamp).total_seconds()))
            freshness = f"STALE ({age // 3600}h)" if age > 600 else f"atual ({age // 60}m)"
        except (TypeError, ValueError):
            pass
        windows = data.get("windows")
        available = []
        if isinstance(windows, list):
            for window in windows:
                if isinstance(window, dict):
                    remaining = window.get("remaining_percent")
                    if remaining is None and isinstance(window.get("used_percent"), (int, float)):
                        remaining = round(100 - float(window["used_percent"]), 2)
                    available.append(f"{window.get('label') or window.get('quota_id')}: {remaining}% restante" if remaining is not None else str(window.get("label") or window.get("quota_id")))
        detail = ", ".join(available) if available else "percentual desconhecido"
        failure = data.get("probe_failure")
        cause = ""
        if isinstance(failure, dict):
            category = failure.get("category") or "unknown"
            code = failure.get("code") or "unknown"
            message = failure.get("message") or "causa sem detalhe"
            cause = f"; causa da coleta: {category}/{code} ({message})"
        lines.append(f"{harness}: {detail}; {freshness}{cause}.")
    return lines


def _handoff() -> list[str]:
    path = project_root() / HANDOFF_RELATIVE
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        return [f"Handoff indisponivel ({type(exc).__name__}): {HANDOFF_RELATIVE.as_posix()}"]
    return content.splitlines() if content else ["Handoff vazio."]


def _sentinel() -> list[str]:
    """Status do sentinela autonomo da linha (USR-439)."""
    from core.line.sentinel import load_last_report, maybe_run_sentinel

    try:
        report = load_last_report() if "pytest" in sys.modules else (maybe_run_sentinel() or load_last_report())
    except Exception as exc:  # noqa: BLE001
        return [f"Indisponivel ({type(exc).__name__})."]

    if report is None:
        return ["Nenhum registro previo do sentinela."]

    status_tag = f"[{report.status.value.upper()}]"
    lines = [
        f"Status: {status_tag} {report.reason}",
        f"Rotas: Dev={report.route_development or 'NENHUMA'} (Frota={report.fleet_route_development or 'NENHUMA'}) | Plan={report.route_planning or 'NENHUMA'}",
        f"Fila: {len(report.pending_tickets)} pendente(s) | {len(report.in_flight_tickets)} em voo",
    ]
    if report.quotas:
        q_str = ", ".join(f"{h}: {q}%" for h, q in report.quotas.items() if q is not None)
        lines.append(f"Cotas telemetria: {q_str}")
    return lines


def render_briefing() -> str:
    sections: list[tuple[str, Callable[[], list[str]]]] = [
        ("Sentinela da Linha (USR-439)", _sentinel),
        ("Fila do control store", _queue),
        ("Acoes abertas do owner", _owner_actions),
        ("Nos (somente leitura)", _nodes),
        ("PRs abertos", _pull_requests),
        ("Cotas por harness (snapshots, sem probes)", _quotas),
        ("Handoff de operacao", _handoff),
    ]
    lines = ["DarkFac | Briefing operacional", f"Gerado em {datetime.now(timezone.utc).isoformat()}"]
    for name, reader in sections:
        lines.extend(("", f"## {name}"))
        lines.extend(_section(name, reader))
    return "\n".join(lines) + "\n"


def main() -> int:
    sys.stdout.buffer.write(render_briefing().encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
