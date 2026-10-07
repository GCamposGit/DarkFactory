"""Deteccao somente leitura de chamada de ferramenta sem resposta em sessao do Codex (USR-79).

O aplicativo Codex desktop grava cada sessao como JSONL em ``~/.codex/sessions``. Quando a
sessao e interrompida entre uma chamada de ferramenta (``custom_tool_call`` / ``function_call``)
e a sua saida (``*_output``), a retomada falha com "Custom tool call output is missing".
A fabrica NAO pode corrigir isso (arquivos internos do aplicativo); este modulo apenas detecta
o sintoma e sugere a recuperacao segura. Nunca escreve em arquivos de sessao.

Uso: ``python -m core.line.codex_session_probe <arquivo.jsonl>``
Codigo de saida: 0 sem chamadas orfas, 2 com chamadas orfas, 1 erro de leitura.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

_OUTPUT_SUFFIX = "_output"
_CALL_SUFFIX = "_call"
_MAX_SUMMARY = 160


@dataclass(frozen=True)
class UnansweredToolCall:
    """Chamada de ferramenta registrada sem a saida correspondente."""

    call_id: str
    call_type: str
    name: str
    line_number: int
    summary: str = ""


@dataclass(frozen=True)
class ProbeResult:
    """Resultado completo da inspecao de uma sessao."""

    path: str
    unanswered: tuple[UnansweredToolCall, ...]
    total_calls: int
    invalid_lines: int
    last_confirmed: str = ""

    @property
    def interrupted(self) -> bool:
        return bool(self.unanswered)


def _record_body(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else record


def _summarize(body: dict[str, Any]) -> str:
    for key in ("input", "arguments", "command", "action"):
        value = body.get(key)
        if value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        text = " ".join(text.split())
        return text[:_MAX_SUMMARY]
    return ""


def _iter_records(path: Path) -> Iterable[tuple[int, Optional[dict[str, Any]]]]:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                record = json.loads(raw)
            except ValueError:
                yield number, None
                continue
            yield number, record if isinstance(record, dict) else None


def probe_session(jsonl_path: Path | str) -> ProbeResult:
    """Le a sessao (somente leitura) e retorna chamadas sem resposta e metricas."""
    path = Path(jsonl_path)
    pending: dict[str, UnansweredToolCall] = {}
    answered: set[str] = set()
    total_calls = 0
    invalid = 0
    last_confirmed = ""
    for number, record in _iter_records(path):
        if record is None:
            invalid += 1
            continue
        body = _record_body(record)
        kind = body.get("type")
        call_id = body.get("call_id")
        if not isinstance(kind, str) or not isinstance(call_id, str) or not call_id:
            continue
        if kind.endswith(_OUTPUT_SUFFIX):
            answered.add(call_id)
            previous = pending.pop(call_id, None)
            if previous is not None:
                last_confirmed = previous.summary or previous.name or previous.call_id
        elif kind.endswith(_CALL_SUFFIX):
            total_calls += 1
            if call_id in answered:
                continue
            name = body.get("name")
            pending[call_id] = UnansweredToolCall(
                call_id=call_id,
                call_type=kind,
                name=name if isinstance(name, str) else "",
                line_number=number,
                summary=_summarize(body),
            )
    return ProbeResult(
        path=str(path),
        unanswered=tuple(pending.values()),
        total_calls=total_calls,
        invalid_lines=invalid,
        last_confirmed=last_confirmed,
    )


def find_unanswered_tool_calls(jsonl_path: Path | str) -> list[UnansweredToolCall]:
    """Lista chamadas de ferramenta sem ``*_output`` correspondente (ordem do arquivo)."""
    return list(probe_session(jsonl_path).unanswered)


def format_report(result: ProbeResult) -> str:
    """Relatorio curto em portugues com a recuperacao sugerida."""
    if not result.interrupted:
        return (
            f"Sessao Codex sem chamadas orfas ({result.total_calls} chamadas inspecionadas): {result.path}"
        )
    lines = [
        f"ALERTA: {len(result.unanswered)} chamada(s) de ferramenta sem resposta em {result.path}",
    ]
    for call in result.unanswered:
        label = f"{call.call_type} {call.name}".strip()
        lines.append(f"  - call_id={call.call_id} ({label}) linha {call.line_number}")
    if result.last_confirmed:
        lines.append(f"Ultimo comando confirmado: {result.last_confirmed}")
    lines.append(
        "Recuperacao: NAO edite o JSONL. Copie-o como evidencia e abra uma tarefa nova com o "
        "estado de trabalho e o ultimo comando confirmado "
        "(docs/runbooks/codex_session_orphan_tool_call.md)."
    )
    return "\n".join(lines)


def warn_if_unanswered(jsonl_path: Path | str, log: Optional[logging.Logger] = None) -> Optional[ProbeResult]:
    """Registra aviso se a sessao tiver chamada orfa. Nunca levanta excecao."""
    target = log or logger
    try:
        result = probe_session(jsonl_path)
    except OSError as exc:
        target.warning("Sessao Codex ilegivel (%s): %s", jsonl_path, exc)
        return None
    if result.interrupted:
        target.warning("%s", format_report(result))
    return result


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("uso: python -m core.line.codex_session_probe <arquivo.jsonl>", file=sys.stderr)
        return 1
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    try:
        result = probe_session(args[0])
    except OSError as exc:
        print(f"[ERRO] Nao foi possivel ler {args[0]}: {exc}", file=sys.stderr)
        return 1
    print(format_report(result))
    return 2 if result.interrupted else 0


if __name__ == "__main__":
    raise SystemExit(main())
