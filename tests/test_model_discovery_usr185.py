"""USR-185 slice 5: model discovery (offline only, no network, no paid calls)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from core.models import discovery
from core.models.registry import DEFAULT_REGISTRY_PATH, parse_registry

FIXTURE = Path(__file__).parent / "fixtures" / "openrouter_models_sample.json"
DAY = "2027-01-05"


def _src(cost: float = 0.0) -> discovery.OpenRouterSource:
    return discovery.OpenRouterSource(discovery.file_fetcher(FIXTURE), cost_usd=cost)


def _reg_copy(tmp_path: Path) -> Path:
    target = tmp_path / "model_registry.json"
    shutil.copyfile(DEFAULT_REGISTRY_PATH, target)
    return target


def test_discovers_only_new_models_as_candidate(tmp_path: Path) -> None:
    path = _reg_copy(tmp_path)
    result = discovery.run(path, discovery.DiscoveryConfig(), [_src()], today=DAY)
    ids = {e["id"] for e in result.new_entries}
    assert "acme/nova-9" in ids and "acme/fable-lite" in ids and "beta/mystery" in ids
    assert "google/gemini-3.1-pro" not in ids  # known via gateway id
    assert "openai/gpt-6-astra" not in ids
    assert result.skipped_invalid == 1
    assert all(e["status"] == "candidate" for e in result.new_entries)
    nova = next(e for e in result.new_entries if e["id"] == "acme/nova-9")
    assert nova["price_input_per_mtok"] == 0.5 and nova["price_output_per_mtok"] == 1.5
    assert nova["context_tokens"] == 128000 and nova["discovered_at"] == DAY and nova["source"] == "openrouter"
    assert nova["release_date"] is not None
    mystery = next(e for e in result.new_entries if e["id"] == "beta/mystery")
    assert mystery["price_input_per_mtok"] is None


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    path = _reg_copy(tmp_path)
    before = path.read_text(encoding="utf-8")
    discovery.run(path, discovery.DiscoveryConfig(), [_src()], write=False, today=DAY)
    assert path.read_text(encoding="utf-8") == before


def test_write_is_idempotent_and_never_alters_existing(tmp_path: Path) -> None:
    path = _reg_copy(tmp_path)
    old = json.loads(path.read_text(encoding="utf-8"))
    discovery.run(path, discovery.DiscoveryConfig(), [_src()], write=True, today=DAY)
    after_first = path.read_text(encoding="utf-8")
    new = json.loads(after_first)
    assert new["models"][: len(old["models"])] == old["models"]
    assert new["roles"] == old["roles"]
    parse_registry(new)
    second = discovery.run(path, discovery.DiscoveryConfig(), [_src()], write=True, today="2027-01-06")
    assert not second.changed
    assert path.read_text(encoding="utf-8") == after_first


def test_forbidden_marker_flagged_and_never_routable(tmp_path: Path) -> None:
    path = _reg_copy(tmp_path)
    discovery.run(path, discovery.DiscoveryConfig(), [_src()], write=True, today=DAY)
    reg = parse_registry(json.loads(path.read_text(encoding="utf-8")))
    entry = next(m for m in reg.models if m.id == "acme/fable-lite")
    assert entry.forbidden_autonomous and entry.status == "candidate"
    assert "acme/fable-lite" not in reg.roles.values()


def test_default_free_source_ok_with_zero_cap() -> None:
    assert discovery.check_spend([_src()], 0.0) == 0.0


def test_paid_source_refused_without_cap_or_over_cap(tmp_path: Path) -> None:
    path = _reg_copy(tmp_path)
    before = path.read_text(encoding="utf-8")

    class Boom(discovery.OpenRouterSource):
        def fetch(self):  # type: ignore[override]
            raise AssertionError("fetch must not run when spend is refused")

    with pytest.raises(discovery.SpendCapExceeded):
        discovery.run(path, discovery.DiscoveryConfig(spend_cap_usd=0.0), [Boom(cost_usd=0.5)], write=True)
    with pytest.raises(discovery.SpendCapExceeded):
        discovery.run(path, discovery.DiscoveryConfig(spend_cap_usd=0.25), [Boom(cost_usd=0.5)], write=True)
    assert path.read_text(encoding="utf-8") == before
    assert discovery.check_spend([_src(0.2)], 0.25) == pytest.approx(0.2)


def test_default_config_is_free_and_zero_cap() -> None:
    cfg = discovery.load_config()
    assert cfg.spend_cap_usd == 0.0
    assert all(sc.estimated_cost_usd == 0.0 for sc in cfg.sources.values())
