"""Harness-agnostic operational briefing from the current shared providers.

The picture is the shared coordinator registry plus the control store, the
synced usage providers, the live line and GitHub. It does not read
``docs/operacao/HANDOFF.md``, ``docs/FASE_ATUAL.md`` or a private quota file
as if that file were the operation. Each section carries the time it was
observed and a freshness label so a stale provider reading stays visible.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from core.line.coordination import (
    AssumptionRefused,
    assume,
    consult,
    open_state,
)
from core.line.session_identity import SessionIdentity, build_session_identity
from core.line.shared_state import SharedState
from core.usage.models import is_stale, snapshot_age_seconds


logger = logging.getLogger("darkfac.line.briefing")

GH_TIMEOUT_SECONDS = 8
RUN_STALE_AFTER_SECONDS = 900
SCHEMA_VERSION = 1
Loader = Callable[[datetime], "SectionReading"]


@dataclass
class SectionReading:
    source: str
    observed_at: str
    source_time: str | None
    age_seconds: int | None
    freshness: str
    cause: str | None
    items: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def unavailable_section(source: str, now: datetime, cause: str) -> SectionReading:
    return SectionReading(
        source=source,
        observed_at=_iso(now),
        source_time=None,
        age_seconds=None,
        freshness="unavailable",
        cause=cause,
        items=[],
    )


def collect_briefing(
    *,
    identity: SessionIdentity,
    now: datetime,
    state: SharedState | None = None,
    state_path: str | None = None,
    assume_coordination: bool = False,
    owner_decision: bool = False,
    queue_loader: Loader | None = None,
    quota_loader: Loader | None = None,
    runs_loader: Loader | None = None,
    pull_request_loader: Loader | None = None,
) -> dict[str, Any]:
    """Consult, or assume when ``owner_decision`` is set, then attach operational sections."""

    moment = _aware(now)
    registry = state if state is not None else open_state(state_path)
    if assume_coordination:
        coordination = assume(registry, identity, owner_decision=owner_decision, now=moment)
    else:
        coordination = consult(registry, identity, now=moment)
    sections = {
        "queue": _safe_section("control_store", moment, queue_loader or load_queue_section),
        "quotas": _safe_section("synced_usage_provider", moment, quota_loader or load_quota_section),
        "live_runs": _safe_section("control_store_line_live", moment, runs_loader or load_live_runs_section),
        "pull_requests": _safe_section("github", moment, pull_request_loader or load_pull_request_section),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": coordination["observed_at"],
        "assumed": bool(coordination.get("assumed")),
        "caller": coordination["caller"],
        "coordinator": coordination["coordinator"],
        "expired_coordinator": coordination["expired_coordinator"],
        "absence_reason": coordination["absence_reason"],
        "sessions": coordination["sessions"],
        "parallel_sessions": coordination["parallel_sessions"],
        **{name: section.to_dict() for name, section in sections.items()},
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "Briefing da linha",
        f"observado_em={report['observed_at']}",
        "",
        "## Coordenador",
        *_coordinator_lines(report),
        "",
        "## Sessoes paralelas",
        *_session_lines(report.get("parallel_sessions") or []),
        "",
        "## Fila",
        *_section_lines(report["queue"], _queue_item),
        "",
        "## Cotas",
        *_section_lines(report["quotas"], _quota_item),
        "",
        "## Runs vivos",
        *_section_lines(report["live_runs"], _run_item),
        "",
        "## PRs",
        *_section_lines(report["pull_requests"], _pull_request_item),
        "",
    ]
    return "\n".join(lines)


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2) + "\n"


def load_queue_section(now: datetime) -> SectionReading:
    """Open tickets from the control store. A missing store is unavailable, not the local ledger."""

    import os

    from core.demands.queue_store import is_terminal_ticket, list_queue_tickets
    from core.paths import state_root

    observed = _iso(now)
    if not os.environ.get("DARKFAC_HF02_DATABASE_URL") and not (state_root() / "control.db").exists():
        return unavailable_section("control_store", now, "control_store_absent")
    try:
        tickets = [ticket for ticket in list_queue_tickets() if not is_terminal_ticket(ticket)]
    except Exception as exc:  # noqa: BLE001 - the section fails closed on its own
        logger.warning("queue provider failed: %s", type(exc).__name__)
        return unavailable_section("control_store", now, type(exc).__name__)
    items = [_ticket_item(ticket) for ticket in tickets]
    newest = _newest_stamp(item.get("updated_at") for item in items)
    return SectionReading(
        source="control_store",
        observed_at=observed,
        source_time=newest,
        age_seconds=snapshot_age_seconds(newest, now) if newest else 0,
        freshness="current",
        cause=None,
        items=items,
    )


def load_quota_section(now: datetime) -> SectionReading:
    """Synced usage-provider readings with their own checked_at. A stale sample stays stale."""

    from core.line.sentinel import HARNESS_PROVIDERS
    from core.paths import state_root
    from core.usage.adapters import DEFAULT_PROVIDER_SPECS, SyncedSnapshotAdapter

    snapshot_dir = state_root() / "usage" / "providers"
    specs = {spec.provider_id: spec for spec in DEFAULT_PROVIDER_SPECS}
    items: list[dict[str, Any]] = []
    for harness, provider_id in HARNESS_PROVIDERS.items():
        spec = specs.get(provider_id)
        if spec is None:
            items.append(_quota_gap(harness, provider_id, "provider_unregistered"))
            continue
        try:
            usage = SyncedSnapshotAdapter(spec, snapshot_dir).inspect(now=now).with_freshness(now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("quota provider %s failed: %s", provider_id, type(exc).__name__)
            items.append(_quota_gap(harness, provider_id, type(exc).__name__))
            continue
        items.append(_quota_from_usage(harness, usage))
    return _quota_section(items, now)


def load_live_runs_section(now: datetime) -> SectionReading:
    """Runs projected from the control store. Local ``live_runs`` files are not the source."""

    from core.paths import state_root
    from core.workflow.line_live import read_line_live

    snapshot = read_line_live(state_root() / "control.db", now=now)
    if snapshot.source.status != "ok":
        cause = snapshot.source.status
        if snapshot.warnings:
            cause = f"{cause}:{snapshot.warnings[0]}"
        return unavailable_section("control_store_line_live", now, cause)
    items = []
    for run in snapshot.runs:
        if run.state not in {"running", "attention", "queued"}:
            continue
        age = None if run.age_seconds is None else int(run.age_seconds)
        freshness = "stale" if age is None or age > RUN_STALE_AFTER_SECONDS else "current"
        items.append(
            {
                "run_id": run.run_id,
                "ticket_id": run.ticket_id,
                "title": run.title,
                "state": run.state,
                "run_status": run.run_status,
                "current_stage": run.current_stage,
                "host_id": run.host_id,
                "updated_at": run.updated_at,
                "age_seconds": age,
                "freshness": freshness,
            }
        )
    ages = [item["age_seconds"] for item in items if item["age_seconds"] is not None]
    freshness = "stale" if any(item["freshness"] == "stale" for item in items) else "current"
    return SectionReading(
        source="control_store_line_live",
        observed_at=_iso(now),
        source_time=snapshot.generated_at,
        age_seconds=max(ages) if ages else 0,
        freshness=freshness,
        cause=None,
        items=items,
    )


def load_pull_request_section(now: datetime) -> SectionReading:
    """Open pull requests from GitHub. There is no local PR snapshot to fall back to."""

    gh = shutil.which("gh")
    if not gh:
        return unavailable_section("github", now, "gh_absent")
    try:
        result = subprocess.run(
            _gh_command(gh),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return unavailable_section("github", now, "gh_timeout")
    except OSError as exc:
        return unavailable_section("github", now, type(exc).__name__)
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        cause = detail[-1][:180] if detail else f"exit_code={result.returncode}"
        return unavailable_section("github", now, cause)
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return unavailable_section("github", now, "gh_invalid_json")
    if not isinstance(rows, list):
        return unavailable_section("github", now, "gh_unexpected_payload")
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        updated = row.get("updatedAt")
        stamp = str(updated) if updated else None
        age = snapshot_age_seconds(stamp, now) if stamp else None
        items.append(
            {
                "number": row.get("number"),
                "title": row.get("title") or "",
                "url": row.get("url") or "",
                "updated_at": stamp,
                "age_seconds": age,
                "freshness": "stale" if is_stale(age) else "current",
            }
        )
    ages = [item["age_seconds"] for item in items if isinstance(item["age_seconds"], int)]
    return SectionReading(
        source="github",
        observed_at=_iso(now),
        source_time=_newest_stamp(item.get("updated_at") for item in items),
        age_seconds=max(ages) if ages else 0,
        freshness="current",
        cause=None,
        items=items,
    )


def main(argv: list[str] | None = None) -> int:
    """Consult by default. ``assumir --decisao-owner`` swaps the lease and prints the result."""

    args = _parser().parse_args(argv)
    now = datetime.now(timezone.utc)
    identity = build_session_identity()
    assuming = args.acao in {"assumir", "assume"}
    try:
        report = collect_briefing(
            identity=identity,
            now=now,
            state_path=args.state,
            assume_coordination=assuming,
            owner_decision=bool(args.decisao_owner),
        )
    except AssumptionRefused as exc:
        return _refuse(exc.cause, json_output=bool(args.json_output), now=now)
    payload = render_json(report) if args.json_output else render_text(report)
    sys.stdout.write(payload)
    return 0


def _parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        prog="line_briefing.py",
        description=(
            "Consulta a operacao ou assume a coordenacao. "
            "O mesmo comando serve para qualquer harness. "
            "A assuncao so ocorre com --decisao-owner e nao pede confirmacao."
        ),
    )
    parser.add_argument(
        "acao",
        nargs="?",
        default="consultar",
        choices=("consultar", "consult", "assumir", "assume"),
        help="consultar (padrao) ou assumir a coordenacao",
    )
    parser.add_argument(
        "--decisao-owner",
        action="store_true",
        help="Marca a chamada como decisao explicita do owner e troca o coordenador agora.",
    )
    parser.add_argument("--json", dest="json_output", action="store_true", help="Saida estruturada.")
    parser.add_argument(
        "--state",
        default=None,
        help="Caminho do registro compartilhado. Sem isto vale DARKFAC_SHARED_STATE_PATH ou o state root.",
    )
    return parser


def _gh_command(gh: str) -> list[str]:
    args = ["pr", "list", "--state", "open", "--limit", "50", "--json", "number,title,url,updatedAt"]
    if os.name == "nt" and gh.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/d", "/c", gh, *args]
    return [gh, *args]


def _refuse(cause: str, *, json_output: bool, now: datetime) -> int:
    message = "Assuncao recusada: a chamada nao esta marcada como decisao do owner."
    if json_output:
        body = {"ok": False, "cause": cause, "observed_at": _iso(now), "message": message}
        sys.stdout.write(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stderr.write(message + "\n")
    return 2


def _safe_section(source: str, now: datetime, loader: Loader) -> SectionReading:
    try:
        return loader(now)
    except Exception as exc:  # noqa: BLE001 - one section must not hide the others
        logger.warning("briefing section %s failed: %s", source, type(exc).__name__)
        return unavailable_section(source, now, type(exc).__name__)


def _ticket_item(ticket: Any) -> dict[str, Any]:
    status = getattr(ticket.status, "value", ticket.status)
    updated = getattr(ticket, "updated_at", None)
    stamp = updated.isoformat().replace("+00:00", "Z") if isinstance(updated, datetime) else None
    return {
        "id": ticket.id,
        "status": str(status),
        "title": ticket.title,
        "updated_at": stamp,
    }


def _quota_gap(harness: str, provider_id: str, cause: str) -> dict[str, Any]:
    return {
        "harness": harness,
        "provider_id": provider_id,
        "remaining_percent": None,
        "checked_at": None,
        "age_seconds": None,
        "freshness": "unavailable",
        "confirmed": False,
        "cause": cause,
    }


def _quota_from_usage(harness: str, usage: Any) -> dict[str, Any]:
    status = getattr(usage.status, "value", usage.status)
    if str(status) == "unknown" and not usage.windows:
        return _quota_gap(harness, usage.provider_id, "no_synced_reading")
    remaining_values = [
        window.remaining_percent
        for window in usage.windows
        if window.remaining_percent is not None
    ]
    remaining = min(remaining_values) if remaining_values else None
    age = usage.age_seconds if usage.age_seconds is not None else snapshot_age_seconds(usage.checked_at)
    if bool(usage.stale) or is_stale(age):
        freshness = "stale"
        cause = None
    else:
        freshness = "current"
        cause = None
    return {
        "harness": harness,
        "provider_id": usage.provider_id,
        "remaining_percent": remaining,
        "checked_at": usage.checked_at,
        "age_seconds": age,
        "freshness": freshness,
        "confirmed": freshness == "current" and remaining is not None,
        "cause": cause,
    }


def _quota_section(items: list[dict[str, Any]], now: datetime) -> SectionReading:
    if items and all(item["freshness"] == "unavailable" for item in items):
        freshness = "unavailable"
        cause = "no_synced_reading"
    elif any(item["freshness"] == "stale" for item in items):
        freshness = "stale"
        cause = None
    else:
        freshness = "current"
        cause = None
    ages = [item["age_seconds"] for item in items if isinstance(item["age_seconds"], int)]
    return SectionReading(
        source="synced_usage_provider",
        observed_at=_iso(now),
        source_time=_oldest_stamp(item.get("checked_at") for item in items),
        age_seconds=max(ages) if ages else None,
        freshness=freshness,
        cause=cause,
        items=items,
    )


def _coordinator_lines(report: dict[str, Any]) -> list[str]:
    coordinator = report.get("coordinator")
    if coordinator:
        return [_lease_line("vigente", coordinator)]
    reason = report.get("absence_reason") or "no_coordinator"
    lines = [f"ausente causa={reason} frescor=indisponivel"]
    expired = report.get("expired_coordinator")
    if expired:
        lines.append(_lease_line("expirado", expired))
    return lines


def _lease_line(label: str, lease: dict[str, Any]) -> str:
    return (
        f"{label} sessao={lease['session_id']} harness={lease['harness']} "
        f"modelo={lease.get('model') or '-'} host={lease['host']} origem={lease['origin']} "
        f"decidido_em={lease['decided_at']} heartbeat={lease['last_heartbeat']} "
        f"expira_em={lease['expires_at']} idade_s={lease['age_seconds']} "
        f"frescor={_human_freshness(str(lease['freshness']))}"
    )


def _session_lines(sessions: list[dict[str, Any]]) -> list[str]:
    if not sessions:
        return ["nenhuma"]
    lines = []
    for session in sessions:
        lines.append(
            f"sessao={session['session_id']} harness={session['harness']} "
            f"modelo={session.get('model') or '-'} host={session['host']} "
            f"heartbeat={session['last_heartbeat']} idade_s={session['age_seconds']} "
            f"frescor={_human_freshness(str(session['freshness']))}"
        )
    return lines


def _section_lines(section: dict[str, Any], item_text: Callable[[dict[str, Any]], str]) -> list[str]:
    header = (
        f"fonte={section['source']} observado_em={section['observed_at']} "
        f"origem_em={section.get('source_time') or '-'} idade_s={_blank(section.get('age_seconds'))} "
        f"frescor={_human_freshness(section['freshness'])}"
    )
    if section.get("cause"):
        header += f" causa={section['cause']}"
    if not section.get("items"):
        return [header, "nenhum item"]
    return [header, *[item_text(item) for item in section["items"]]]


def _queue_item(item: dict[str, Any]) -> str:
    return f"{item['id']} | {item['status']} | {item['title']} | atualizado_em={item.get('updated_at') or '-'}"


def _quota_item(item: dict[str, Any]) -> str:
    remaining = "-" if item.get("remaining_percent") is None else f"{item['remaining_percent']}%"
    confirmed = "sim" if item.get("confirmed") else "nao"
    cause = f" causa={item['cause']}" if item.get("cause") else ""
    return (
        f"{item['harness']}: restante={remaining} verificado_em={item.get('checked_at') or '-'} "
        f"idade_s={_blank(item.get('age_seconds'))} frescor={_human_freshness(item['freshness'])} "
        f"confirmado={confirmed}{cause}"
    )


def _run_item(item: dict[str, Any]) -> str:
    return (
        f"{item['run_id']} | {item['ticket_id']} | {item.get('state')} | "
        f"fase={item.get('current_stage') or '-'} | atualizado_em={item.get('updated_at') or '-'} "
        f"idade_s={_blank(item.get('age_seconds'))} frescor={_human_freshness(item['freshness'])}"
    )


def _pull_request_item(item: dict[str, Any]) -> str:
    return (
        f"#{item.get('number')} | {item.get('title') or ''} | {item.get('url') or ''} | "
        f"atualizado_em={item.get('updated_at') or '-'} frescor={_human_freshness(item['freshness'])}"
    )


def _human_freshness(value: str) -> str:
    return {"current": "vigente", "stale": "stale", "unavailable": "indisponivel"}.get(value, value)


def _blank(value: Any) -> str:
    return "-" if value is None else str(value)


def _newest_stamp(values: Any) -> str | None:
    stamps = [str(value) for value in values if value]
    if not stamps:
        return None
    return max(stamps)


def _oldest_stamp(values: Any) -> str | None:
    stamps = [str(value) for value in values if value]
    if not stamps:
        return None
    return min(stamps)


def _iso(moment: datetime) -> str:
    return _aware(moment).isoformat().replace("+00:00", "Z")


def _aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue
