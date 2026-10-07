"""Anti-leak guard for reserved E2E cases.

The scanner never returns matched text: findings carry only ``case_id``, the
case hash prefix and the detection kind, so a finding can itself be printed
without leaking.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evals.e2e.models import (
    ChatStep,
    CliStep,
    E2ECase,
    HttpStep,
    Visibility,
    iter_strings,
)

NGRAM_SIZE = 8
MIN_LITERAL_CHARS = 12
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class LeakKind(str, Enum):
    CANARY = "canary"
    SPEC_NGRAM = "spec_ngram"
    ORACLE_LITERAL = "oracle_literal"


class LeakFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    case_sha256_prefix: str = Field(min_length=12, max_length=12)
    kind: LeakKind
    location: str = ""


class LeakError(RuntimeError):
    """Raised when an artifact that must be clean contains reserved content."""

    def __init__(self, findings: list[LeakFinding]) -> None:
        self.findings = findings
        summary = ", ".join(sorted({f"{f.case_id}:{f.kind.value}" for f in findings}))
        super().__init__(f"reserved case content detected ({summary})")


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _ngrams(text: str) -> set[tuple[str, ...]]:
    tokens = _TOKEN_RE.findall(text.lower())
    if len(tokens) < NGRAM_SIZE:
        return set()
    return {tuple(tokens[i : i + NGRAM_SIZE]) for i in range(len(tokens) - NGRAM_SIZE + 1)}


def _oracle_literals(case: E2ECase) -> set[str]:
    values: list[str] = []
    for step in case.journey:
        values.extend(step.expect.contains)
        values.extend(step.expect.error_contains)
        values.extend(step.expect.regex)
        if isinstance(step, CliStep):
            values.append(step.stdin)
        elif isinstance(step, ChatStep):
            values.append(step.message)
        elif isinstance(step, HttpStep) and step.json_body:
            values.extend(iter_strings(step.json_body))
    for content in case.seed_files.values():
        values.extend(line for line in content.splitlines())
    brief = _normalize(case.brief)
    result: set[str] = set()
    for value in values:
        norm = _normalize(value)
        # Only long, distinctive literals count; public brief text never does.
        if len(norm) >= MIN_LITERAL_CHARS and norm not in brief:
            result.add(norm)
    return result


class _Fingerprint:
    def __init__(self, case: E2ECase) -> None:
        self.case_id = case.case_id
        self.prefix = case.content_sha256()[:12]
        self.canary = case.canary.lower()
        brief_grams = _ngrams(case.brief)
        self.spec_grams = _ngrams(case.spec) - brief_grams
        self.literals = _oracle_literals(case)


class LeakScanner:
    """Detects reserved spec/oracle content in arbitrary text or JSON structures."""

    def __init__(self, cases: Iterable[E2ECase], *, only_reserved: bool = True) -> None:
        self._prints = [
            _Fingerprint(case)
            for case in cases
            if not only_reserved or case.visibility is Visibility.RESERVED
        ]

    def scan_text(self, text: str, *, location: str = "") -> list[LeakFinding]:
        norm = _normalize(text)
        grams: set[tuple[str, ...]] | None = None
        findings: list[LeakFinding] = []
        for fp in self._prints:
            kinds: list[LeakKind] = []
            if fp.canary in norm:
                kinds.append(LeakKind.CANARY)
            if any(literal in norm for literal in fp.literals):
                kinds.append(LeakKind.ORACLE_LITERAL)
            if fp.spec_grams:
                if grams is None:
                    grams = _ngrams(text)
                if grams & fp.spec_grams:
                    kinds.append(LeakKind.SPEC_NGRAM)
            findings.extend(
                LeakFinding(case_id=fp.case_id, case_sha256_prefix=fp.prefix, kind=kind, location=location)
                for kind in kinds
            )
        return findings

    def scan_value(self, value: Any, *, location: str = "") -> list[LeakFinding]:
        """Scan every string leaf of a JSON-like structure (keys excluded)."""

        seen: set[tuple[str, LeakKind]] = set()
        result: list[LeakFinding] = []
        for text in iter_strings(value):
            for finding in self.scan_text(text, location=location):
                key = (finding.case_id, finding.kind)
                if key not in seen:
                    seen.add(key)
                    result.append(finding)
        return result

    def assert_clean(self, value: Any, *, location: str = "") -> None:
        findings = self.scan_value(value, location=location)
        if findings:
            raise LeakError(findings)
