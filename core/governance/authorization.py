"""Verificacao de autorizacao assinada de evolucao de governanca (USR-343 S1).

Um registro JSON em ``.factory/governance/authorizations/<id>.json`` vincula
ticket, aprovador, caminhos protegidos exatos, candidato (commit ou tree) e
expiracao, e carrega uma assinatura Ed25519 do owner sobre o JSON canonico
(sem o campo ``signature``). A chave publica vem de
``.factory/governance/owner_pubkey`` (base64 de 32 bytes), lida da BASE
CONFIAVEL via ``git show``, junto com os registros. Registro sem assinatura
valida, chave ausente ou placeholder, ou qualquer duvida: bloqueia. Roda so
com stdlib + ``cryptography`` (o CI executa o guard sem pydantic). Sem rede.
"""

from __future__ import annotations

import base64
import binascii
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

AUTHORIZATIONS_DIR = ".factory/governance/authorizations"
OWNER_PUBKEY_PATH = ".factory/governance/owner_pubkey"
DEFAULT_TRUSTED_BASE = "origin/main"
OWNER_APPROVERS: tuple[str, ...] = ("owner",)
_GLOB_CHARS = frozenset("*?[]")
_FIELDS = frozenset(
    {"id", "ticket_id", "approver", "paths", "candidate_kind", "candidate", "expires_at"}
)


@dataclass(frozen=True)
class AuthorizationResult:
    ok: bool
    reason: str
    record_id: str | None = None


def canonical_payload(record: dict[str, Any]) -> bytes:
    """Bytes assinados: JSON canonico do registro sem ``signature``."""
    body = {k: v for k, v in record.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


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
    return [
        (n, _git(repository, "show", f"{trusted_base}:{n}"))
        for n in sorted(n for n in names if n.endswith(".json"))
    ]


def _load_pubkey(repository: Path, trusted_base: str) -> Ed25519PublicKey | None:
    text = _git(repository, "show", f"{trusted_base}:{OWNER_PUBKEY_PATH}").strip()
    try:
        return Ed25519PublicKey.from_public_bytes(base64.b64decode(text, validate=True))
    except (ValueError, binascii.Error):
        return None


def _signature_ok(key: Ed25519PublicKey, record: dict[str, Any]) -> bool:
    try:
        key.verify(base64.b64decode(str(record.get("signature", "")), validate=True), canonical_payload(record))
    except (InvalidSignature, ValueError, binascii.Error):
        return False
    return True


def verify_authorization(
    violations: Iterable[str],
    *,
    repository: Path,
    trusted_base: str = DEFAULT_TRUSTED_BASE,
    now: datetime | None = None,
    approvers: tuple[str, ...] = OWNER_APPROVERS,
) -> AuthorizationResult:
    """Authorize ``violations`` only with a validly signed record on the trusted base."""
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
        key = _load_pubkey(repository, trusted_base)
        if key is None:
            return AuthorizationResult(False, "owner public key missing or invalid on trusted base")
        raw_records = _load_base_records(repository, trusted_base)
    except (subprocess.CalledProcessError, OSError) as exc:
        return AuthorizationResult(False, f"trusted base unreadable: {exc}")
    if not raw_records:
        return AuthorizationResult(False, "no authorization record on trusted base")

    reasons: list[str] = []
    for name, text in raw_records:
        try:
            rec = json.loads(text)
            if not isinstance(rec, dict) or not _FIELDS <= rec.keys() <= _FIELDS | {"signature"}:
                raise ValueError("fields")
            exp = datetime.fromisoformat(str(rec["expires_at"]).replace("Z", "+00:00"))
            paths = rec["paths"]
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise ValueError("paths")
        except (ValueError, TypeError):
            reasons.append(f"{name}: malformed")
            continue
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        rid = str(rec["id"])
        kind = rec["candidate_kind"]
        if not _signature_ok(key, rec):
            reasons.append(f"{rid}: invalid or missing signature")
        elif rec["approver"] not in approvers:
            reasons.append(f"{rid}: approver not allowed")
        elif exp <= moment:
            reasons.append(f"{rid}: expired")
        elif not _valid_exact_paths(paths):
            reasons.append(f"{rid}: paths must be exact")
        elif not wanted <= {_norm(p) for p in paths}:
            reasons.append(f"{rid}: paths do not cover candidate changes")
        elif kind not in ("commit", "tree") or rec["candidate"] != identity[0 if kind == "commit" else 1]:
            reasons.append(f"{rid}: candidate mismatch")
        elif not str(rec["ticket_id"]).strip():
            reasons.append(f"{rid}: missing ticket")
        else:
            return AuthorizationResult(True, "authorized", rid)
    return AuthorizationResult(False, "; ".join(reasons))
