"""Corpus loader, cross-case validator, versioned manifest and agent-facing view."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from evals.e2e.models import E2ECase, Manifest, ManifestEntry

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CORPUS_DIR = PACKAGE_DIR / "corpus"
DEFAULT_MANIFEST_PATH = PACKAGE_DIR / "manifest.json"
DEFAULT_CORPUS_ID = "darkfac-e2e"
MAX_STEP_OVERLAP = 0.5


class CorpusError(ValueError):
    """The corpus (or its manifest) violates a structural rule."""


class AgentView(BaseModel):
    """The only part of a case ever handed to the evaluated system."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    brief: str


class Corpus:
    """An ordered, validated set of cases addressable by id."""

    def __init__(self, cases: Iterable[E2ECase], *, corpus_id: str = DEFAULT_CORPUS_ID, corpus_version: str = "1.0.0") -> None:
        self.cases: list[E2ECase] = sorted(cases, key=lambda case: case.case_id)
        self.corpus_id = corpus_id
        self.corpus_version = corpus_version
        self._by_id = {case.case_id: case for case in self.cases}

    def __len__(self) -> int:
        return len(self.cases)

    def get(self, case_id: str) -> E2ECase | None:
        return self._by_id.get(case_id)

    def build_manifest(self) -> Manifest:
        entries = [
            ManifestEntry(
                case_id=case.case_id,
                sha256=case.content_sha256(),
                visibility=case.visibility,
                product_kind=case.product_kind,
            )
            for case in self.cases
        ]
        return Manifest(
            corpus_id=self.corpus_id,
            corpus_version=self.corpus_version,
            entries=entries,
            manifest_sha256=Manifest.compute_hash(self.corpus_id, self.corpus_version, entries),
        )


def validate_cases(cases: list[E2ECase], *, require_distinct_kinds: bool = True) -> None:
    """Reject corpora whose cases are not independent of each other.

    Rules: unique case id, product, canary and title; one product kind per case
    (when ``require_distinct_kinds``); no shared seed file contents; and no two
    journeys overlapping on more than ``MAX_STEP_OVERLAP`` of the smaller one.
    """

    if not cases:
        raise CorpusError("corpus is empty")
    for label, values in (
        ("case_id", [c.case_id for c in cases]),
        ("product_id", [c.product_id for c in cases]),
        ("canary", [c.canary for c in cases]),
        ("title", [c.title.strip().lower() for c in cases]),
    ):
        if len(set(values)) != len(values):
            raise CorpusError(f"duplicate {label} in corpus")
    if require_distinct_kinds:
        kinds = [c.product_kind for c in cases]
        if len(set(kinds)) != len(kinds):
            raise CorpusError("cases must cover distinct product kinds")

    seed_owner: dict[str, str] = {}
    for case in cases:
        for content in case.seed_files.values():
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            owner = seed_owner.setdefault(digest, case.case_id)
            if owner != case.case_id:
                raise CorpusError(f"cases {owner} and {case.case_id} share seed file content")

    prints = {case.case_id: case.step_fingerprints() for case in cases}
    ids = sorted(prints)
    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            shared = prints[left] & prints[right]
            if not shared:
                continue
            overlap = len(shared) / min(len(prints[left]), len(prints[right]))
            if overlap > MAX_STEP_OVERLAP:
                raise CorpusError(f"cases {left} and {right} share their acceptance journey")


def load_corpus(
    corpus_dir: Path | str = DEFAULT_CORPUS_DIR,
    *,
    manifest_path: Path | str | None = DEFAULT_MANIFEST_PATH,
    require_distinct_kinds: bool = True,
) -> Corpus:
    """Load ``*.json`` cases, validate them and (optionally) verify the manifest."""

    directory = Path(corpus_dir)
    if not directory.is_dir():
        raise CorpusError(f"corpus directory not found: {directory.name}")
    cases: list[E2ECase] = []
    for path in sorted(directory.glob("*.json")):
        try:
            cases.append(E2ECase.model_validate_json(path.read_text(encoding="utf-8")))
        except (ValidationError, ValueError) as exc:
            # Report the file and the first error location only: never echo content.
            location = ""
            if isinstance(exc, ValidationError) and exc.errors():
                location = "/".join(str(part) for part in exc.errors()[0]["loc"])
            raise CorpusError(f"invalid case file {path.name} ({location or 'schema'})") from None
        if path.stem != cases[-1].case_id:
            raise CorpusError(f"case file name must match case_id: {path.name}")
    validate_cases(cases, require_distinct_kinds=require_distinct_kinds)

    manifest: Manifest | None = None
    if manifest_path is not None and Path(manifest_path).is_file():
        try:
            manifest = Manifest.model_validate_json(Path(manifest_path).read_text(encoding="utf-8"))
        except ValidationError as exc:
            raise CorpusError("invalid manifest") from exc
    corpus = Corpus(
        cases,
        corpus_id=manifest.corpus_id if manifest else DEFAULT_CORPUS_ID,
        corpus_version=manifest.corpus_version if manifest else "1.0.0",
    )
    if manifest is not None:
        problems = verify_manifest(corpus, manifest)
        if problems:
            raise CorpusError("corpus does not match manifest: " + "; ".join(problems))
    logger.debug("loaded %d e2e cases", len(corpus))
    return corpus


def verify_manifest(corpus: Corpus, manifest: Manifest) -> list[str]:
    """Return human-readable (content-free) problems; empty means consistent."""

    problems: list[str] = []
    expected = {entry.case_id: entry for entry in manifest.entries}
    for case in corpus.cases:
        entry = expected.pop(case.case_id, None)
        if entry is None:
            problems.append(f"{case.case_id} missing from manifest")
        elif entry.sha256 != case.content_sha256():
            problems.append(f"{case.case_id} hash drift")
        elif entry.visibility is not case.visibility:
            problems.append(f"{case.case_id} visibility drift")
    problems.extend(f"{case_id} in manifest but not in corpus" for case_id in sorted(expected))
    return problems


def write_manifest(corpus: Corpus, path: Path | str = DEFAULT_MANIFEST_PATH) -> Manifest:
    manifest = corpus.build_manifest()
    text = json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    Path(path).write_text(text, encoding="utf-8", newline="\n")
    return manifest


def agent_view(case: E2ECase) -> AgentView:
    """What the evaluated system may see: id and brief, never spec or oracle."""

    return AgentView(case_id=case.case_id, brief=case.brief)
