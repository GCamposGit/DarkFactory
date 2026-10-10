#!/usr/bin/env python3
"""Confere TODAS as fontes de token do Telegram por papel (USR-197).

Para cada papel (owner, ops) e cada fonte (variaveis de ambiente do processo, variaveis do escopo
User do Windows, ``.env`` e os JSON em ``.factory/telegram``), chama ``getMe`` e imprime uma tabela
``papel | fonte | estado``. Aponta qual fonte o loader (``load_telegram_config``) usa de fato e sai
com codigo 1 quando a fonte efetiva OU qualquer fonte secundaria esta REVOGADA, para o owner limpar.

Nunca imprime token inteiro: so os 4 ultimos caracteres.

Uso (PowerShell):
    python C:\\dev\\DarkFac\\scripts\\telegram_tokens_check.py
    python C:\\dev\\DarkFac\\scripts\\telegram_tokens_check.py --offline   # so formato/espacos, sem rede
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT = 4.0

# Same keys, same order as core.integrations.telegram.load_telegram_config.
ROLE_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "owner": ("TELEGRAM_OWNER_BOT_TOKEN",),
    "ops": ("TELEGRAM_OPS_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
}
ROLE_JSON_FILES: dict[str, tuple[str, ...]] = {
    "owner": ("owner_config.json", "config.json"),
    "ops": ("ops_config.json", "config.json"),
}
TOKEN_FORMAT = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")

VALID = "VALIDO"
REVOKED = "REVOGADO"
EMPTY = "vazio"
ABSENT = "ausente"
NETWORK_ERROR = "ERRO(rede)"
BAD_FORMAT = "FORMATO INVALIDO"
FORMAT_OK = "formato ok"

# fetch(token, timeout) -> (outcome, bot_username); outcome in {"valid", "revoked", "error"}.
Fetch = Callable[[str, float], tuple[str, str | None]]
UserEnvReader = Callable[[str], str | None]


@dataclass
class SourceRow:
    role: str
    source: str  # human label, e.g. "env(processo):TELEGRAM_OPS_BOT_TOKEN"
    kind: str  # "process" | "user" | "dotenv" | "json"
    value: str | None  # None = absent, "" = empty
    state: str = ""
    suffix: str = ""
    notes: list[str] | None = None
    effective: bool = False
    loader_reads: bool = True

    @property
    def revoked(self) -> bool:
        return self.state == REVOKED


def mask_suffix(token: str | None) -> str:
    """Only the last 4 characters of a token are ever shown."""
    clean = (token or "").strip()
    return f"...{clean[-4:]}" if len(clean) >= 12 else ""


def fetch_get_me(token: str, timeout: float = DEFAULT_TIMEOUT) -> tuple[str, str | None]:
    """Real ``getMe`` call. Never raises, never leaks the token (only exception class/HTTP code)."""
    request = urllib.request.Request(
        f"{API_BASE}/bot{token}/getMe", headers={"User-Agent": "DarkFactory-TokensCheck/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Telegram answers 401 for a revoked token and 404 for a malformed one.
        return ("revoked", None) if exc.code in (401, 404) else ("error", None)
    except Exception:
        return ("error", None)
    if isinstance(data, dict) and data.get("ok"):
        result = data.get("result")
        username = result.get("username") if isinstance(result, dict) else None
        return ("valid", str(username) if username else None)
    return ("revoked", None)


def read_windows_user_env(name: str) -> str | None:
    """Value of a User-scope environment variable (HKCU\\Environment); None off Windows or if unset."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _kind = winreg.QueryValueEx(key, name)
        return str(value)
    except OSError:
        return None


def _whitespace_note(value: str) -> str | None:
    if value != value.strip():
        has_newline = bool(re.search(r"[\r\n]", value[: len(value) - len(value.lstrip())] + value[len(value.rstrip()) :]))
        return "tem QUEBRA DE LINHA nas pontas" if has_newline else "tem ESPACOS nas pontas"
    if re.search(r"\s", value):
        return "tem espaco/quebra NO MEIO do valor"
    return None


def collect_rows(
    role: str,
    root: Path,
    user_env_reader: UserEnvReader = read_windows_user_env,
) -> list[SourceRow]:
    """All sources for ``role`` in the loader's precedence order (process env, .env, JSON files)."""
    from core.integrations.telegram import _read_env_fallback

    rows: list[SourceRow] = []
    dotenv = _read_env_fallback(root)
    keys = ROLE_ENV_KEYS[role]
    for key in keys:
        rows.append(SourceRow(role, f"env(processo):{key}", "process", os.environ.get(key)))
    for key in keys:
        rows.append(SourceRow(role, f".env:{key}", "dotenv", dotenv.get(key)))
    cfg_dir = root / ".factory" / "telegram"
    for name in ROLE_JSON_FILES[role]:
        path = cfg_dir / name
        value: str | None = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "bot_token" in data:
                value = "" if data["bot_token"] is None else str(data["bot_token"])
        except (OSError, ValueError):
            value = None
        rows.append(SourceRow(role, f".factory/telegram/{name}", "json", value))
    for key in keys:
        # The loader never reads the User scope; it matters because new processes inherit it.
        rows.append(SourceRow(role, f"env(User Windows):{key}", "user", user_env_reader(key), loader_reads=False))
    return rows


def _mark_effective(role: str, rows: list[SourceRow], root: Path) -> None:
    """Replicate the precedence by calling the real loader and flag the row it picked."""
    import logging

    from core.integrations.telegram import load_telegram_config

    # The loader warns about diverging sources with 8-char fingerprints; this tool shows 4 at most.
    loader_log = logging.getLogger("darkfac.integrations.telegram")
    previous = loader_log.level
    loader_log.setLevel(logging.ERROR)
    try:
        token = load_telegram_config(role=role, config_dir=root / ".factory" / "telegram").bot_token
    finally:
        loader_log.setLevel(previous)
    if not token:
        return
    for row in rows:
        if row.loader_reads and row.value and row.value.strip() == token:
            row.effective = True
            return


def evaluate(
    rows: list[SourceRow],
    *,
    offline: bool,
    fetch: Fetch,
    timeout: float,
) -> None:
    cache: dict[str, tuple[str, str | None]] = {}
    for row in rows:
        row.notes = []
        if row.value is None:
            row.state = ABSENT
            continue
        if row.value == "":
            row.state = EMPTY
            continue
        note = _whitespace_note(row.value)
        if note:
            row.notes.append(note)
        token = row.value.strip()
        row.suffix = mask_suffix(token)
        if not TOKEN_FORMAT.match(token):
            row.state = BAD_FORMAT
            continue
        if offline:
            row.state = FORMAT_OK
            continue
        if token not in cache:
            cache[token] = fetch(token, timeout)
        outcome, username = cache[token]
        if outcome == "valid":
            row.state = f"{VALID}(@{username})" if username else VALID
        elif outcome == "revoked":
            row.state = REVOKED
        else:
            row.state = NETWORK_ERROR


def _render(rows: list[SourceRow]) -> list[str]:
    headers = ("papel", "fonte", "final", "estado")
    body: list[tuple[str, str, str, str]] = []
    for row in rows:
        marks = []
        if row.effective:
            marks.append("<- EFETIVA (usada pelo loader)")
        if not row.loader_reads and row.value:
            marks.append("(nao lida pelo loader; novos processos herdam)")
        state = " ".join([row.state, *marks]).strip()
        body.append((row.role, row.source, row.suffix or "-", state))
    widths = [max(len(h), *(len(r[i]) for r in body)) if body else len(h) for i, h in enumerate(headers)]
    fmt = " | ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), "-+-".join("-" * w for w in widths)]
    lines.extend(fmt.format(*r) for r in body)
    return lines


def run_check(
    *,
    root: Path = REPO_ROOT,
    offline: bool = False,
    timeout: float = DEFAULT_TIMEOUT,
    fetch: Fetch = fetch_get_me,
    user_env_reader: UserEnvReader = read_windows_user_env,
    roles: tuple[str, ...] = ("owner", "ops"),
) -> tuple[list[str], int]:
    """Return (output lines, exit code)."""
    out: list[str] = []
    exit_code = 0
    all_rows: list[SourceRow] = []
    findings: list[str] = []
    for role in roles:
        rows = collect_rows(role, root, user_env_reader)
        _mark_effective(role, rows, root)
        evaluate(rows, offline=offline, fetch=fetch, timeout=timeout)
        all_rows.extend(rows)
        effective = next((r for r in rows if r.effective), None)
        if effective is None:
            findings.append(f"[{role}] SEM token efetivo: nenhuma fonte lida pelo loader tem valor.")
            exit_code = 1
        else:
            if effective.revoked:
                findings.append(f"[{role}] A fonte EFETIVA ({effective.source}) esta REVOGADA.")
                exit_code = 1
            elif offline and effective.state == BAD_FORMAT:
                findings.append(f"[{role}] A fonte EFETIVA ({effective.source}) tem formato invalido.")
                exit_code = 1
        for row in rows:
            if row.revoked and not row.effective:
                findings.append(f"[{role}] Fonte secundaria REVOGADA: {row.source} (limpe ou atualize).")
                exit_code = 1
            elif offline and row.state == BAD_FORMAT and not row.effective:
                findings.append(f"[{role}] Fonte secundaria com formato invalido: {row.source}.")
                exit_code = 1
            for note in row.notes or []:
                findings.append(f"[{role}] AVISO {row.source}: {note}.")
    out.extend(_render(all_rows))
    out.append("")
    if offline:
        out.append("Modo --offline: so formato e espacos foram verificados (nenhuma chamada de rede).")
    out.extend(findings or ["Nenhum problema encontrado."])
    out.append("RESULTADO: " + ("PROBLEMAS ENCONTRADOS (codigo 1)" if exit_code else "OK (codigo 0)"))
    return out, exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="valida so formato/espacos, sem chamar a rede")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="timeout do getMe em segundos")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    lines, code = run_check(offline=args.offline, timeout=args.timeout)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
