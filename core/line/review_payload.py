"""Bounded, verifiable review context (USR-474 T1).

Reads ``SPEC.md``, the acceptance criteria in ``tickets.json`` and the bytes
of the changed files. No network and no agent. Changed files are emitted in
sorted repo-relative order, so the manifest does not depend on the caller's
order. Each manifest item records the repo-relative path, the sha256 of the
source bytes, how many of those bytes were included after redaction, and
whether the included text was cut.

Redaction runs on the full decoded text before the cut, so a secret that
straddles the limit is not left half visible. Item and total limits count
UTF-8 bytes and stop on a character boundary. ``truncated`` means the redacted
text did not fit in the budget. It does not mean redaction shortened the text.

A missing, empty, invalid or unreadable SPEC or ticket file yields
``complete=False``. A SPEC whose included text is blank after the cut is not
ready either. An empty context is never reported as ready.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel, ConfigDict, model_validator

from core.line.agent_cli import redact_secrets

logger = logging.getLogger(__name__)

SPEC_NAME = "SPEC.md"
TICKETS_NAME = "tickets.json"
CONTEXT_SPEC_PATH = "context/SPEC.md"
CONTEXT_TICKETS_PATH = "context/tickets.json"

DEFAULT_ITEM_BUDGET_BYTES = 32_768
DEFAULT_TOTAL_BUDGET_BYTES = 131_072

REASON_SPEC_MISSING = "spec_missing"
REASON_SPEC_UNREADABLE = "spec_unreadable"
REASON_SPEC_EMPTY = "spec_empty"
REASON_SPEC_NOT_INCLUDED = "spec_not_included"
REASON_TICKETS_MISSING = "tickets_missing"
REASON_TICKETS_UNREADABLE = "tickets_unreadable"
REASON_TICKETS_EMPTY = "tickets_empty"
REASON_TICKETS_NOT_INCLUDED = "tickets_not_included"
REASON_CHANGED_UNREADABLE = "changed_file_unreadable"
REASON_CHANGED_OUTSIDE = "changed_file_outside_root"

_CONTEXT_BLOCKERS = frozenset({
    REASON_SPEC_MISSING,
    REASON_SPEC_UNREADABLE,
    REASON_SPEC_EMPTY,
    REASON_SPEC_NOT_INCLUDED,
    REASON_TICKETS_MISSING,
    REASON_TICKETS_UNREADABLE,
    REASON_TICKETS_EMPTY,
    REASON_TICKETS_NOT_INCLUDED,
})

_CRITERIA_KEYS = ("acceptance", "acceptance_criteria", "criteria")
_HEX = frozenset("0123456789abcdef")


class ReviewManifestItem(BaseModel):
    """One source the payload claims to have read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str
    bytes_included: int
    truncated: bool


class ReviewFileSlice(BaseModel):
    """Redacted text actually carried for one manifest path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    text: str


class ReviewPayload(BaseModel):
    """SPEC text, ticket criteria, changed-file slices and a checkable manifest.

    ``slices`` and ``manifest`` share the same paths in the same order.
    ``bytes_included`` is the UTF-8 size of that slice. Criteria are the
    redacted acceptance strings taken from the whole ``tickets.json``, even
    when the JSON slice itself was cut. They are not added again to
    ``bytes_included``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    complete: bool
    reasons: tuple[str, ...] = ()
    criteria: tuple[str, ...] = ()
    slices: tuple[ReviewFileSlice, ...] = ()
    manifest: tuple[ReviewManifestItem, ...] = ()

    @property
    def spec_text(self) -> str:
        for item in self.slices:
            if item.path == CONTEXT_SPEC_PATH:
                return item.text
        return ""

    @model_validator(mode="after")
    def _consistent(self) -> "ReviewPayload":
        if self.complete and self.reasons:
            raise ValueError("a complete review payload cannot list reasons")
        if not self.complete and not self.reasons:
            raise ValueError("an incomplete review payload needs a reason")
        if len(self.slices) != len(self.manifest):
            raise ValueError("manifest and slices differ in length")
        for sl, item in zip(self.slices, self.manifest):
            if sl.path != item.path or not item.path or "\\" in item.path:
                raise ValueError("manifest path does not match the slice")
            if Path(item.path).is_absolute() or ".." in Path(item.path).parts:
                raise ValueError("manifest path must stay inside the tree")
            if item.bytes_included != len(sl.text.encode("utf-8")) or item.bytes_included < 0:
                raise ValueError("bytes_included does not match the slice text")
            if len(item.sha256) != 64 or any(char not in _HEX for char in item.sha256):
                raise ValueError("sha256 must be 64 lowercase hex characters")
        return self


class _Budget:
    def __init__(self, per_item: int, total: int) -> None:
        self.per_item = per_item
        self.remaining = total

    def clip(self, text: str) -> tuple[str, int, bool]:
        allowance = min(self.per_item, self.remaining)
        included, truncated = _cut_utf8(text, allowance)
        size = len(included.encode("utf-8"))
        self.remaining -= size
        return included, size, truncated


def _cut_utf8(text: str, budget: int) -> tuple[str, bool]:
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text, False
    if budget <= 0:
        return "", True
    clipped = raw[:budget]
    while clipped:
        try:
            return clipped.decode("utf-8"), True
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return "", True


def _decode_utf8(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _reason_path(raw: str) -> str:
    cleaned = raw.replace("\\", "/").replace("\n", "").replace("\r", "").strip()
    return cleaned[:200]


def _read_source(path: Path) -> tuple[bytes | None, str | None]:
    """Return ``(bytes, None)`` or ``(None, "missing"|"unreadable")``."""
    if not path.is_file():
        return None, "missing"
    try:
        return _read_bytes(path), None
    except OSError as exc:
        logger.warning("review payload could not read %s (%s)", path.name, type(exc).__name__)
        return None, "unreadable"


def _slice_raw(path: str, raw: bytes, budget: _Budget) -> tuple[ReviewFileSlice, ReviewManifestItem] | None:
    try:
        decoded = _decode_utf8(raw)
    except UnicodeDecodeError:
        return None
    included, size, truncated = budget.clip(redact_secrets(decoded))
    sl = ReviewFileSlice(path=path, text=included)
    item = ReviewManifestItem(
        path=path, sha256=_sha256(raw), bytes_included=size, truncated=truncated,
    )
    return sl, item


def _string_list(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    found: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        stripped = item.strip()
        if stripped:
            found.append(stripped)
    return found


def _criteria_from_mapping(obj: dict[str, object]) -> list[str] | None:
    found: list[str] = []
    for key in _CRITERIA_KEYS:
        if key not in obj:
            continue
        parsed = _string_list(obj[key])
        if parsed is None:
            return None
        found.extend(parsed)
    return found


def _parse_criteria(decoded: str) -> tuple[list[str], str | None]:
    try:
        data = json.loads(decoded)
    except json.JSONDecodeError:
        return [], REASON_TICKETS_UNREADABLE
    extra: list[str] = []
    if isinstance(data, dict):
        tickets = data.get("tickets", [])
        if not isinstance(tickets, list):
            return [], REASON_TICKETS_UNREADABLE
        top = {key: data[key] for key in _CRITERIA_KEYS if key in data}
        parsed_top = _criteria_from_mapping(top)
        if parsed_top is None:
            return [], REASON_TICKETS_UNREADABLE
        extra = parsed_top
        body: object = tickets
    elif isinstance(data, list):
        body = data
    else:
        return [], REASON_TICKETS_UNREADABLE
    found: list[str] = []
    if not isinstance(body, list):
        return [], REASON_TICKETS_UNREADABLE
    for item in body:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                found.append(stripped)
            continue
        if isinstance(item, dict):
            parsed = _criteria_from_mapping(item)
            if parsed is None:
                return [], REASON_TICKETS_UNREADABLE
            found.extend(parsed)
            continue
        return [], REASON_TICKETS_UNREADABLE
    found.extend(extra)
    if not found:
        return [], REASON_TICKETS_EMPTY
    return [redact_secrets(item) for item in found], None


def _add(
    slices: list[ReviewFileSlice],
    manifest: list[ReviewManifestItem],
    piece: tuple[ReviewFileSlice, ReviewManifestItem] | None,
) -> bool:
    if piece is None:
        return False
    sl, item = piece
    slices.append(sl)
    manifest.append(item)
    return True


def _load_spec(
    context_dir: Path,
    budget: _Budget,
    reasons: list[str],
    slices: list[ReviewFileSlice],
    manifest: list[ReviewManifestItem],
) -> None:
    raw, problem = _read_source(context_dir / SPEC_NAME)
    if problem == "missing":
        reasons.append(REASON_SPEC_MISSING)
        return
    if problem == "unreadable" or raw is None:
        reasons.append(REASON_SPEC_UNREADABLE)
        return
    try:
        decoded = _decode_utf8(raw)
    except UnicodeDecodeError:
        reasons.append(REASON_SPEC_UNREADABLE)
        return
    blank_source = not decoded.strip()
    if blank_source:
        reasons.append(REASON_SPEC_EMPTY)
    piece = _slice_raw(CONTEXT_SPEC_PATH, raw, budget)
    if not _add(slices, manifest, piece):
        reasons.append(REASON_SPEC_UNREADABLE)
        return
    if piece is not None and not piece[0].text.strip() and not blank_source:
        reasons.append(REASON_SPEC_NOT_INCLUDED)


def _load_tickets(
    context_dir: Path,
    budget: _Budget,
    reasons: list[str],
    slices: list[ReviewFileSlice],
    manifest: list[ReviewManifestItem],
) -> tuple[str, ...]:
    raw, problem = _read_source(context_dir / TICKETS_NAME)
    if problem == "missing":
        reasons.append(REASON_TICKETS_MISSING)
        return ()
    if problem == "unreadable" or raw is None:
        reasons.append(REASON_TICKETS_UNREADABLE)
        return ()
    try:
        decoded = _decode_utf8(raw)
    except UnicodeDecodeError:
        reasons.append(REASON_TICKETS_UNREADABLE)
        return ()
    criteria, parse_problem = _parse_criteria(decoded)
    if parse_problem:
        reasons.append(parse_problem)
        criteria = []
    ticket_piece = _slice_raw(CONTEXT_TICKETS_PATH, raw, budget)
    if not _add(slices, manifest, ticket_piece):
        if REASON_TICKETS_UNREADABLE not in reasons:
            reasons.append(REASON_TICKETS_UNREADABLE)
        return ()
    if (
        ticket_piece is None
        or not ticket_piece[0].text.strip()
        or ticket_piece[1].truncated
    ):
        reasons.append(REASON_TICKETS_NOT_INCLUDED)

    included_criteria: list[str] = []
    for criterion in criteria:
        included, _size, truncated = budget.clip(redact_secrets(criterion))
        if not included.strip() or truncated:
            reasons.append(REASON_TICKETS_NOT_INCLUDED)
            continue
        included_criteria.append(included)
    if criteria and len(included_criteria) != len(criteria):
        # No partial acceptance list can silently stand in for the ticket's criteria.
        included_criteria = []
    if not included_criteria and REASON_TICKETS_NOT_INCLUDED not in reasons:
        reasons.append(REASON_TICKETS_NOT_INCLUDED)
    return tuple(included_criteria)


def _locate(root: Path, raw_path: str) -> tuple[Path | None, str]:
    cleaned = _reason_path(raw_path)
    if not cleaned or cleaned.startswith("/") or ":" in cleaned:
        return None, cleaned
    # Reject traversal syntax even when normalization would happen to land back
    # inside the tree. Review inputs are repo-relative paths, never path programs.
    if ".." in cleaned.split("/"):
        return None, cleaned
    try:
        resolved = (root / cleaned).resolve()
        display = resolved.relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return None, cleaned
    if not display or display == ".":
        return None, cleaned
    return resolved, display


def _load_changed(
    root: Path,
    changed_paths: Sequence[str],
    budget: _Budget,
    reasons: list[str],
    slices: list[ReviewFileSlice],
    manifest: list[ReviewManifestItem],
) -> None:
    files: dict[str, Path] = {}
    outside: set[str] = set()
    for raw_path in changed_paths:
        located, display = _locate(root, raw_path)
        if located is None:
            outside.add(display or _reason_path(raw_path) or "(vazio)")
            continue
        files.setdefault(display, located)
    for display in sorted(files):
        raw, problem = _read_source(files[display])
        if problem is not None or raw is None:
            reasons.append(f"{REASON_CHANGED_UNREADABLE}:{display}")
            continue
        if not _add(slices, manifest, _slice_raw(display, raw, budget)):
            reasons.append(f"{REASON_CHANGED_UNREADABLE}:{display}")
    for label in sorted(outside):
        reasons.append(f"{REASON_CHANGED_OUTSIDE}:{label}")


def build_review_payload(
    root: Path,
    context_dir: Path,
    changed_paths: Sequence[str],
    *,
    item_budget_bytes: int = DEFAULT_ITEM_BUDGET_BYTES,
    total_budget_bytes: int = DEFAULT_TOTAL_BUDGET_BYTES,
) -> ReviewPayload:
    """Read the review sources and return a bounded payload or an incomplete one.

    ``root`` is the tree of the changed files. ``context_dir`` holds ``SPEC.md``
    and ``tickets.json`` and may live outside that tree. Paths that are
    absolute or resolve outside ``root`` are not read. Changed-file slices and
    their reasons follow sorted path order, independent of ``changed_paths``.
    """
    if item_budget_bytes < 0 or total_budget_bytes < 0:
        raise ValueError("review payload budget must be zero or positive")
    budget = _Budget(item_budget_bytes, total_budget_bytes)
    reasons: list[str] = []
    slices: list[ReviewFileSlice] = []
    manifest: list[ReviewManifestItem] = []
    _load_spec(context_dir, budget, reasons, slices, manifest)
    criteria = _load_tickets(context_dir, budget, reasons, slices, manifest)
    _load_changed(root, changed_paths, budget, reasons, slices, manifest)
    return ReviewPayload(
        complete=not reasons,
        reasons=tuple(reasons),
        criteria=criteria,
        slices=tuple(slices),
        manifest=tuple(manifest),
    )


def included_text(payload: ReviewPayload) -> str:
    """Slice text in manifest order, then the redacted criteria."""
    parts = [item.text for item in payload.slices]
    parts.extend(payload.criteria)
    return "\n".join(parts)


def context_blocks_review(payload: ReviewPayload) -> bool:
    """True when SPEC or tickets are missing, empty or unreadable.

    A changed file that could not be read does not, by itself, hide the SPEC.
    A SPEC that was cut down to blank text does.
    """
    return any(reason in _CONTEXT_BLOCKERS for reason in payload.reasons)


def render_paid_context(payload: ReviewPayload) -> str:
    """Sanitized SPEC, criteria, file slices and manifest hashes for a transport.

    The SPEC slice is written once, under its manifest path. Criteria are listed
    after the manifest and are not copied back into the SPEC section.
    """
    lines = ["", "## Contexto verificavel da revisao", "", "### Manifesto"]
    for item in payload.manifest:
        lines.append(
            f"- path: {item.path} sha256: {item.sha256} "
            f"bytes_included: {item.bytes_included} truncated: {str(item.truncated).lower()}"
        )
    lines.append("")
    lines.append("### Criterios")
    for criterion in payload.criteria:
        lines.append(f"- {criterion}")
    for sl in payload.slices:
        lines.append("")
        lines.append(f"### {sl.path}")
        lines.append(sl.text)
    lines.append("")
    return "\n".join(lines)
