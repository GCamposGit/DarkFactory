"""Comportamento da politica de pesquisa condicional (USR-376)."""
import json
import logging
from datetime import datetime as DateTime

import pytest
from pydantic import ValidationError

from core.research.policy import ResearchSourceRecord, lookup, run_research_stage

HISTORICAL_PROVENANCE_BASELINE = "2020-01-15"


class _HistoricalDateTime(DateTime):
    @classmethod
    def now(cls, tz=None):
        value = DateTime.fromisoformat(HISTORICAL_PROVENANCE_BASELINE)
        return value.replace(tzinfo=tz) if tz else value


@pytest.fixture(autouse=True)
def _freeze_research_clock(monkeypatch):
    monkeypatch.setattr("core.research.policy.datetime", _HistoricalDateTime)


FEATURE = {"id": "USR-1", "item_type": "feature", "estimated_complexity": "high", "tags": [],
           "title": "Integracao webhook Telegram para alertas", "problem_statement": "Receber alertas via API externa."}


def fake(calls):
    def search(q):
        calls.append(q)
        return [{"title": "Telegram Bot API", "url": "https://core.telegram.org/bots/api",
                 "insight": "Webhooks exigem HTTPS", "influence": "Usamos webhook com segredo"}]
    return search


def test_simple_fix_is_skipped_with_reason(tmp_path):
    calls = []
    t = {"id": "USR-2", "item_type": "quality", "tags": ["bug"], "title": "Fix typo no portao", "estimated_complexity": "low"}
    art = run_research_stage(t, tmp_path, fake(calls))
    assert art.kind == "research_skipped" and art.reason and not calls
    assert json.loads((tmp_path / "_artifacts" / "USR-2.json").read_text("utf-8"))["kind"] == "research_skipped"


def test_feature_researches_and_records_sources_with_url_and_date(tmp_path):
    art = run_research_stage(FEATURE, tmp_path, fake([]))
    assert art.kind == "research_done" and art.ledger_id
    s = art.sources[0]
    assert s.url.startswith("https://") and s.accessed_at == HISTORICAL_PROVENANCE_BASELINE and s.influence
    saved = json.loads((tmp_path / art.ledger_id / "ledger.json").read_text("utf-8"))
    assert saved["sources"][0]["metadata"]["accessed_at"] == s.accessed_at
    assert "Como influenciou o plano" in saved["insights"][0]["architectural_implications"]


def test_same_topic_reuses_ledger_without_web_search(tmp_path):
    run_research_stage(FEATURE, tmp_path, fake([]))
    calls = []
    art = run_research_stage({**FEATURE, "id": "USR-3"}, tmp_path, fake(calls))
    assert art.kind == "research_reused" and not calls and art.sources[0].url


def test_source_without_url_is_rejected():
    with pytest.raises(ValidationError):
        ResearchSourceRecord(title="x", url="", accessed_at="2000-01-01", insight="i", influence="o")


def test_unreadable_legacy_ledger_never_fails_lookup(tmp_path):
    """Legacy ledgers (old enums/fields) broke the RESEARCH stage of every fleet ticket on 25/10."""
    bad = tmp_path / "20000101_legacy"
    bad.mkdir()
    (bad / "ledger.json").write_text('{"ledger_id": "20000101_legacy", "query": "x", "sources": [{"canonical_url": "u"}]}', "utf-8")
    run_research_stage(FEATURE, tmp_path, fake([]))
    assert lookup(["x"], tmp_path) is not None


def test_research_reuses_historical_question_and_provenance_without_search(tmp_path):
    ledger_id = f"{HISTORICAL_PROVENANCE_BASELINE.replace('-', '')}_owner_workflow_revision"
    directory = tmp_path / ledger_id
    directory.mkdir()
    (directory / "ledger.json").write_text(json.dumps({
        "ledger_id": ledger_id,
        "question": "How to configure Telegram webhook delivery",
        "consulted_on": HISTORICAL_PROVENANCE_BASELINE,
        "status": "validated",
        "sources": [{
            "canonical_url": "https://docs.example.org/telegram/webhooks",
            "organization": "Example Docs",
            "consulted_at": HISTORICAL_PROVENANCE_BASELINE,
            "application": "Webhook retries require idempotency",
        }],
    }), encoding="utf-8")
    ticket = {
        "id": "USR-42", "item_type": "feature", "estimated_complexity": "high",
        "question": "Telegram webhook delivery retry policy",
        "title": "Reliability improvements", "problem_statement": "External API integration",
    }
    calls = []

    matches = lookup(["telegram", "webhook", "delivery"], tmp_path)
    artifact = run_research_stage(ticket, tmp_path, fake(calls))

    assert [ledger.ledger_id for ledger in matches] == [ledger_id]
    assert artifact.kind == "research_reused"
    assert artifact.ledger_id == ledger_id
    assert calls == []
    assert artifact.sources[0].title
    assert artifact.sources[0].insight == "Webhook retries require idempotency"
    assert artifact.sources[0].influence
    saved = json.loads((directory / "ledger.json").read_text(encoding="utf-8"))
    assert saved["related_tickets"] == ["USR-42"]
    assert saved["provenance"]["status"] == "validated"
    assert saved["sources"][0]["metadata"]["accessed_at"] == HISTORICAL_PROVENANCE_BASELINE
    assert saved["sources"][0]["metadata"]["organization"] == "Example Docs"


def test_lookup_isolates_invalid_history_and_logs_redacted_structured_error(tmp_path, caplog):
    bad_id = f"{HISTORICAL_PROVENANCE_BASELINE.replace('-', '')}_invalid_history"
    bad_dir = tmp_path / bad_id
    bad_dir.mkdir()
    secret_url = "https://user:credential@example.org/private?token=secret"
    (bad_dir / "ledger.json").write_text(json.dumps({
        "ledger_id": bad_id, "question": "Telegram webhook delivery",
        "sources": [{"canonical_url": secret_url, "authority_tier": "not-a-tier"}],
    }), encoding="utf-8")
    valid_ids = [f"{HISTORICAL_PROVENANCE_BASELINE.replace('-', '')}_valid_one", "historical_valid_two"]
    for ledger_id in valid_ids:
        directory = tmp_path / ledger_id
        directory.mkdir()
        (directory / "ledger.json").write_text(json.dumps({
            "ledger_id": ledger_id, "question": "Telegram webhook delivery",
            "sources": [{"canonical_url": f"https://{ledger_id}.example.org/docs"}],
        }), encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="core.research.ledger")
    calls = []
    ticket = {**FEATURE, "id": "USR-43", "question": "Telegram webhook delivery"}

    matches = lookup(["telegram", "webhook", "delivery"], tmp_path)
    artifact = run_research_stage(ticket, tmp_path, fake(calls))

    assert {ledger.ledger_id for ledger in matches} == set(valid_ids)
    assert artifact.kind == "research_reused"
    assert calls == []
    record = next(record for record in caplog.records if getattr(record, "ledger_id", None) == bad_id)
    assert record.field == "sources[0].authority_tier"
    assert record.reason == "unknown authority tier"
    assert secret_url not in record.getMessage()
    assert "credential" not in record.getMessage() and "secret" not in record.getMessage()
