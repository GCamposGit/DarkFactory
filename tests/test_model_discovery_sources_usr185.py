from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.models import discovery
from core.models.discovery_sources import ProviderApiSource, apply_candidates, pending_candidates, already_ran_today, build_sources, run_daily_discovery
from core.models.registry import DEFAULT_REGISTRY_PATH

DAY1 = "-".join(("2026", "10", "22"))
DAY2 = "-".join(("2026", "10", "23"))
KEY_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-SECRET",
    "OPENAI_API_KEY": "sk-oai-SECRET",
    "XAI_API_KEY": "xai-SECRET",
    "GEMINI_API_KEY": "gm-SECRET",
}

PAYLOADS = {
    "api.anthropic.com": {"data": [{"id": "zz-claude-test-1", "created_at": "not-a-date"}]},
    "api.openai.com": {"data": [{"id": "zz-gpt-test-1", "created": 1790000000}]},
    "api.x.ai": {"data": [{"id": "zz-grok-test-1"}]},
    "generativelanguage": {"models": [{"name": "models/zz-gemini-test-1", "inputTokenLimit": 1000, "outputTokenLimit": 10}]},
}


def fake_http(calls: list):
    def get(url: str, headers: dict):
        calls.append((url, headers))
        for host, payload in PAYLOADS.items():
            if host in url:
                return payload
        raise AssertionError(url)

    return get


@pytest.fixture()
def registry(tmp_path: Path) -> Path:
    target = tmp_path / "model_registry.json"
    target.write_text(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return target


CFG = discovery.DiscoveryConfig(sources={"openrouter": discovery.SourceConfig(enabled=False)})


@pytest.mark.parametrize(
    "name,model_id,header",
    [
        ("anthropic", "zz-claude-test-1", "x-api-key"),
        ("openai", "zz-gpt-test-1", "Authorization"),
        ("xai", "zz-grok-test-1", "Authorization"),
        ("google", "zz-gemini-test-1", "x-goog-api-key"),
    ],
)
def test_each_source_fetches_and_normalizes(name, model_id, header):
    calls: list = []
    src = ProviderApiSource(name, fake_http(calls), KEY_ENV)
    records = src.fetch()
    assert len(records) == 1
    assert header in calls[0][1]
    entry = discovery._NORMALIZERS[name](records[0], [], DAY1)
    assert entry["id"] == model_id and entry["status"] == "candidate" and entry["source"] == name


def test_missing_key_is_skipped_with_reason():
    sources, skipped = build_sources(CFG, fake_http([]), {"OPENAI_API_KEY": "k"})
    assert [s.name for s in sources] == ["openai"]
    assert set(skipped) == {"anthropic", "xai", "google"}
    assert skipped["anthropic"].startswith("missing_api_key:")
    assert "SECRET" not in json.dumps(skipped)


def test_daily_dedupe_and_idempotent(registry: Path, tmp_path: Path):
    calls: list = []
    state = tmp_path / "state"
    dup = {"data": [{"id": "zz-gpt-test-1"}]}
    first = run_daily_discovery(registry, CFG, fake_http(calls), KEY_ENV, state, DAY1)
    assert sorted(first["new"]) == ["zz-claude-test-1", "zz-gemini-test-1", "zz-gpt-test-1", "zz-grok-test-1"]
    assert already_ran_today(DAY1, state)
    n_calls = len(calls)
    assert run_daily_discovery(registry, CFG, fake_http(calls), KEY_ENV, state, DAY1) is None
    assert len(calls) == n_calls
    assert len(pending_candidates(state)) == 4
    applied = apply_candidates(registry, state)
    assert len(applied) == 4 and apply_candidates(registry, state) == []
    ids = [m["id"] for m in json.loads(registry.read_text(encoding="utf-8"))["models"]]
    assert ids.count("zz-gpt-test-1") == 1
    # next day: everything already known, no new entries
    PAYLOADS_BACKUP = dict(PAYLOADS)
    second = run_daily_discovery(registry, CFG, fake_http(calls), KEY_ENV, state, DAY2)
    assert second["new"] == [] and PAYLOADS_BACKUP == PAYLOADS and dup


def test_failing_source_does_not_stop_others(registry: Path, tmp_path: Path):
    def http(url, headers):
        if "openai" in url:
            raise OSError("boom gm-SECRET")
        return fake_http([])(url, headers)

    out = run_daily_discovery(registry, CFG, http, KEY_ENV, tmp_path / "s", DAY1)
    assert out["failed_sources"] == {"openai": "OSError"}
    assert "zz-claude-test-1" in out["new"] and "SECRET" not in json.dumps(out)


def test_daily_never_touches_registry(registry: Path, tmp_path: Path):
    before, mtime = registry.read_bytes(), registry.stat().st_mtime_ns
    state = tmp_path / "st"
    out = run_daily_discovery(registry, CFG, fake_http([]), KEY_ENV, state, DAY1)
    assert out["new"] and out["written"] is False
    assert registry.read_bytes() == before and registry.stat().st_mtime_ns == mtime
    saved = json.loads((state / "model_discovery" / f"candidates-{DAY1}.json").read_text(encoding="utf-8"))
    assert {e["id"] for e in saved} == set(out["new"])
