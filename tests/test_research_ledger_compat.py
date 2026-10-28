import json

import pytest

from core.research.compat import ResearchLedgerSchemaError
from core.research.ledger import KnowledgeLedgerManager
from core.research.models import AuthorityTier

HISTORICAL_PROVENANCE_BASELINE = "2020-01-15"


def _write_ledger(tmp_path, ledger_id, payload):
    directory = tmp_path / ledger_id
    directory.mkdir()
    (directory / "ledger.json").write_text(json.dumps(payload), encoding="utf-8")


def test_loads_primary_authority_historical_schema_and_round_trips_provenance(tmp_path):
    ledger_id = f"{HISTORICAL_PROVENANCE_BASELINE.replace('-', '')}_hybrid_workflow"
    _write_ledger(tmp_path, ledger_id, {
        "schema_version": "1",
        "ledger_id": ledger_id,
        "consulted_on": HISTORICAL_PROVENANCE_BASELINE,
        "status": "planning_research",
        "decision_status": "candidate",
        "evidence_limits": ["No runtime spike"],
        "limits": "validate before rollout",
        "revalidate": "on model refresh",
        "plan_ref": "plans/workflow.md",
        "sources": [{
            "id": "dbos-architecture",
            "url": "https://docs.example.org/architecture",
            "application": "Recovery requirements",
            "authority_tier": "primary",
            "source_type": "official_documentation",
            "accessed_on": HISTORICAL_PROVENANCE_BASELINE,
            "custom_evidence": "retained",
        }],
    })

    manager = KnowledgeLedgerManager(tmp_path)
    ledger = manager.load_ledger(ledger_id)

    assert ledger is not None
    assert ledger.sources[0].authority_tier is AuthorityTier.HIGH_CREDIBILITY
    assert ledger.sources[0].metadata["accessed_at"] == HISTORICAL_PROVENANCE_BASELINE
    assert ledger.sources[0].metadata["custom_evidence"] == "retained"
    assert ledger.query == ledger_id
    assert ledger.provenance["schema_version"] == "1"
    assert ledger.provenance["evidence_limits"] == ["No runtime spike"]

    manager.save_ledger(ledger)
    reloaded = manager.load_ledger(ledger_id)
    assert reloaded is not None
    serialized = reloaded.to_dict()
    assert serialized["provenance"] == ledger.provenance
    assert serialized["sources"][0]["metadata"]["accessed_at"] == HISTORICAL_PROVENANCE_BASELINE
    assert serialized["sources"][0]["metadata"]["custom_evidence"] == "retained"


def test_loads_question_and_canonical_url_historical_schema(tmp_path):
    ledger_id = f"{HISTORICAL_PROVENANCE_BASELINE.replace('-', '')}_owner_workflow_revision"
    _write_ledger(tmp_path, ledger_id, {
        "ledger_id": ledger_id,
        "question": "Model identifier and infrastructure inspection",
        "sources": [{
            "canonical_url": "https://developers.example.org/models/model-x",
            "title": "Model X",
            "organization": "Example Org",
            "consulted_at": HISTORICAL_PROVENANCE_BASELINE,
            "insight": "Official model identifier",
            "related": ["HF-07"],
        }],
        "revalidate": "on catalog refresh",
    })

    manager = KnowledgeLedgerManager(tmp_path)
    ledger = manager.load_ledger(ledger_id)

    assert ledger is not None
    assert ledger.query == "Model identifier and infrastructure inspection"
    assert ledger.sources[0].url == "https://developers.example.org/models/model-x"
    assert ledger.sources[0].id
    assert ledger.sources[0].title == "Model X"
    assert ledger.sources[0].metadata["accessed_at"] == HISTORICAL_PROVENANCE_BASELINE
    assert ledger.sources[0].metadata["organization"] == "Example Org"
    assert ledger.sources[0].metadata["related"] == ["HF-07"]
    assert ledger.sources[0].summary == "Official model identifier"
    assert ledger.provenance["revalidate"] == "on catalog refresh"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("primary", AuthorityTier.HIGH_CREDIBILITY),
        ("official", AuthorityTier.HIGH_CREDIBILITY),
        ("canonical", AuthorityTier.HIGH_CREDIBILITY),
        ("documentation", AuthorityTier.HIGH_CREDIBILITY),
        ("secondary", AuthorityTier.TREND_SIGNAL),
        ("community", AuthorityTier.TREND_SIGNAL),
        ("trend", AuthorityTier.TREND_SIGNAL),
        ("trend_signal", AuthorityTier.TREND_SIGNAL),
        ("social", AuthorityTier.TREND_SIGNAL),
    ],
)
def test_authority_aliases_are_normalized(tmp_path, value, expected):
    ledger_id = f"alias-{value}"
    _write_ledger(tmp_path, ledger_id, {
        "ledger_id": ledger_id,
        "query": "test",
        "topic_type": "topic_concept",
        "sources": [{"url": "https://example.org", "authority_tier": value}],
    })
    ledger = KnowledgeLedgerManager(tmp_path).load_ledger(ledger_id)
    assert ledger is not None
    assert ledger.sources[0].authority_tier is expected


def test_unknown_authority_tier_raises_structured_redacted_error(tmp_path):
    ledger_id = "invalid-authority"
    secret_url = "https://user:supersecretpassword@example.org/private?token=abc"
    _write_ledger(tmp_path, ledger_id, {
        "ledger_id": ledger_id,
        "sources": [{"url": secret_url, "authority_tier": "unknown-tier"}],
    })

    with pytest.raises(ResearchLedgerSchemaError) as error:
        KnowledgeLedgerManager(tmp_path).load_ledger(ledger_id)

    assert error.value.ledger_id == ledger_id
    assert error.value.field == "sources[0].authority_tier"
    assert "unknown authority tier" in error.value.reason
    assert "unknown-tier" in str(error.value)
    assert "supersecretpassword" not in str(error.value)
    assert secret_url not in str(error.value)


def test_list_ledgers_normalizes_query_and_source_count_for_history(tmp_path):
    _write_ledger(tmp_path, "old-ledger", {
        "ledger_id": "old-ledger",
        "question": "Historical question",
        "sources": [
            {"canonical_url": "https://one.example.org", "title": "One"},
            {"canonical_url": "https://two.example.org", "title": "Two"},
        ],
    })

    rows = KnowledgeLedgerManager(tmp_path).list_ledgers()

    assert rows == [{
        "ledger_id": "old-ledger",
        "query": "Historical question",
        "topic_type": "topic_concept",
        "created_at": "",
        "sources_count": 2,
        "insights_count": 0,
        "path": str(tmp_path / "old-ledger"),
    }]
