"""Contract tests for the curated HF-01-01 baseline data files."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[1]
CATALOG_PATH = ROOT / "docs" / "handoffs" / "hf01-sources.json"
CLAIMS_PATH = ROOT / "docs" / "handoffs" / "hf01-claims.json"
FIXTURES_PATH = ROOT / "tests" / "fixtures" / "baseline_cases.json"

SOURCE_KEYS = {
    "source_id",
    "kind",
    "relative_path",
    "required",
    "section_heading",
    "table_header",
    "item_id_column",
}
CLAIM_KEYS = {
    "claim_id",
    "item_id",
    "dimension",
    "assertion",
    "evidence_kind",
    "source_id",
    "locator",
    "source_hash",
    "candidate_sha",
    "observed_at",
    "scope",
    "summary",
}
ALLOWED_SOURCE_KINDS = {
    "json_items",
    "markdown_table",
    "report",
    "inventory",
    "adr",
    "owner_statement",
    "source_file",
}
ALLOWED_DIMENSIONS = {"implementation", "integration", "operation"}
ALLOWED_ASSERTIONS = {"positive", "negative", "partial"}
ALLOWED_EVIDENCE_KINDS = {"document", "owner_statement", "remote_git", "service_probe"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _path_from_locator(locator: str) -> Path:
    return Path(locator.split("#", 1)[0])


def _assert_safe_relative_path(relative_path: str) -> Path:
    path = Path(relative_path)
    assert not path.is_absolute(), relative_path
    assert ".." not in path.parts, relative_path
    resolved = (ROOT / path).resolve()
    assert resolved.is_relative_to(ROOT.resolve()), relative_path
    return resolved


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_catalog_is_explicit_safe_and_resolves_to_captured_sources() -> None:
    catalog = _read_json(CATALOG_PATH)

    assert isinstance(catalog, list)
    assert catalog
    assert len({entry["source_id"] for entry in catalog}) == len(catalog)

    for entry in catalog:
        assert set(entry) == SOURCE_KEYS
        assert entry["kind"] in ALLOWED_SOURCE_KINDS
        assert isinstance(entry["required"], bool)
        assert isinstance(entry["source_id"], str) and entry["source_id"]
        assert isinstance(entry["relative_path"], str)
        source_path = _assert_safe_relative_path(entry["relative_path"])
        assert source_path.is_file(), entry["relative_path"]
        assert not any(
            key in entry for key in {"command", "commands", "executable", "shell", "script"}
        )
        if entry["kind"] == "markdown_table":
            assert entry["table_header"]
            assert entry["item_id_column"]
        else:
            assert entry["table_header"] is None

    catalog_paths = {entry["relative_path"] for entry in catalog}
    required_paths = {
        "MISSION.md",
        "AGENTS.md",
        "FACTORY_RULES.md",
        "docs/ROADMAP_OPERACIONAL.md",
        "docs/DEVELOPMENT_PLAN_2026-09-05.md",
        "docs/HYBRID_WORKFLOW_PLAN_2026-09-08.md",
        ".factory/roadmap/darkfac.json",
        ".factory/infra/roadmap.json",
        ".factory/infra/roadmap.md",
        ".factory/infra/inventory.json",
        "docs/RUNTIME_DECISION.md",
        "docs/AUTONOMY_POLICY.md",
        "core/roadmap/sources.py",
        "core/orchestrator/runtime.py",
        "core/orchestrator/store.py",
        "core/harness/remote_worker.py",
        "core/harness/test_subagent.py",
        "hub/backend/api.py",
        "hub/backend/service.py",
        "hub/frontend/tasks.js",
        ".factory/infra/decisions/ADR-001-hybrid-multi-project-topology.md",
        ".factory/infra/decisions/ADR-002-vps-and-paas-orchestration.md",
        ".factory/infra/decisions/ADR-003-database-and-backup-strategy.md",
        ".factory/infra/decisions/ADR-004-secure-networking-and-edge.md",
    }
    required_paths.update(
        path.relative_to(ROOT).as_posix()
        for path in ROOT.joinpath(".factory", "reports").glob("df-*-report.md")
    )
    required_paths.update(
        path.relative_to(ROOT).as_posix()
        for path in ROOT.joinpath(".factory", "reports").glob("rm-*-report.md")
    )
    required_paths.update(
        {
            ".factory/reports/roadmap-operacional-report.md",
            ".factory/reports/roadmap-list-sort-report.md",
            ".factory/reports/usr-16-report.md",
        }
    )
    assert required_paths <= catalog_paths
    assert not any("canaletto" in path.lower() for path in catalog_paths)


def test_curated_claims_are_separated_hashed_and_grounded_in_catalog_sources() -> None:
    catalog = _read_json(CATALOG_PATH)
    catalog_by_id = {entry["source_id"]: entry for entry in catalog}
    document = _read_json(CLAIMS_PATH)
    claims = document["claims"]

    assert document["schema_version"] == "1"
    assert document["ticket_id"] == "HF-01-01"
    assert claims
    assert len({claim["claim_id"] for claim in claims}) == len(claims)
    assert {claim["dimension"] for claim in claims} == ALLOWED_DIMENSIONS

    for claim in claims:
        assert set(claim) == CLAIM_KEYS
        assert claim["dimension"] in ALLOWED_DIMENSIONS
        assert claim["assertion"] in ALLOWED_ASSERTIONS
        assert claim["evidence_kind"] in ALLOWED_EVIDENCE_KINDS
        assert claim["source_id"] in catalog_by_id
        source_path = _assert_safe_relative_path(catalog_by_id[claim["source_id"]]["relative_path"])
        locator_path = _assert_safe_relative_path(_path_from_locator(claim["locator"]).as_posix())
        assert locator_path == source_path
        assert claim["source_hash"] == _sha256(source_path)
        assert claim["candidate_sha"] is None or re.fullmatch(r"[0-9a-f]{40}", claim["candidate_sha"])
        assert len(claim["summary"]) <= 400
        assert not re.search(r"(?:sk|ghp|github_pat|password|secret|token)[_-]?[A-Za-z0-9]{12,}", claim["summary"], re.I)
        assert "canaletto" not in json.dumps(claim, ensure_ascii=False).lower()

    target_items = {claim["item_id"] for claim in claims}
    assert {"DF-11", "DF-20", "DF-21", "DF-23", "INFRA-08", "INFRA-09", "INFRA-10", "RM-07", "RM-09"} <= target_items
    assert any(claim["item_id"] == "RM-07" and claim["assertion"] == "negative" for claim in claims)
    assert any(claim["item_id"] == "RM-07" and claim["assertion"] == "positive" for claim in claims)
    assert any(claim["item_id"] == "INFRA-10" and claim["source_id"] == "usr-16-report" for claim in claims)
    assert any(claim["item_id"] == "n8n" and claim["dimension"] == "operation" for claim in claims)
    assert any(claim["item_id"] == "PostgreSQL" and claim["evidence_kind"] == "owner_statement" for claim in claims)


def test_acceptance_fixtures_cover_a1_to_a10_without_commands_or_secrets() -> None:
    fixtures = _read_json(FIXTURES_PATH)

    assert fixtures["schema_version"] == "1"
    cases = fixtures["cases"]
    assert {case["case_id"] for case in cases} == {f"A{number}" for number in range(1, 11)}
    assert len(FIXTURES_PATH.read_bytes()) < 64 * 1024
    serialized = json.dumps(fixtures, ensure_ascii=False).lower()
    assert "canaletto" not in serialized
    assert not any(key in serialized for key in ('"command"', '"commands"', '"shell"', '"secret"'))

    expected = {case["case_id"]: case["expected"] for case in cases}
    assert expected["A1"]["assessments"]["implementation"] == "reported"
    assert expected["A1"]["assessments"]["integration"] == "unknown"
    assert expected["A1"]["assessments"]["operation"] == "unknown"
    assert expected["A2"]["assessments"]["implementation"] == "partial"
    assert "partial_ticket_coverage" in expected["A2"]["issue_codes"]
    assert expected["A3"]["item_count"] == 1
    assert "source_conflict" in expected["A3"]["issue_codes"]
    assert expected["A4"]["assessments"]["operation"] == "unknown"
    assert "service_endpoint_missing" in expected["A4"]["issue_codes"]
    assert expected["A5"]["assessments"]["operation"] == "reported"
    assert "access_required" in expected["A5"]["issue_codes"]
    assert expected["A6"]["assessments"]["implementation"] == "reported"
    assert "service_endpoint_missing" in expected["A6"]["issue_codes"]
    assert expected["A7"]["completion_claims_for_mentioned_item"] == 0
    assert expected["A8"]["assessments"]["integration"] == "unknown"
    assert "stale_evidence" in expected["A8"]["issue_codes"]
    assert expected["A9"]["source_statuses"] == ["unstable", "invalid", "access_denied"]
    assert expected["A10"]["source_fingerprint_same"] is True
    assert expected["A10"]["assessments_same"] is True
