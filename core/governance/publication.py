"""Aprovacao por toque no Telegram para PRs que tocam arquivos protegidos (USR-343 S3).

Fluxo: PR aberto/atualizado -> o bot avisa o owner (PR, caminhos protegidos, sha, resumo do diff)
com botoes Aprovar/Rejeitar. O callback e aceito so com o segredo do webhook (verificado pelo Hub) e
``from.id == TELEGRAM_OWNER_CHAT_ID``. O broker (processo do Hub, nunca um agente) monta o registro
de autorizacao, ASSINA com a chave Ed25519 do owner (``OWNER_SIGNING_KEY`` ou cofre), grava em
``.factory/governance/authorizations/`` na main via REST com o token do GitHub App e faz o merge
(squash, ``sha`` = head aprovado) quando os checks obrigatorios estiverem verdes (sweep pendente).
"""

from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.governance.authorization import AUTHORIZATIONS_DIR, canonical_payload
from core.orchestrator.guard import audit_paths

logger = logging.getLogger(__name__)

SIGNING_KEY_ENV = "OWNER_SIGNING_KEY"
OWNER_CHAT_ENV = "TELEGRAM_OWNER_CHAT_ID"
DEFAULT_REPO = "GCamposGit/DarkFactory"
DEFAULT_TTL = timedelta(hours=24)
CALLBACK_PREFIX = "pp"


class PublicationError(Exception):
    """Pedido recusado (fail-closed)."""


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    head_sha: str
    files: tuple[str, ...]
    ticket_id: str
    state: str = "open"
    title: str = ""


class GitHubPort(Protocol):
    def get_pull_request(self, number: int) -> PullRequestInfo: ...

    def diff_summary(self, number: int) -> str: ...

    def commit_to_main(self, path: str, content: str, message: str) -> str: ...

    def checks_green(self, sha: str) -> bool: ...

    def merge(self, number: int, expected_head_sha: str) -> bool: ...


class RestGitHubPort:
    """Porta real via GitHub REST com o token do GitHub App (core.git.app_token)."""

    API = "https://api.github.com"

    def __init__(self, repo: str | None = None, token: str | None = None, client: httpx.Client | None = None) -> None:
        from core.git.app_token import get_app_token

        self.repo = repo or os.environ.get("DARKFAC_GH_REPO", DEFAULT_REPO)
        tok = token or get_app_token()
        if not tok:
            raise PublicationError("token do GitHub App indisponivel (GH_APP_* ausentes)")
        self._client = client or httpx.Client(timeout=30.0)
        self._headers = {
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "darkfac-factory",
        }

    def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            res = self._client.request(method, f"{self.API}/repos/{self.repo}/{path}", headers=self._headers, **kw)
        except httpx.HTTPError as exc:
            raise PublicationError(f"GitHub inacessivel: {type(exc).__name__}") from exc
        if res.status_code not in (200, 201):
            raise PublicationError(f"GitHub {method} {path.split('?')[0]} -> {res.status_code}")
        return res

    def _files(self, number: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            chunk = self._req("GET", f"pulls/{number}/files?per_page=100&page={page}").json()
            out.extend(chunk)
            if len(chunk) < 100:
                return out
            page += 1

    def get_pull_request(self, number: int) -> PullRequestInfo:
        pr = self._req("GET", f"pulls/{number}").json()
        match = re.search(r"USR-\d+", f"{pr.get('title', '')} {pr.get('head', {}).get('ref', '')}")
        state = "merged" if pr.get("merged") else pr.get("state", "")
        files = tuple(f["filename"] for f in self._files(number))
        return PullRequestInfo(number, pr["head"]["sha"], files, match.group(0) if match else "", state, pr.get("title", ""))

    def diff_summary(self, number: int) -> str:
        files = self._files(number)
        return "\n".join(f"{f['filename']} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})" for f in files[:15])

    def commit_to_main(self, path: str, content: str, message: str) -> str:
        body = {"message": message, "branch": "main", "content": base64.b64encode(content.encode("utf-8")).decode("ascii")}
        return self._req("PUT", f"contents/{path}", json=body).json()["commit"]["sha"]

    def checks_green(self, sha: str) -> bool:
        runs = self._req("GET", f"commits/{sha}/check-runs?per_page=100").json().get("check_runs", [])
        if not runs or any(r.get("status") != "completed" for r in runs):
            return False
        if any(r.get("conclusion") not in ("success", "neutral", "skipped") for r in runs):
            return False
        statuses = self._req("GET", f"commits/{sha}/status").json().get("statuses", [])
        return not any(st.get("state") in ("failure", "error") for st in statuses)

    def merge(self, number: int, expected_head_sha: str) -> bool:
        """Squash merge fixado no sha. False = ainda nao mergeavel (checks/regras)."""
        try:
            res = self._client.put(
                f"{self.API}/repos/{self.repo}/pulls/{number}/merge",
                headers=self._headers,
                json={"merge_method": "squash", "sha": expected_head_sha},
            )
        except httpx.HTTPError as exc:
            raise PublicationError(f"GitHub inacessivel: {type(exc).__name__}") from exc
        if res.status_code == 200:
            return True
        if res.status_code in (405, 409):
            return False
        raise PublicationError(f"merge -> {res.status_code}")


def load_signing_key(
    env: Mapping[str, str] | None = None, vault: Callable[[], Mapping[str, str]] | None = None
) -> Ed25519PrivateKey:
    """Chave privada do owner: ``OWNER_SIGNING_KEY`` (PEM, ``\\n`` aceito) ou o cofre. Nunca no env de agentes."""
    source = os.environ if env is None else env
    pem = source.get(SIGNING_KEY_ENV, "")
    if not pem:
        if vault is None:
            from core.infra.secrets_vault import decrypt as vault  # type: ignore[assignment]
        pem = (vault() or {}).get(SIGNING_KEY_ENV, "")  # type: ignore[misc]
    if not pem:
        raise PublicationError("chave de assinatura do owner ausente")
    try:
        key = serialization.load_pem_private_key(pem.replace("\\n", "\n").encode(), password=None)
    except (ValueError, TypeError) as exc:
        raise PublicationError("chave de assinatura invalida") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise PublicationError("chave de assinatura nao e Ed25519")
    return key


def build_signed_record(
    pr: PullRequestInfo, key: Ed25519PrivateKey, *, now: datetime, ttl: timedelta = DEFAULT_TTL
) -> dict[str, Any]:
    paths = sorted(audit_paths(list(pr.files)))
    if not paths:
        raise PublicationError("PR sem arquivos protegidos")
    if not pr.ticket_id:
        raise PublicationError("PR sem ticket USR-xxx no titulo/branch")
    record: dict[str, Any] = {
        "id": f"auth-{pr.ticket_id.lower()}-pr{pr.number}-{pr.head_sha[:8]}",
        "ticket_id": pr.ticket_id,
        "approver": "owner",
        "paths": paths,
        "candidate_kind": "commit",
        "candidate": pr.head_sha,
        "expires_at": (now + ttl).isoformat().replace("+00:00", "Z"),
    }
    record["signature"] = base64.b64encode(key.sign(canonical_payload(record))).decode("ascii")
    return record


Send = Callable[[int, str, list[list[dict[str, str]]]], Any]


def notify_owner(port: GitHubPort, pr_number: int, send: Send, owner_chat_id: int) -> bool:
    """Avisa o owner com botoes Aprovar/Rejeitar. False se o PR nao toca protegidos."""
    pr = port.get_pull_request(pr_number)
    protected = sorted(audit_paths(list(pr.files)))
    if pr.state != "open" or not protected:
        return False
    sha = pr.head_sha[:12]
    text = html.escape(
        f"PR #{pr.number} {pr.ticket_id} toca arquivos protegidos\n"
        f"sha {sha}\nProtegidos: {', '.join(protected)}\nDiff:\n{port.diff_summary(pr_number)}"
    )
    buttons = [
        [
            {"text": "Aprovar", "callback_data": f"{CALLBACK_PREFIX}:a:{pr.number}:{sha}"},
            {"text": "Rejeitar", "callback_data": f"{CALLBACK_PREFIX}:r:{pr.number}:{sha}"},
        ]
    ]
    send(owner_chat_id, text, buttons)
    return True


def is_publication_callback(callback: Mapping[str, Any]) -> bool:
    return str(callback.get("data", "")).startswith(f"{CALLBACK_PREFIX}:")


def _audit(audit_log: Path, entry: dict[str, Any]) -> None:
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    with audit_log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **entry}, ensure_ascii=False) + "\n")


def handle_callback(
    callback: Mapping[str, Any],
    *,
    owner_chat_id: int | None,
    port: GitHubPort,
    key_loader: Callable[[], Ed25519PrivateKey],
    pending_file: Path,
    audit_log: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Processa o toque. Fail-closed: so o owner (``from.id``), sha atual e PR aberto assinam."""
    sender = (callback.get("from") or {}).get("id")
    if owner_chat_id is None or sender != owner_chat_id:
        _audit(audit_log, {"event": "rejected-non-owner", "from": sender})
        return {"ok": False, "reason": "not owner"}
    try:
        _, action, number, sha_prefix = str(callback.get("data", "")).split(":")
        pr_number = int(number)
    except ValueError:
        return {"ok": False, "reason": "malformed callback"}
    if action == "r":
        _audit(audit_log, {"event": "rejected-by-owner", "pr": pr_number})
        return {"ok": True, "decision": "rejected", "pr": pr_number}
    if action != "a":
        return {"ok": False, "reason": "unknown action"}
    try:
        pr = port.get_pull_request(pr_number)
        if pr.state != "open":
            return {"ok": False, "reason": "PR nao esta aberto"}
        if not sha_prefix or not pr.head_sha.startswith(sha_prefix):
            return {"ok": False, "reason": "sha mudou desde o aviso; novo aviso necessario"}
        record = build_signed_record(pr, key_loader(), now=now or datetime.now(timezone.utc))
        main_sha = port.commit_to_main(
            f"{AUTHORIZATIONS_DIR}/{record['id']}.json",
            json.dumps(record, indent=2, ensure_ascii=False) + "\n",
            f"chore(governance): autorizacao do owner para PR #{pr_number} [{pr.ticket_id}]",
        )
    except PublicationError as exc:
        _audit(audit_log, {"event": "approve-failed", "pr": pr_number, "error": str(exc)})
        return {"ok": False, "reason": str(exc)}
    _pending_add(pending_file, pr_number, pr.head_sha, record["id"])
    _audit(
        audit_log,
        {"event": "approved", "pr": pr_number, "record": record["id"], "head_sha": pr.head_sha,
         "paths": record["paths"], "main_commit": main_sha},
    )
    return {"ok": True, "decision": "approved", "pr": pr_number, "record": record}


def _pending_load(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _pending_save(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2), encoding="utf-8")
    tmp.replace(path)


def _pending_add(path: Path, pr: int, sha: str, record_id: str) -> None:
    items = [i for i in _pending_load(path) if i.get("pr") != pr]
    items.append({"pr": pr, "head_sha": sha, "record_id": record_id, "status": "pending-merge"})
    _pending_save(path, items)


def sweep_pending(port: GitHubPort, pending_file: Path, audit_log: Path) -> list[dict[str, Any]]:
    """Mergeia PRs aprovados quando os checks estao verdes; descarta os invalidados (sha mudou)."""
    kept: list[dict[str, Any]] = []
    for item in _pending_load(pending_file):
        pr_no, sha = int(item["pr"]), str(item["head_sha"])
        outcome = "pending-merge"
        try:
            info = port.get_pull_request(pr_no)
            if info.state == "merged":
                outcome = "merged"
            elif info.state != "open" or info.head_sha != sha:
                outcome = "invalidated"
            elif port.checks_green(sha) and port.merge(pr_no, sha):
                outcome = "merged"
        except PublicationError as exc:
            logger.warning("sweep PR #%s: %s", pr_no, exc)
        if outcome == "pending-merge":
            kept.append(item)
        else:
            _audit(audit_log, {"event": outcome, "pr": pr_no, "head_sha": sha})
    _pending_save(pending_file, kept)
    return kept
