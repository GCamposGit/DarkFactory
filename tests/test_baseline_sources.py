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


def test_cr11_positive_control_df11_in_hf_table_and_no_ghost_cycle(tmp_path: Path) -> None:
    # 1. Direct parser check: DF-11 in HF table must yield only DF-11
    parsed = baseline_sources._parse_markdown_dependencies("DF-11", "HF")
    assert parsed == ["DF-11"]

    # 2. Ingestion of table where HF-11 depends on DF-11
    plan_file = tmp_path / "plan.md"
    plan_file.write_text(
        "# Plano\n\n| ID | Entrega | Depende | Horizonte |\n| --- | --- | --- | --- |\n| HF-11 | Spec | DF-11 | Agora |\n",
        encoding="utf-8",
    )
    collected = collect_sources(
        tmp_path,
        [
            _spec(
                "plan.md",
                kind=BaselineSourceKind.MARKDOWN_TABLE,
                source_id="plan-markdown",
                table_header=["ID", "Entrega", "Depende", "Horizonte"],
                item_id_column="ID",
            )
        ],
    )
    item = collected.planned_items[0]
    assert item.item_id == "HF-11"
    assert item.dependencies == ["DF-11"]


def test_cr11_real_catalog_hf12_drops_ghost_dependencies() -> None:
    # In HF-12, 'HF-03, HF-11, INFRA-08, INFRA-09' must not generate 'HF-08' or 'HF-09'
    raw = "HF-03, HF-11, INFRA-08, INFRA-09"
    parsed = baseline_sources._parse_markdown_dependencies(raw, "HF")
    assert parsed == ["HF-03", "HF-11", "INFRA-08", "INFRA-09"]


def test_cr11_grammar_prefix_inheritance_and_bare_tokens() -> None:
    # HF-01, 02 in HF table -> HF-01 and HF-02
    assert baseline_sources._parse_markdown_dependencies("HF-01, 02", "HF") == ["HF-01", "HF-02"]
    # HF-01, 02 in DF table -> HF-01 and HF-02 (inherits HF from preceding token)
    assert baseline_sources._parse_markdown_dependencies("HF-01, 02", "DF") == ["HF-01", "HF-02"]
    # 02,03 in DF table -> DF-02, DF-03
    assert baseline_sources._parse_markdown_dependencies("02,03", "DF") == ["DF-02", "DF-03"]
    # 03, 11–14 in DF table -> DF-03, DF-11, DF-12, DF-13, DF-14
    assert baseline_sources._parse_markdown_dependencies("03, 11–14", "DF") == [
        "DF-03",
        "DF-11",
        "DF-12",
        "DF-13",
        "DF-14",
    ]
    # Single bare ID
    assert baseline_sources._parse_markdown_dependencies("13", "DF") == ["DF-13"]
    # Slash separated
    assert baseline_sources._parse_markdown_dependencies("DF-08/13", "DF") == ["DF-08", "DF-13"]
    # Conjunction 'e'
    assert baseline_sources._parse_markdown_dependencies("DF-11 e 12", "HF") == ["DF-11", "DF-12"]
    assert baseline_sources._parse_markdown_dependencies("DF-11 e HF-02", "HF") == ["DF-11", "HF-02"]
    # Semicolon separated
    assert baseline_sources._parse_markdown_dependencies("DF-11; 12; 13", "HF") == ["DF-11", "DF-12", "DF-13"]


def test_cr11_mixed_prefixes_and_ranges() -> None:
    # Mixed prefixes: HF-01, 02, DF-05, 06 in RM table
    assert baseline_sources._parse_markdown_dependencies("HF-01, 02, DF-05, 06", "RM") == [
        "HF-01",
        "HF-02",
        "DF-05",
        "DF-06",
    ]
    # Prefixed range
    assert baseline_sources._parse_markdown_dependencies("DF-01–03", "HF") == ["DF-01", "DF-02", "DF-03"]
    # Suffix in ID
    assert baseline_sources._parse_markdown_dependencies("INFRA-06B", "INFRA") == ["INFRA-06B"]


def test_cr11_repeated_tokens_deduplicated() -> None:
    assert baseline_sources._parse_markdown_dependencies("HF-01, HF-01", "HF") == ["HF-01"]
    assert baseline_sources._parse_markdown_dependencies("02, 02", "HF") == ["HF-02"]


def test_cr11_unknown_prefixes_and_prose_do_not_invent_links() -> None:
    # Unknown prefix alone
    assert baseline_sources._parse_markdown_dependencies("UNKNOWN-11", "HF") == []
    # Mixed with unknown prefix
    assert baseline_sources._parse_markdown_dependencies("DF-11, UNKNOWN-05, HF-02", "HF") == ["DF-11", "HF-02"]
    # Common technical terms with hyphens
    assert baseline_sources._parse_markdown_dependencies("UTF-8 e RFC-2119", "HF") == []
    assert baseline_sources._parse_markdown_dependencies("ISO-9001, SHA-256", "DF") == []
    # Prose with numbers
    assert baseline_sources._parse_markdown_dependencies("Python 3.12, porta 8080, ver nota 42", "HF") == []
    # Prose annotation after valid item
    assert baseline_sources._parse_markdown_dependencies("DF-11 (ver nota 1 e revisão 2)", "HF") == ["DF-11"]
    # Non-structural text
    assert baseline_sources._parse_markdown_dependencies("Nenhuma dependência identificada", "HF") == []
    # Empty and dashes
    assert baseline_sources._parse_markdown_dependencies("—", "HF") == []
    assert baseline_sources._parse_markdown_dependencies("-", "HF") == []
    assert baseline_sources._parse_markdown_dependencies("", "HF") == []

