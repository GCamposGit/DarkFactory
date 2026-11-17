"""Offline, credential-safe comparison of the control store and progress sink (USR-455 T1).

The launcher resolves these destinations independently. This doctor uses the same resolvers but
never opens a socket or database connection, so it is safe to run while offline and cannot affect a
ticket or production data. Its report describes effective connection settings, not proof of reachability.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict

from core.line.local_progress import (
    ENV_SWITCH,
    PostgresSink,
    SWITCH_OFF,
    SqliteSink,
    resolve_sink_source,
)
from core.orchestrator.cloud_db import resolve_control_database, sanitize_database_target

Cause = Literal[
    "dsn_divergente",
    "host_sem_rota",
    "timeout_curto",
    "credencial_ausente",
    "no_url",
    "ok",
]
DEFAULT_CONTROL_TIMEOUT_S = 5.0
DEFAULT_SSLMODE = "prefer"
CONTROL_STORE_URL_ENV = "DARKFAC_HF02_DATABASE_URL"
_HOST_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_SSLMODES = frozenset({"disable", "allow", "prefer", "require", "verify-ca", "verify-full"})
_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})


class EffectiveTarget(BaseModel):
    """Safe subset of a destination's effective settings; never stores a user or URL."""

    model_config = ConfigDict(frozen=True)

    source: str
    host: str | None = None
    port: int | None = None
    database: str | None = None
    sslmode: str | None = None
    timeout_s: float | None = None
    statement_timeout_ms: int | None = None
    credential_present: bool = False


class DoctorResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    cause: Cause
    control_store: EffectiveTarget
    sink: EffectiveTarget
    detail: str


def _url_target(
    url: str,
    source: str,
    timeout_s: float,
    statement_timeout_ms: int,
    environ: Mapping[str, str],
) -> EffectiveTarget:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _POSTGRES_SCHEMES:
        # Not a URL at all (e.g. a stray secret-looking string in the env var): report that a value is
        # configured without echoing any part of it back.
        return EffectiveTarget(
            source=source, timeout_s=timeout_s, statement_timeout_ms=statement_timeout_ms
        )
    query = parse_qs(parsed.query)
    raw_sslmode = (query.get("sslmode", [None])[0] or environ.get("PGSSLMODE") or DEFAULT_SSLMODE).lower()
    sslmode = raw_sslmode if raw_sslmode in _SSLMODES else "invalid"
    target = sanitize_database_target(url)
    return EffectiveTarget(
        source=source,
        host=target.host,
        port=target.port or (int(environ["PGPORT"]) if environ.get("PGPORT", "").isdigit() else 5432),
        database=target.database,
        sslmode=sslmode,
        timeout_s=timeout_s,
        statement_timeout_ms=statement_timeout_ms,
        credential_present=bool(parsed.username and parsed.password),
    )


def _control_target(environ: Mapping[str, str]) -> EffectiveTarget:
    resolved = resolve_control_database(environ)
    if resolved is not None:
        return _url_target(
            resolved.url,
            CONTROL_STORE_URL_ENV,
            resolved.connect_timeout_s,
            resolved.statement_timeout_ms,
            environ,
        )
    return EffectiveTarget(source="control.db", database="control.db")


def _sink_target(sink: Any, source: str, environ: Mapping[str, str]) -> EffectiveTarget:
    if isinstance(sink, PostgresSink):
        return _url_target(
            sink.url,
            source,
            sink.connect_timeout_s,
            sink.connection.statement_timeout_ms,
            environ,
        )
    if isinstance(sink, SqliteSink):
        return EffectiveTarget(source=source, database=sink.path.name, timeout_s=sink.timeout_s)
    return EffectiveTarget(source=source)


def _classify(control: EffectiveTarget, sink: EffectiveTarget) -> tuple[Cause, str]:
    for target in (control, sink):
        if target.host is not None and not _HOST_RE.fullmatch(target.host):
            return "host_sem_rota", "host ausente ou malformado; rota de rede nao foi testada"
    if any(target.host is not None and not target.credential_present for target in (control, sink)):
        return "credencial_ausente", "usuario ou senha ausentes em uma URL PostgreSQL"
    if (control.host, control.port, control.database, control.sslmode) != (
        sink.host, sink.port, sink.database, sink.sslmode
    ):
        return "dsn_divergente", "host, porta, banco ou sslmode do sink difere do control store"
    if (
        (sink.timeout_s is not None and control.timeout_s is not None and sink.timeout_s < control.timeout_s)
        or (
            sink.statement_timeout_ms is not None
            and control.statement_timeout_ms is not None
            and sink.statement_timeout_ms < control.statement_timeout_ms
        )
    ):
        return "timeout_curto", "limite de conexao ou comando do sink menor que o do control store"
    return "ok", "destinos e configuracao efetiva coerentes; conectividade nao testada offline"


def diagnose(environ: Mapping[str, str] | None = None) -> DoctorResult:
    """Compare effective configuration without network or database access; never raises."""
    env: Mapping[str, str] = os.environ if environ is None else environ
    try:
        sink, source = resolve_sink_source(env)
        if sink is None:
            control = _control_target(env)
            sink_target = EffectiveTarget(source="disabled")
            deliberately_off = (env.get(ENV_SWITCH) or "").strip().lower() in SWITCH_OFF
            if deliberately_off:
                return DoctorResult(cause="ok", control_store=control, sink=sink_target,
                                    detail="publicacao desligada de proposito; nenhuma conexao foi aberta")
            return DoctorResult(cause="no_url", control_store=control, sink=sink_target,
                                detail="nenhum sink configurado; nenhuma URL de escrita foi resolvida")
        control = _control_target(env)
        sink_target = _sink_target(sink, source or "unknown", env)
        cause, detail = _classify(control, sink_target)
        return DoctorResult(cause=cause, control_store=control, sink=sink_target, detail=detail)
    except Exception as exc:  # noqa: BLE001 - a doctor failure is a structured result, never a traceback
        empty = EffectiveTarget(source="unavailable")
        return DoctorResult(cause="host_sem_rota", control_store=empty, sink=empty,
                            detail=f"configuracao nao pode ser resolvida ({type(exc).__name__}); nenhuma conexao foi aberta")


def _format_target(label: str, target: EffectiveTarget) -> str:
    return (f"{label}: source={target.source} host={target.host or '-'} port={target.port or '-'} "
            f"sslmode={target.sslmode or '-'} timeout_s={target.timeout_s if target.timeout_s is not None else '-'} "
            f"statement_timeout_ms={target.statement_timeout_ms if target.statement_timeout_ms is not None else '-'} "
            f"database={target.database or '-'} credential_present={'yes' if target.credential_present else 'no'}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compara a configuracao efetiva do control store e do sink sem rede.")
    parser.add_argument("--json", action="store_true", help="imprime o resultado estruturado em JSON")
    return parser


def main(argv: list[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = diagnose(environ)
    if args.json:
        print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False))
    else:
        print("[live_progress_doctor] Diagnostico offline; conectividade/rota nao testada.")
        print("[live_progress_doctor] " + _format_target("control_store", result.control_store))
        print("[live_progress_doctor] " + _format_target("sink", result.sink))
        print(f"[live_progress_doctor] causa: {result.cause} - {result.detail}")
    return 0 if result.cause == "ok" else 1


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001 - stdout may be wrapped by a host application
        pass
    raise SystemExit(main())
