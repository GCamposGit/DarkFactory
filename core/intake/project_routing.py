"""Project resolution for the Telegram ``/demand`` and ``/newproject`` commands (USR-278).

Syntax (an explicit project is never silently redirected to ``darkfac``):

* ``/demand <text>``                 -> ``darkfac`` (documented default, internal demands)
* ``/demand <project_id>: <text>``   -> registered project, else onboarding request
* ``/demand --project <id> <text>``  -> same as above, unambiguous form
* ``/newproject <name>: <text>``     -> onboarding request for a new product

Onboarding creates a request record only. It never creates repositories (USR-274
handles bootstrap) and never starts code in the core.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

DEFAULT_PROJECT_ID = "darkfac"
ONBOARDING_FILE = "onboarding_requests.json"

_SLUG = r"[a-z0-9][a-z0-9_-]{1,63}"
_PREFIX_RE = re.compile(rf"^({_SLUG}):\s+(\S.*)$", re.DOTALL)
_FLAG_RE = re.compile(r"^--project[\s=]+(\S+)\s+(\S.*)$", re.DOTALL)
_NEWPROJECT_RE = re.compile(r"^(.{2,80}?)\s*:\s*(\S.*)$", re.DOTALL)
# Conventional-commit style prefixes are plain text, not project ids.
_NOT_PROJECTS = frozenset(
    {"fix", "feat", "bug", "chore", "docs", "refactor", "test", "tests", "perf", "hotfix", "todo", "nota", "note", "ideia"}
)

Action = Literal["accept", "onboarding", "usage"]


@dataclass(frozen=True)
class RouteDecision:
    action: Action
    project_id: str = DEFAULT_PROJECT_ID
    text: str = ""
    message: str = ""
    onboarding_request_id: str | None = None


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64]
    return slug if re.fullmatch(_SLUG, slug) else ""


def _known(project_id: str) -> bool:
    from core.projects.registry import get_project_registry

    return get_project_registry().get_project(project_id) is not None


def parse_demand_args(arg_str: str) -> tuple[str | None, str]:
    """Return ``(explicit_project_id | None, text)``."""
    arg = arg_str.strip()
    flag = _FLAG_RE.match(arg)
    if flag:
        return flag.group(1).strip().lower(), flag.group(2).strip()
    prefixed = _PREFIX_RE.match(arg)
    if prefixed and prefixed.group(1) not in _NOT_PROJECTS:
        return prefixed.group(1), prefixed.group(2).strip()
    return None, arg


def _record_onboarding(
    state_dir: Path, *, project_id: str, name: str, text: str, external_id: str, requested_by: int | None
) -> str:
    """Idempotent (by external_id) onboarding request record. Returns the request id."""
    path = state_dir / ONBOARDING_FILE
    rows: list[dict] = []
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            rows = loaded if isinstance(loaded, list) else []
        except (OSError, ValueError):
            rows = []
    for row in rows:
        if row.get("external_id") == external_id:
            return str(row["id"])
    request_id = f"ONB-{len(rows) + 1:04d}"
    rows.append(
        {
            "id": request_id,
            "status": "pending_confirmation",
            "project_id": project_id,
            "name": name,
            "description": text,
            "external_id": external_id,
            "requested_by": requested_by,
            "created_at": datetime.now(UTC).isoformat(),
            "creates_repository": False,
        }
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    return request_id


def _onboarding_reply(request_id: str, project_id: str, name: str) -> str:
    return (
        f"Projeto novo detectado: <b>{name}</b> (id <code>{project_id}</code>).\n"
        f"Solicitacao de onboarding registrada: <code>{request_id}</code> (pendente de confirmacao).\n"
        "Nenhum repositorio foi criado e nenhum codigo foi iniciado. Para seguir, o Grill precisa das "
        "decisoes de negocio (escopo, canais, repositorio). Depois de registrado o projeto, "
        f"use <code>/demand {project_id}: &lt;texto&gt;</code>."
    )


def request_onboarding(
    state_dir: Path, *, name: str, text: str, external_id: str, requested_by: int | None
) -> RouteDecision:
    project_id = slugify(name)
    if not project_id:
        return RouteDecision("usage", message="Nome de projeto invalido. Uso: /newproject <nome>: <descricao>")
    if _known(project_id):
        return RouteDecision(
            "usage",
            message=f"Projeto {project_id} ja esta registrado. Use /demand {project_id}: <texto>.",
        )
    request_id = _record_onboarding(
        state_dir, project_id=project_id, name=name.strip(), text=text, external_id=external_id,
        requested_by=requested_by,
    )
    return RouteDecision(
        "onboarding", project_id=project_id, text=text, onboarding_request_id=request_id,
        message=_onboarding_reply(request_id, project_id, name.strip()),
    )


def resolve_newproject(arg_str: str, state_dir: Path, *, external_id: str, requested_by: int | None) -> RouteDecision:
    match = _NEWPROJECT_RE.match(arg_str.strip())
    if not match:
        return RouteDecision("usage", message="Uso: /newproject <nome>: <descricao do produto>")
    return request_onboarding(
        state_dir, name=match.group(1), text=match.group(2).strip(), external_id=external_id,
        requested_by=requested_by,
    )


def resolve_demand(arg_str: str, state_dir: Path, *, external_id: str, requested_by: int | None) -> RouteDecision:
    """Decide where a ``/demand`` goes. Unknown explicit project => onboarding, never darkfac."""
    explicit, text = parse_demand_args(arg_str)
    if explicit is None:
        return RouteDecision("accept", project_id=DEFAULT_PROJECT_ID, text=text)
    if not re.fullmatch(_SLUG, explicit):
        return RouteDecision("usage", message=f"Id de projeto invalido: {explicit}")
    if _known(explicit):
        return RouteDecision("accept", project_id=explicit, text=text)
    return request_onboarding(
        state_dir, name=explicit, text=text, external_id=external_id, requested_by=requested_by
    )
