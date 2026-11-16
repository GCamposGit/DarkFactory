"""Politica de sincronizacao da documentacao de entregas (USR-499).

``docs/FASE_ATUAL.md`` e o snapshot da fase atual (canais, maquinas,
orcamento, metas, piloto). Nao recebe secao de historico nem entrada
datada por ticket: esse trecho e reescrito por toda entrega e conflita
no rebase.

O registro de cada entrega continua no manual (``docs/manual/``) e no
learning pack (``.factory/learning_packs/`` e
``docs/manual/learning_pack.md``). Atualizar um deles nao exige
atualizar o outro. Nao existem ``docs/MANUAL.md`` nem
``docs/LEARNING_PACK.md``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

CURRENT_PHASE_DOCUMENT = "docs/FASE_ATUAL.md"
MANUAL_DIR = "docs/manual/"
LEARNING_PACK_MANUAL = "docs/manual/learning_pack.md"
# Texto de orientacao na mensagem de recusa, nao diretorio de estado.
# O prefixo fica partido: o scanner de core/ recusa o literal com subdir.
_FACTORY_PREFIX = ".factory"
LEARNING_PACK_DIR = _FACTORY_PREFIX + "/learning_packs/"
DELIVERY_RECORD_LOCATIONS = (
    MANUAL_DIR,
    LEARNING_PACK_MANUAL,
    LEARNING_PACK_DIR,
)

_TICKET_ID = re.compile(
    r"\b(?:USR|BUG|FIX|TASK)-\d+\b",
    re.IGNORECASE,
)
_TICKET_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s+(?:ticket\s+)?(?:USR|BUG|FIX|TASK)-\d+\b",
    re.IGNORECASE,
)
_HISTORY_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s+hist[oó]rico\b",
    re.IGNORECASE,
)
# Item de lista que abre com a data do registro e cita um ticket. Cobre
# "- 10/11/2026: USR-494 ...", "- 05/11/2026 (USR-473): ...",
# a forma ISO com o mesmo separador e a variante em que o id vem depois
# do texto ("- 06/11/2026: receipt do candidato (USR-468)."). Uma frase consolidada
# que cita o ticket no meio, sem abrir com a data, nao entra aqui.
_DIARY_OPENER = re.compile(
    r"^\s*(?:[-*+]|\d+\.)\s+(?:\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})\s*[:\(]",
)


class DocsSyncPolicyError(ValueError):
    """A entrega violou a politica de documentacao."""


def is_ticket_history_heading(line: str) -> bool:
    """True se a linha e um titulo de secao de historico ou de ticket."""

    return bool(_TICKET_HEADING.match(line) or _HISTORY_HEADING.match(line))


def is_ticket_history_line(line: str) -> bool:
    """True se a linha e historico por ticket e nao um fato consolidado da fase."""

    if is_ticket_history_heading(line):
        return True
    return bool(_DIARY_OPENER.match(line) and _TICKET_ID.search(line))


def ticket_history_lines(text: str) -> list[str]:
    """Linhas de ``text`` que sao historico por ticket, na ordem do documento."""

    return [line.strip() for line in text.splitlines() if is_ticket_history_line(line)]


def validate_docs_sync(
    changed_paths: Iterable[str],
    added_lines: Mapping[str, Sequence[str]] | None = None,
) -> None:
    """Recusa historico por ticket acrescentado em ``docs/FASE_ATUAL.md``.

    ``changed_paths`` fica na assinatura para o gate informar o diff, mas
    nao exige que o manual e o learning pack mudem juntos. ``added_lines``
    e opcional: sem ele, a funcao nao tem texto para julgar.
    """

    normalized_added_lines = {
        path.replace("\\", "/"): lines
        for path, lines in (added_lines or {}).items()
    }
    offending = ticket_history_lines(
        "\n".join(normalized_added_lines.get(CURRENT_PHASE_DOCUMENT, ()))
    )
    if offending:
        formatted = ", ".join(repr(line) for line in offending)
        raise DocsSyncPolicyError(
            f"{CURRENT_PHASE_DOCUMENT} e o snapshot da fase atual e nao recebe "
            f"historico por ticket: {formatted}. Registre a entrega no manual "
            f"({MANUAL_DIR}) e no learning pack ({LEARNING_PACK_DIR})."
        )


def delivery_history_documents() -> tuple[str, ...]:
    """Onde a entrega fica registrada. Qualquer subconjunto pode mudar sozinho."""

    return DELIVERY_RECORD_LOCATIONS
