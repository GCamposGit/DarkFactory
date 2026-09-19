"""Contract tests for the curated HF-01-01 baseline data files."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from core.planning.baseline_models import (
    ClaimAssertion,
    ClaimDimension,
    CollectedBaseline,
    EvidenceClaim,
    EvidenceKind,
    PlannedItem,
    SourceObservation,
    SourceStatus,
    ValidationMode,
)
from core.planning.baseline_probes import ProbeObservation, ProbeStatus
from core.planning.baseline_reconcile import reconcile_baseline, source_fingerprint

ROOT = Path(__file__).parents[1]
CATALOG_PATH = ROOT / "docs" / "handoffs" / "hf01-sources.json"
CLAIMS_PATH = ROOT / "docs" / "handoffs" / "hf01-claims.json"
FIXTURES_PATH = ROOT / "tests" / "fixtures" / "baseline_cases.json"

EXPECTED_CATALOG_SHA256 = "31c95fb515955af9e9710b501bdb59c38191e66bf474712156f43df288bb635d"
EXPECTED_CLAIMS_SHA256 = "262e74402cc33b970de469fc875250c68fc444cdb578f5746d73dfccdf0b0916"
EXPECTED_FIXTURES_SHA256 = "fbb1a3f444ad0c6e3a897490bd02f544cfd2b63d09d0fd63cab1c5ef888b59d3"

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
    assert _sha256(CATALOG_PATH) == EXPECTED_CATALOG_SHA256
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


def test_curated_claims_are_separated_hashed_and_grounded_in_catalog_sources() -> None:
    assert _sha256(CLAIMS_PATH) == EXPECTED_CLAIMS_SHA256
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

    target_items = {claim["item_id"] for claim in claims}
    assert {"DF-11", "DF-20", "DF-21", "DF-23", "INFRA-08", "INFRA-09", "INFRA-10", "RM-07", "RM-09"} <= target_items
    assert any(claim["item_id"] == "RM-07" and claim["assertion"] == "negative" for claim in claims)
    assert any(claim["item_id"] == "RM-07" and claim["assertion"] == "positive" for claim in claims)
    assert any(claim["item_id"] == "INFRA-10" and claim["source_id"] == "usr-16-report" for claim in claims)
    assert any(claim["item_id"] == "n8n" and claim["dimension"] == "operation" for claim in claims)
    assert any(claim["item_id"] == "PostgreSQL" and claim["evidence_kind"] == "owner_statement" for claim in claims)


def test_acceptance_fixtures_cover_a1_to_a10_without_commands_or_secrets() -> None:
    assert _sha256(FIXTURES_PATH) == EXPECTED_FIXTURES_SHA256
    fixtures = _read_json(FIXTURES_PATH)

    assert fixtures["schema_version"] == "1"
    cases = fixtures["cases"]
    assert {case["case_id"] for case in cases} == {f"A{number}" for number in range(1, 11)}
    assert len(FIXTURES_PATH.read_bytes()) < 64 * 1024
    serialized = json.dumps(fixtures, ensure_ascii=False).lower()
    assert not any(key in serialized for key in ('"command"', '"commands"', '"shell"', '"secret"'))

    expected = {case["case_id"]: case["expected"] for case in cases}
    now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    base_sha = "b" * 40
    hash_val = "a" * 64

    # A1: Ticket planejado com relatório de implementação, mas sem SHA remoto
    obs_a1 = SourceObservation(source_id="fixture", relative_path=Path("fixture.md"), sha256=hash_val, status=SourceStatus.READ, observed_at=now)
    item_a1 = PlannedItem(item_id="T-A1", title="T-A1", declared_status="planned", dependencies=[], source_id="fixture", locator="fixture.md#T-A1")
    claim_a1 = EvidenceClaim(
        claim_id="c-a1", item_id="T-A1", dimension=ClaimDimension.IMPLEMENTATION, assertion=ClaimAssertion.POSITIVE,
        evidence_kind=EvidenceKind.DOCUMENT, source_id="fixture", locator="fixture.md#T-A1", source_hash=hash_val,
        candidate_sha=None, scope="fixture", summary="fixture"
    )
    res_a1 = reconcile_baseline(CollectedBaseline(observations=[obs_a1], planned_items=[item_a1], claims=[], issues=[]), [claim_a1], base_sha=base_sha, snapshot_id="s-a1", observed_at=now)
    assert res_a1.items[0].implementation.value == expected["A1"]["assessments"]["implementation"]
    assert res_a1.items[0].integration.value == expected["A1"]["assessments"]["integration"]
    assert res_a1.items[0].operation.value == expected["A1"]["assessments"]["operation"]
    assert [i.code for i in res_a1.issues if i.code != "HF02_RUNTIME_EVIDENCE_INCOMPLETE"] == expected["A1"]["issue_codes"]

    # A2: DF-23 declara protocolo, mas não automação funcional
    item_a2 = PlannedItem(item_id="DF-23", title="DF-23", declared_status="completed", dependencies=[], source_id="fixture", locator="fixture.md#DF-23")
    claim_a2 = EvidenceClaim(
        claim_id="c-a2", item_id="DF-23", dimension=ClaimDimension.IMPLEMENTATION, assertion=ClaimAssertion.PARTIAL,
        evidence_kind=EvidenceKind.DOCUMENT, source_id="fixture", locator="fixture.md#DF-23", source_hash=hash_val,
        candidate_sha=None, scope="automation coverage", summary="automation coverage is partial"
    )
    res_a2 = reconcile_baseline(CollectedBaseline(observations=[obs_a1], planned_items=[item_a2], claims=[], issues=[]), [claim_a2], base_sha=base_sha, snapshot_id="s-a2", observed_at=now)
    assert res_a2.items[0].implementation.value == expected["A2"]["assessments"]["implementation"]
    assert any(code in [i.code for i in res_a2.issues] for code in expected["A2"]["issue_codes"])

    # A3: Declarações conflitantes entre fontes
    item_a3_1 = PlannedItem(item_id="INFRA-10", title="INFRA-10", declared_status="planned", dependencies=[], source_id="infra-roadmap", locator="infra.md#INFRA-10")
    item_a3_2 = PlannedItem(item_id="INFRA-10", title="INFRA-10", declared_status="completed", dependencies=[], source_id="usr-16-report", locator="usr16.md#INFRA-10")
    obs_a3_1 = SourceObservation(source_id="infra-roadmap", relative_path=Path("infra.md"), sha256=hash_val, status=SourceStatus.READ, observed_at=now)
    obs_a3_2 = SourceObservation(source_id="usr-16-report", relative_path=Path("usr16.md"), sha256=hash_val, status=SourceStatus.READ, observed_at=now)
    res_a3 = reconcile_baseline(CollectedBaseline(observations=[obs_a3_1, obs_a3_2], planned_items=[item_a3_1, item_a3_2], claims=[], issues=[]), [], base_sha=base_sha, snapshot_id="s-a3", observed_at=now)
    assert len(res_a3.items) == expected["A3"]["item_count"]
    assert any(code in [i.code for i in res_a3.issues] for code in expected["A3"]["issue_codes"])

    # A4: n8n link genérico não é evidência de operação
    item_a4 = PlannedItem(item_id="n8n", title="n8n", declared_status="active", dependencies=[], source_id="fixture", locator="fixture.md#n8n")
    claim_a4 = EvidenceClaim(
        claim_id="c-a4", item_id="n8n", dimension=ClaimDimension.OPERATION, assertion=ClaimAssertion.NEGATIVE,
        evidence_kind=EvidenceKind.DOCUMENT, source_id="fixture", locator="fixture.md#n8n", source_hash=hash_val,
        candidate_sha=None, scope="fixture", summary="link points only to https://n8n.io"
    )
    res_a4 = reconcile_baseline(CollectedBaseline(observations=[obs_a1], planned_items=[item_a4], claims=[], issues=[]), [claim_a4], base_sha=base_sha, snapshot_id="s-a4", observed_at=now)
    assert res_a4.items[0].operation.value == expected["A4"]["assessments"]["operation"]
    assert any(code in [i.code for i in res_a4.issues] for code in expected["A4"]["issue_codes"])

    # A5: Declaração de owner sem credencial exige acesso
    item_a5 = PlannedItem(item_id="PostgreSQL", title="PostgreSQL", declared_status="planned", dependencies=[], source_id="fixture", locator="fixture.md#PostgreSQL")
    claim_a5 = EvidenceClaim(
        claim_id="c-a5", item_id="PostgreSQL", dimension=ClaimDimension.OPERATION, assertion=ClaimAssertion.POSITIVE,
        evidence_kind=EvidenceKind.OWNER_STATEMENT, source_id="fixture", locator="fixture.md#PostgreSQL", source_hash=hash_val,
        candidate_sha=None, scope="fixture", summary="installed, working and tested by owner"
    )
    res_a5 = reconcile_baseline(CollectedBaseline(observations=[obs_a1], planned_items=[item_a5], claims=[], issues=[]), [claim_a5], base_sha=base_sha, snapshot_id="s-a5", observed_at=now)
    assert res_a5.items[0].operation.value == expected["A5"]["assessments"]["operation"]
    assert any(code in [i.code for i in res_a5.issues] for code in expected["A5"]["issue_codes"])

    # A6: Endpoint 404 em probe não apaga relatório de implementação
    item_a6 = PlannedItem(item_id="DF-21", title="DF-21", declared_status="planned", dependencies=[], source_id="fixture", locator="fixture.md#DF-21")
    claim_a6 = EvidenceClaim(
        claim_id="c-a6", item_id="DF-21", dimension=ClaimDimension.IMPLEMENTATION, assertion=ClaimAssertion.POSITIVE,
        evidence_kind=EvidenceKind.DOCUMENT, source_id="fixture", locator="fixture.md#DF-21", source_hash=hash_val,
        candidate_sha=None, scope="fixture", summary="artifact present"
    )
    probe_a6 = ProbeObservation(
        probe_id="p-a6", item_id="DF-21", origin="https://hub.example.test", status=ProbeStatus.NOT_FOUND,
        status_code=404, observed_at=now, environment="target-vps", validation_mode=ValidationMode.TARGET_ENVIRONMENT
    )
    res_a6 = reconcile_baseline(CollectedBaseline(observations=[obs_a1], planned_items=[item_a6], claims=[], issues=[]), [claim_a6], base_sha=base_sha, snapshot_id="s-a6", observed_at=now, probe_observations=(probe_a6,))
    assert res_a6.items[0].implementation.value == expected["A6"]["assessments"]["implementation"]
    assert any(code in [i.code for i in res_a6.issues] for code in expected["A6"]["issue_codes"])

    # A7: Menção incidental não conclui item mencionado
    item_a7 = PlannedItem(item_id="DF-23", title="DF-23", declared_status="planned", dependencies=["DF-17"], source_id="fixture", locator="fixture.md#DF-23")
    res_a7 = reconcile_baseline(CollectedBaseline(observations=[obs_a1], planned_items=[item_a7], claims=[], issues=[]), [], base_sha=base_sha, snapshot_id="s-a7", observed_at=now)
    assert len([c for c in res_a7.claims if c.item_id == "DF-17"]) == expected["A7"]["completion_claims_for_mentioned_item"]

    # A8: Evidência remota antiga gera status unknown e issue stale_evidence
    item_a8 = PlannedItem(item_id="DF-20", title="DF-20", declared_status="planned", dependencies=[], source_id="fixture", locator="fixture.md#DF-20")
    claim_a8 = EvidenceClaim(
        claim_id="c-a8", item_id="DF-20", dimension=ClaimDimension.INTEGRATION, assertion=ClaimAssertion.POSITIVE,
        evidence_kind=EvidenceKind.REMOTE_GIT, source_id="fixture", locator="fixture.md#DF-20", source_hash=hash_val,
        candidate_sha="1" * 40, scope="fixture", summary="remote git check"
    )
    res_a8 = reconcile_baseline(CollectedBaseline(observations=[obs_a1], planned_items=[item_a8], claims=[], issues=[]), [claim_a8], base_sha=base_sha, snapshot_id="s-a8", observed_at=now)
    assert res_a8.items[0].integration.value == expected["A8"]["assessments"]["integration"]
    assert any(code in [i.code for i in res_a8.issues] for code in expected["A8"]["issue_codes"])

    # A9: Categorias distintas de falha na coleta permanecem preservadas
    obs_unstable = SourceObservation(source_id="unstable", relative_path=Path("unstable.md"), sha256=None, status=SourceStatus.UNSTABLE, observed_at=now, error_code="SOURCE_UNSTABLE")
    obs_invalid = SourceObservation(source_id="invalid", relative_path=Path("invalid.json"), sha256=None, status=SourceStatus.INVALID, observed_at=now, error_code="SOURCE_INVALID_JSON")
    obs_outside = SourceObservation(source_id="outside", relative_path=Path("outside.md"), sha256=None, status=SourceStatus.ACCESS_DENIED, observed_at=now, error_code="SOURCE_OUTSIDE_ROOT")
    res_a9 = reconcile_baseline(CollectedBaseline(observations=[obs_unstable, obs_invalid, obs_outside], planned_items=[], claims=[], issues=[]), [], base_sha=base_sha, snapshot_id="s-a9", observed_at=now)
    assert [entry.status.value for entry in res_a9.source_observations] == expected["A9"]["source_statuses"]

    # A10: Mesmos bytes observados em horários diferentes mantêm fingerprint idêntico
    obs_a10_first = SourceObservation(source_id="fixture", relative_path=Path("fixture.md"), sha256=hash_val, status=SourceStatus.READ, observed_at=now)
    obs_a10_second = obs_a10_first.model_copy(update={"observed_at": now.replace(minute=5)})
    assert (source_fingerprint([obs_a10_first]) == source_fingerprint([obs_a10_second])) is expected["A10"]["source_fingerprint_same"]
    res_a10_first = reconcile_baseline(CollectedBaseline(observations=[obs_a10_first], planned_items=[item_a1], claims=[], issues=[]), [claim_a1], base_sha=base_sha, snapshot_id="s-a10-1", observed_at=now)
    res_a10_second = reconcile_baseline(CollectedBaseline(observations=[obs_a10_second], planned_items=[item_a1], claims=[], issues=[]), [claim_a1], base_sha=base_sha, snapshot_id="s-a10-2", observed_at=now.replace(minute=5))
    assert (res_a10_first.items == res_a10_second.items) is expected["A10"]["assessments_same"]


def test_baseline_catalog_and_fixtures_mutation_rejection(tmp_path: Path) -> None:
    # 1. Mutar arquivo do catálogo altera o hash e falha na verificação de digest
    mutated_catalog = tmp_path / "mutated_catalog.json"
    content = CATALOG_PATH.read_text(encoding="utf-8")
    mutated_catalog.write_text(content + "\n ", encoding="utf-8")
    assert _sha256(mutated_catalog) != EXPECTED_CATALOG_SHA256

    # 2. Mutar o resultado esperado na fixture sem reconciliação gera divergência observável
    now = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    obs = SourceObservation(source_id="fixture", relative_path=Path("fixture.md"), sha256="a" * 64, status=SourceStatus.READ, observed_at=now)
    claim_obj = EvidenceClaim(
        claim_id="c-mut", item_id="T-A1", dimension=ClaimDimension.IMPLEMENTATION, assertion=ClaimAssertion.POSITIVE,
        evidence_kind=EvidenceKind.DOCUMENT, source_id="fixture", locator="fixture.md#T-A1", source_hash="a" * 64,
        candidate_sha=None, scope="fixture", summary="fixture"
    )
    res = reconcile_baseline(CollectedBaseline(observations=[obs], planned_items=[PlannedItem(item_id="T-A1", title="T-A1", declared_status="planned", dependencies=[], source_id="fixture", locator="fixture.md#T-A1")], claims=[], issues=[]), [claim_obj], base_sha="b" * 40, snapshot_id="s-mut", observed_at=now)
    
    # Documento não atestado produz 'reported'. Se alguém adulterar o expected para 'verified', a comparação falha:
    with pytest.raises(AssertionError):
        assert res.items[0].implementation.value == "verified"

    # 3. Fonte com hash divergente gera stale_evidence no reconciliador
    corrupt_obs = SourceObservation(source_id="fixture", relative_path=Path("fixture.md"), sha256="0" * 64, status=SourceStatus.READ, observed_at=now)
    res_corrupt = reconcile_baseline(CollectedBaseline(observations=[corrupt_obs], planned_items=[], claims=[], issues=[]), [claim_obj], base_sha="b" * 40, snapshot_id="s-corrupt", observed_at=now)
    issue_codes = {i.code for i in res_corrupt.issues}
    assert "stale_evidence" in issue_codes

    # 4. Fonte referenciada ausente gera claim_source_missing
    res_missing_src = reconcile_baseline(CollectedBaseline(observations=[], planned_items=[], claims=[], issues=[]), [claim_obj], base_sha="b" * 40, snapshot_id="s-missing", observed_at=now)
    assert "claim_source_missing" in {i.code for i in res_missing_src.issues}
