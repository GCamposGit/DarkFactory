"""Read-only verifier for the USR-279 pilot M3 Telegram demand."""

from __future__ import annotations

import argparse
import inspect
import json
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.intake.project_routing import resolve_demand
from core.projects import registry as project_registry
from core.projects.registry import ProjectRegistry

DEFAULT_PROJECT = "pilot-hello"
DEFAULT_DEMAND_TEXT = (
    "pagina inicial com o texto DarkFac pilot e endpoint /healthz que devolve status ok e a versao"
)


@dataclass(frozen=True)
class M3Report:
    checked_at: str
    external_id: str | None
    demand_text: str
    ticket_id: str | None
    project_id: str | None
    created_at: str | None
    has_ph_ticket: bool
    has_usr_ticket_for_entry: bool
    routing_excerpt: str
    exit_code: int
    diagnostic: str


def _load_rows(path: Path) -> list[dict[str, Any]]:
    """Read the demand ledger without creating directories or files."""
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Nao foi possivel ler o ledger {path}: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("demands", [])
    if not isinstance(payload, list):
        raise ValueError(f"Formato de ledger invalido: {path}")
    return [row for row in payload if isinstance(row, dict)]


def _ticket_time(row: dict[str, Any]) -> datetime | None:
    value = row.get("created_at")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _normalized(value: str) -> str:
    return " ".join(value.casefold().split())


def _matches_entry(row: dict[str, Any], *, external_id: str | None, demand_text: str) -> bool:
    searchable: list[str] = []
    for key in ("title", "problem_statement", "core_journey", "tags", "acceptance_criteria"):
        value = row.get(key)
        if isinstance(value, str):
            searchable.append(value)
        elif isinstance(value, list):
            searchable.extend(str(item) for item in value)
    haystack = _normalized(" ".join(searchable))
    if external_id and external_id in haystack:
        return True
    return _normalized(demand_text) in haystack


def _routing_trace(*, project_id: str, demand_text: str, external_id: str | None, projects_file: Path | None) -> str:
    previous = project_registry._GLOBAL_REGISTRY
    if projects_file is not None:
        project_registry._GLOBAL_REGISTRY = ProjectRegistry(projects_file)
    try:
        # resolve_demand may persist onboarding for an unknown explicit project;
        # confine that path to a temporary directory, never the repository.
        with tempfile.TemporaryDirectory(prefix="pilot-m3-routing-") as temporary_state:
            route = resolve_demand(
                f"{project_id}: {demand_text}",
                Path(temporary_state),
                external_id=external_id or "pilot-m3-read-only-check",
                requested_by=None,
            )
        source = inspect.getsource(resolve_demand).splitlines()
        start = next((index for index, line in enumerate(source) if line.startswith("def resolve_demand")), 0)
        excerpt = "\n".join(source[start : start + 14])
        return f"resolve_demand returned action={route.action}, project_id={route.project_id}\n{excerpt}"
    finally:
        project_registry._GLOBAL_REGISTRY = previous


def verify_m3(
    *,
    demands_path: Path,
    projects_file: Path | None,
    external_id: str | None,
    demand_text: str,
    since: datetime | None = None,
    project_id: str = DEFAULT_PROJECT,
) -> M3Report:
    """Correlate the canonical input to ledger rows and exercise routing in a temp state dir."""
    candidates = [
        row
        for row in _load_rows(demands_path)
        if _matches_entry(row, external_id=external_id, demand_text=demand_text)
    ]
    if since is not None:
        since_utc = since.replace(tzinfo=UTC) if since.tzinfo is None else since.astimezone(UTC)
        candidates = [row for row in candidates if (_ticket_time(row) or datetime.min.replace(tzinfo=UTC)) >= since_utc]

    ph_match = next(
        (row for row in candidates if str(row.get("id", "")).startswith("PH-") and row.get("project_id") == project_id),
        None,
    )
    usr_match = next((row for row in candidates if str(row.get("id", "")).startswith("USR-")), None)
    wrong_project_match = next((row for row in candidates if row.get("project_id") != project_id), None)
    match = usr_match or wrong_project_match or ph_match or (candidates[0] if candidates else None)
    matched_id = str(match.get("id")) if match and match.get("id") is not None else None
    matched_project = str(match.get("project_id")) if match and match.get("project_id") is not None else None
    matched_external = external_id
    if match and matched_external is None:
        tags = [str(item) for item in match.get("tags", [])] if isinstance(match.get("tags"), list) else []
        journey = match.get("core_journey", [])
        linked = tags + ([str(item) for item in journey] if isinstance(journey, list) else [str(journey)])
        found = next((re.search(r"tg-msg-[\w-]+", item) for item in linked if "tg-msg-" in item), None)
        matched_external = found.group(0) if found else None

    is_ph = ph_match is not None
    is_usr = any(str(row.get("id", "")).startswith("USR-") for row in candidates)
    wrong_project = any(row.get("project_id") != project_id for row in candidates)
    if candidates and (wrong_project or is_usr or not is_ph):
        code, diagnostic = 1, "violacao: a entrada foi registrada fora do projeto esperado ou sem ticket PH"
    elif match:
        code, diagnostic = 0, "criterios comprovados: ticket PH vinculado ao projeto esperado; nenhum ticket USR- para a entrada"
    else:
        code, diagnostic = 2, "entrada ausente: nenhuma demanda correlacionavel foi encontrada no ledger"

    return M3Report(
        checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
        external_id=matched_external,
        demand_text=demand_text,
        ticket_id=matched_id,
        project_id=matched_project,
        created_at=str(match.get("created_at")) if match and match.get("created_at") is not None else None,
        has_ph_ticket=is_ph,
        has_usr_ticket_for_entry=is_usr,
        routing_excerpt=_routing_trace(
            project_id=project_id,
            demand_text=demand_text,
            external_id=matched_external,
            projects_file=projects_file,
        ),
        exit_code=code,
        diagnostic=diagnostic,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    parser.add_argument("--demands", type=Path, default=Path(".factory/demands/demands.json"))
    parser.add_argument("--projects", type=Path, default=Path(".factory/projects.json"))
    parser.add_argument("--external-id", help="Telegram message id (tg-msg-<chat>-<message>); optional if demand text is unique")
    parser.add_argument("--text", default=DEFAULT_DEMAND_TEXT)
    parser.add_argument("--since", help="Only consider tickets created at or after this ISO-8601 timestamp")
    args = parser.parse_args()
    try:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00")) if args.since else None
        report = verify_m3(
            demands_path=args.demands,
            projects_file=args.projects,
            external_id=args.external_id,
            demand_text=args.text,
            since=since,
            project_id=args.project,
        )
    except ValueError as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    print(json.dumps(asdict(report), indent=2, ensure_ascii=False))
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
