import json

from core.research import cli
from core.research.ledger import KnowledgeLedgerManager

HISTORICAL_PROVENANCE_BASELINE = "2020-01-15"


def _write_ledger(base_dir, ledger_id, payload):
    directory = base_dir / ledger_id
    directory.mkdir(parents=True)
    path = directory / "ledger.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_migration_cli_is_read_only_by_default_and_preserves_provenance(tmp_path, monkeypatch, capsys):
    ledger_id = f"{HISTORICAL_PROVENANCE_BASELINE.replace('-', '')}_owner_workflow_revision"
    ledger_path = _write_ledger(tmp_path, ledger_id, {
        "ledger_id": ledger_id,
        "question": "Model identifier and infrastructure inspection",
        "sources": [{
            "canonical_url": "https://developers.example.org/models/model-x",
            "title": "Model X",
            "organization": "Example Org",
            "consulted_at": HISTORICAL_PROVENANCE_BASELINE,
            "insight": "Official model identifier",
            "authority_tier": "primary",
        }],
        "revalidate": "on catalog refresh",
    })
    original_json = ledger_path.read_text(encoding="utf-8")
    insights_path = ledger_path.parent / "INSIGHTS.md"
    manager = KnowledgeLedgerManager(tmp_path)
    monkeypatch.setattr(cli, "KnowledgeLedgerManager", lambda: manager)
    monkeypatch.setattr("sys.argv", ["research", "migrate-ledgers"])

    cli.main()

    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["applied"] is False
    assert dry_run["errors"] == []
    assert dry_run["ledgers"][0]["ledger_id"] == ledger_id
    assert "sources[0].url" in dry_run["ledgers"][0]["normalization"]["changed_fields"]
    assert ledger_path.read_text(encoding="utf-8") == original_json
    assert not insights_path.exists()

    monkeypatch.setattr("sys.argv", ["research", "migrate-ledgers", "--apply", "--ledger-id", ledger_id])
    cli.main()
    applied = json.loads(capsys.readouterr().out)

    assert applied["applied"] is True
    assert applied["ledgers"][0]["status"] == "migrated"
    canonical = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert canonical["sources"][0]["url"] == "https://developers.example.org/models/model-x"
    assert canonical["sources"][0]["metadata"]["organization"] == "Example Org"
    assert canonical["sources"][0]["metadata"]["accessed_at"] == HISTORICAL_PROVENANCE_BASELINE
    assert canonical["sources"][0]["authority_tier"] == "high_credibility"
    assert canonical["provenance"]["revalidate"] == "on catalog refresh"
    assert "Model X" in insights_path.read_text(encoding="utf-8")


def test_migration_reports_invalid_ledger_and_migrates_other_ledgers(tmp_path, monkeypatch, capsys):
    _write_ledger(tmp_path, "bad-ledger", {
        "ledger_id": "bad-ledger",
        "sources": [{
            "url": "https://user:supersecret@example.org/private?token=secret-token",
            "authority_tier": "unknown-tier",
        }],
    })
    good_path = _write_ledger(tmp_path, "good-ledger", {
        "ledger_id": "good-ledger",
        "question": "Historical question",
        "sources": [{"canonical_url": "https://example.org/source", "title": "Source"}],
    })
    manager = KnowledgeLedgerManager(tmp_path)
    monkeypatch.setattr(cli, "KnowledgeLedgerManager", lambda: manager)
    monkeypatch.setattr("sys.argv", ["research", "migrate-ledgers", "--apply"])

    try:
        cli.main()
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("invalid ledger migration must return a nonzero exit code")

    result = json.loads(capsys.readouterr().out)
    assert result["ledgers"][0]["ledger_id"] == "good-ledger"
    assert result["ledgers"][0]["status"] == "migrated"
    assert result["errors"] == [{
        "ledger_id": "bad-ledger",
        "field": "sources[0].authority_tier",
        "reason": "unknown authority tier",
    }]
    output = json.dumps(result)
    assert "supersecret" not in output
    assert "secret-token" not in output
    assert json.loads(good_path.read_text(encoding="utf-8"))["query"] == "Historical question"
