"""Comportamento da politica de pesquisa condicional (USR-376)."""
import json

import pytest
from pydantic import ValidationError

from core.research.policy import ResearchSourceRecord, run_research_stage

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
    assert s.url.startswith("https://") and len(s.accessed_at) == 10 and s.influence
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
