"""Política de sincronização da documentação de entregas.

``docs/FASE_ATUAL.md`` é um snapshot do estado corrente do produto. Ele não
deve receber changelogs ou seções históricas por ticket.

O histórico permanente das entregas continua sendo mantido no manual e no
learning pack.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

CURRENT_PHASE_DOCUMENT = "docs/FASE_ATUAL.md"
DELIVERY_HISTORY_DOCUMENTS = (
    "docs/MANUAL.md",
    "docs/LEARNING_PACK.md",
)

_TICKET_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s+(?:ticket\s+)?(?:USR|BUG|FIX|TASK)-\d+\b",
    re.IGNORECASE,
)
_HISTORY_HEADING = re.compile(
    r"^\s{0,3}#{1,6}\s+hist[oó]rico(?:\s+(?:de|por)\s+tickets?)?\s*$",
    re.IGNORECASE,
)


class DocsSyncPolicyError(ValueError):
    """Raised when a delivery violates the documentation sync policy."""


def is_ticket_history_heading(line: str) -> bool:
    """Return whether a Markdown line introduces ticket-by-ticket history."""

    return bool(_TICKET_HEADING.match(line) or _HISTORY_HEADING.match(line))


def validate_docs_sync(
    changed_paths: Iterable[str],
    added_lines: Mapping[str, Sequence[str]] | None = None,
) -> None:
    """Validate documentation changes made by a delivery.

    The phase snapshot may still be updated when the current phase actually
    changes. Only ticket-history headings are forbidden there. Ticket records
    belong to the manual and learning pack.

    ``added_lines`` is intentionally optional so callers that only calculate
    required documentation can use the same policy without reading diffs.
    """

    normalized_paths = {path.replace("\\", "/") for path in changed_paths}
    normalized_added_lines = {
        path.replace("\\", "/"): lines
        for path, lines in (added_lines or {}).items()
    }

    offending_lines = [
        line.strip()
        for line in normalized_added_lines.get(CURRENT_PHASE_DOCUMENT, ())
        if is_ticket_history_heading(line)
    ]
    if offending_lines:
        formatted = ", ".join(repr(line) for line in offending_lines)
        raise DocsSyncPolicyError(
            f"{CURRENT_PHASE_DOCUMENT} is a current-state snapshot and cannot "
            f"receive ticket history headings: {formatted}. Record the "
            "delivery in the manual and learning pack instead."
        )

    changed_history_documents = normalized_paths.intersection(
        DELIVERY_HISTORY_DOCUMENTS
    )
    if changed_history_documents and changed_history_documents != set(
        DELIVERY_HISTORY_DOCUMENTS
    ):
        missing = sorted(
            set(DELIVERY_HISTORY_DOCUMENTS) - changed_history_documents
        )
        raise DocsSyncPolicyError(
            "Delivery history must keep the manual and learning pack in sync; "
            f"missing: {', '.join(missing)}."
        )


def delivery_history_documents() -> tuple[str, ...]:
    """Return the documents that persist delivery history."""

    return DELIVERY_HISTORY_DOCUMENTS
