"""Verificacao de autorizacao de evolucao de governanca (USR-228, SQ-03).

Esquema: um registro JSON em ``.factory/governance/authorizations/<id>.json``
vincula ticket, aprovador (owner), caminhos protegidos exatos, candidato
(commit SHA ou tree hash) e expiracao. O registro so vale quando esta presente
na BASE CONFIAVEL (por padrao ``origin/main``), lido via ``git show`` e nunca
da arvore de trabalho do candidato; o agente de build escreve apenas o
candidato e portanto nao consegue emitir a propria autorizacao. Qualquer
duvida bloqueia (fail-closed). Sem rede.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

AUTHORIZATIONS_DIR = ".factory/governance/authorizations"
DEFAULT_TRUSTED_BASE = "origin/main"
OWNER_APPROVERS: tuple[str, ...] = ("owner",)
_GLOB_CHARS = frozenset("*?[]")


class AuthorizationRecord(BaseModel):
    """Registro de decisao do owner para uma evolucao protegida."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    ticket_id: str
    approver: str
    paths: tuple[str, ...]
    candidate_kind: Literal["commit", "tree"]
    candidate: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthorizationResult:
    ok: bool
    reason: str
    record_id: str | None = None


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repository,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )
    return result.stdout


def _norm(path: str) -> str:
    return path.replace("\\", "/").strip()


def _valid_exact_paths(paths: Iterable[str]) -> bool:
    items = [_norm(p) for p in paths]
    return bool(items) and all(
        p and not (_GLOB_CHARS & set(p)) and not p.startswith("/") and ".." not in p.split("/")
        for p in items
    )


def _candidate_identity(repository: Path) -> tuple[str, str] | None:
    """Return (commit, tree) of a clean HEAD, or None if unprovable."""
    if _git(repository, "status", "--porcelain", "--untracked-files=all").strip():
        return None
    commit = _git(repository, "rev-parse", "--verify", "HEAD^{commit}").strip()
    tree = _git(repository, "rev-parse", "--verify", "HEAD^{tree}").strip()
    return commit, tree


def _load_base_records(repository: Path, trusted_base: str) -> list[tuple[str, str]]:
    names = _git(
        repository, "ls-tree", "-r", "--name-only", "-z", trusted_base, "--", AUTHORIZATIONS_DIR
    ).split("\0")
    out: list[tuple[str, str]] = []
    for name in sorted(n for n in names if n.endswith(".json")):
        out.append((name, _git(repository, "show", f"{trusted_base}:{name}")))
    return out


def verify_authorization(
    violations: Iterable[str],
    *,
    repository: Path,
    trusted_base: str = DEFAULT_TRUSTED_BASE,
    now: datetime | None = None,
    approvers: tuple[str, ...] = OWNER_APPROVERS,
) -> AuthorizationResult:
    """Authorize ``violations`` only with a valid record on the trusted base."""
    wanted = {_norm(v) for v in violations}
    if not wanted:
        return AuthorizationResult(False, "no protected paths to authorize")
    moment = now if now is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        identity = _candidate_identity(repository)
        if identity is None:
            return AuthorizationResult(False, "candidate has uncommitted changes")
        raw_records = _load_base_records(repository, trusted_base)
    except (subprocess.CalledProcessError, OSError) as exc:
        return AuthorizationResult(False, f"trusted base unreadable: {exc}")
    if not raw_records:
        return AuthorizationResult(False, "no authorization record on trusted base")

    reasons: list[str] = []
    for name, text in raw_records:
        try:
            rec = AuthorizationRecord.model_validate(json.loads(text))
        except (ValueError, ValidationError):
            reasons.append(f"{name}: malformed")
            continue
        exp = rec.expires_at if rec.expires_at.tzinfo else rec.expires_at.replace(tzinfo=timezone.utc)
        if rec.approver not in approvers:
            reasons.append(f"{rec.id}: approver not allowed")
        elif exp <= moment:
            reasons.append(f"{rec.id}: expired")
        elif not _valid_exact_paths(rec.paths):
            reasons.append(f"{rec.id}: paths must be exact")
        elif not wanted <= {_norm(p) for p in rec.paths}:
            reasons.append(f"{rec.id}: paths do not cover candidate changes")
        elif rec.candidate not in (identity[0] if rec.candidate_kind == "commit" else identity[1],):
            reasons.append(f"{rec.id}: candidate mismatch")
        elif not rec.ticket_id.strip():
            reasons.append(f"{rec.id}: missing ticket")
        else:
            return AuthorizationResult(True, "authorized", rec.id)
    return AuthorizationResult(False, "; ".join(reasons))

