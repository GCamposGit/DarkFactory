"""Focused acceptance tests for the HF-01-02 contracts and collector."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from core.planning import (
    BaselineSourceKind,
    BaselineSourceSpec,
    SourceStatus,
    baseline_sources,
    collect_sources,
    load_catalog,
)

ROOT = Path(__file__).parents[1]
CATALOG_PATH = ROOT / "docs" / "handoffs" / "hf01-sources.json"


def _spec(
    relative_path: str,
    *,
    kind: BaselineSourceKind = BaselineSourceKind.SOURCE_FILE,
    source_id: str = "fixture-source",
    required: bool = True,
    section_heading: str | None = None,
    table_header: list[str] | None = None,
    item_id_column: str | None = None,
) -> BaselineSourceSpec:
    return BaselineSourceSpec(
        source_id=source_id,
        kind=kind,
        relative_path=relative_path,
        required=required,
        section_heading=section_heading,
        table_header=table_header,
        item_id_column=item_id_column,
    )


def test_catalog_loads_and_real_sources_are_read_without_verified_promotion() -> None:
    catalog = load_catalog(CATALOG_PATH)
    collected = collect_sources(ROOT, catalog)

    assert len(catalog) >= 50
    assert len(collected.observations) == len(catalog)
    assert all(observation.status in set(SourceStatus) for observation in collected.observations)
    assert all(claims == [] for claims in [collected.claims])
    assert any(item.item_id == "RM-01" for item in collected.planned_items)
    assert any(item.item_id == "INFRA-08" for item in collected.planned_items)
    assert all(observation.sha256 != "" for observation in collected.observations)


def test_json_and_markdown_declarations_are_extracted(tmp_path: Path) -> None:
    (tmp_path / "roadmap.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "id": "HF-01",
                        "title": "Baseline",
                        "delivery_status": "planned",
                        "dependencies": ["DF-20", {"item_id": "RM-09"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "plan.md").write_text(
        """# Plano\n\n| ID | Entrega | Depende | Horizonte |\n| --- | --- | --- | --- |\n| HF-02 | Runtime | HF-01 | Agora |\n""",
        encoding="utf-8",
    )

    collected = collect_sources(
        tmp_path,
        [
            _spec("roadmap.json", kind=BaselineSourceKind.JSON_ITEMS, source_id="json"),
            _spec(
                "plan.md",
                kind=BaselineSourceKind.MARKDOWN_TABLE,
                source_id="markdown",
                table_header=["ID", "Entrega", "Depende", "Horizonte"],
                item_id_column="ID",
            ),
        ],
    )

    assert [(item.item_id, item.dependencies) for item in collected.planned_items] == [
        ("HF-01", ["DF-20", "RM-09"]),
        ("HF-02", ["HF-01"]),
    ]
    assert {item.declared_status for item in collected.planned_items} == {"planned", "Agora"}


def test_invalid_json_schema_and_unreadable_source_have_distinct_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "corrupt.json").write_text("{not-json", encoding="utf-8")
    (tmp_path / "schema.json").write_text(json.dumps({"records": []}), encoding="utf-8")
    (tmp_path / "unreadable.txt").write_text("private", encoding="utf-8")

    original_read = baseline_sources._read_bounded

    def read_with_denied_access(path: Path) -> bytes:
        if path.name == "unreadable.txt":
            raise baseline_sources._ReadFailure(SourceStatus.ACCESS_DENIED, "SOURCE_ACCESS_DENIED")
        return original_read(path)

    monkeypatch.setattr(baseline_sources, "_read_bounded", read_with_denied_access)
    collected = collect_sources(
        tmp_path,
        [
            _spec("corrupt.json", kind=BaselineSourceKind.JSON_ITEMS, source_id="corrupt"),
            _spec("schema.json", kind=BaselineSourceKind.JSON_ITEMS, source_id="schema"),
            _spec("unreadable.txt", source_id="unreadable"),
        ],
    )

    errors = {observation.source_id: observation.error_code for observation in collected.observations}
    statuses = {observation.source_id: observation.status for observation in collected.observations}
    assert errors == {
        "corrupt": "SOURCE_INVALID_JSON",
        "schema": "SOURCE_SCHEMA_CHANGED",
        "unreadable": "SOURCE_ACCESS_DENIED",
    }
    assert statuses["corrupt"] == SourceStatus.INVALID
    assert statuses["schema"] == SourceStatus.INVALID
    assert statuses["unreadable"] == SourceStatus.ACCESS_DENIED


def test_safe_paths_reject_parent_escape_and_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-baseline-source.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(ValidationError):
        _spec("../outside-baseline-source.txt")

    link = tmp_path / "linked.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this Windows host")

    collected = collect_sources(tmp_path, [_spec("linked.txt")])
    observation = collected.observations[0]
    assert observation.status == SourceStatus.ACCESS_DENIED
    assert observation.error_code == "SOURCE_OUTSIDE_ROOT"
    assert not (tmp_path / "outside-baseline-source.txt").exists()


def test_file_over_two_mib_is_rejected_without_modifying_the_source(tmp_path: Path) -> None:
    source = tmp_path / "large.txt"
    source.write_bytes(b"x" * (baseline_sources.MAX_SOURCE_BYTES + 1))
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    collected = collect_sources(tmp_path, [_spec("large.txt")])

    observation = collected.observations[0]
    assert observation.status == SourceStatus.INVALID
    assert observation.error_code == "SOURCE_TOO_LARGE"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_changed_source_is_marked_unstable_after_one_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "changing.txt"
    source.write_text("before", encoding="utf-8")
    reads = [b"before", b"after", b"after-again", b"should-not-be-read"]
    calls = 0

    def changing_read(path: Path) -> bytes:
        nonlocal calls
        calls += 1
        return reads.pop(0)

    monkeypatch.setattr(baseline_sources, "_read_bounded", changing_read)
    collected = collect_sources(tmp_path, [_spec("changing.txt")])

    observation = collected.observations[0]
    assert observation.status == SourceStatus.UNSTABLE
    assert observation.error_code == "SOURCE_UNSTABLE"
    assert calls == 3


def test_contract_forbids_extra_fields_and_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        BaselineSourceSpec(
            source_id="source",
            kind=BaselineSourceKind.SOURCE_FILE,
            relative_path="file.txt",
            required=True,
            unexpected=True,
        )

    with pytest.raises(ValidationError):
        baseline_sources.SourceObservation(
            source_id="source",
            relative_path=Path("file.txt"),
            status=SourceStatus.READ,
            observed_at=datetime.fromisoformat("2026-09-08T00:00:00"),
        )
