"""Sonda de gateway OpenRouter (USR-496 T2/T3).

O portao e offline contra o snapshot versionado. A chamada viva fica atras de
``DARKFAC_LIVE_OPENROUTER=1`` e nao entra no portao.
"""

from __future__ import annotations

import json
import os
import urllib.error
from pathlib import Path

import pytest

from core.benchmarks.fetcher import OPENROUTER_MODELS_URL
from core.models.gateway_probe import (
    DEFAULT_REGISTRY_PATH,
    DEFAULT_SNAPSHOT_PATH,
    EXIT_ERROR,
    EXIT_FINDINGS,
    EXIT_OK,
    UPDATE_COMMAND,
    CatalogError,
    active_gateway_ids,
    check_gateway_ids,
    fetch_catalog_ids,
    main,
    probe_registry,
    write_snapshot,
)

_REQUIRED_WIRE_IDS = {
    "anthropic/claude-opus-5.5",
    "anthropic/claude-sonnet-5.5",
    "anthropic/claude-haiku-5.5",
    "openai/gpt-5.6-sol",
    "openai/gpt-6-luna",
    "google/gemini-3.8-flash",
    "x-ai/grok-4.6",
    "x-ai/grok-4.7",
}


def _registry(*models: dict) -> dict:
    return {"models": list(models)}


def test_real_registry_has_no_findings_against_versioned_snapshot() -> None:
    snapshot = json.loads(DEFAULT_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert snapshot["generated_by"] == UPDATE_COMMAND
    assert snapshot["source"] == OPENROUTER_MODELS_URL
    assert snapshot["collected_at"] != "2025-11-09T00:00:00Z"
    assert isinstance(snapshot["collected_at"], str) and snapshot["collected_at"]
    assert len(snapshot["model_ids"]) > 100
    assert _REQUIRED_WIRE_IDS <= set(snapshot["model_ids"])
    assert probe_registry() == []


def test_planted_gateway_outside_snapshot_names_model_and_update_command(tmp_path: Path) -> None:
    registry = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    planted_model = next(model for model in registry["models"] if model["status"] == "active")
    planted_model["gateway_ids"] = ["vendor/not-in-snapshot"]
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    findings = probe_registry(registry_path, DEFAULT_SNAPSHOT_PATH)

    assert len(findings) == 1
    finding = findings[0]
    assert finding.model == planted_model["id"]
    assert finding.gateway_id == "vendor/not-in-snapshot"
    assert planted_model["id"] in finding.message
    assert "vendor/not-in-snapshot" in finding.message
    assert UPDATE_COMMAND in finding.message


def test_empty_catalog_is_a_structured_error_not_a_finding_per_model(tmp_path: Path) -> None:
    snapshot_path = tmp_path / "empty.json"
    snapshot_path.write_text(json.dumps({"model_ids": [], "source": OPENROUTER_MODELS_URL}), encoding="utf-8")

    with pytest.raises(CatalogError) as raised:
        probe_registry(DEFAULT_REGISTRY_PATH, snapshot_path)

    assert raised.value.code == "empty_catalog"
    with pytest.raises(CatalogError) as direct:
        check_gateway_ids(_registry({"id": "m", "status": "active", "gateway_ids": ["vendor/m"]}), [])
    assert direct.value.code == "empty_catalog"


def test_missing_declared_gateway_and_canonical_fallback_are_findings() -> None:
    catalog = {"vendor/ok"}
    registry = _registry(
        {"id": "declared-missing", "status": "active", "gateway_ids": ["vendor/missing"]},
        {"id": "grok-4.7", "status": "active"},
        {"id": "kept", "status": "active", "gateway_ids": ["vendor/ok"], "cli_ids": ["not-a-wire-id"]},
        {"id": "retired", "status": "deprecated", "gateway_ids": ["vendor/gone"]},
        {"id": "candidate", "status": "candidate", "gateway_ids": ["vendor/gone"]},
    )

    findings = check_gateway_ids(registry, catalog)

    by_model = {finding.model: finding.gateway_id for finding in findings}
    assert by_model == {"declared-missing": "vendor/missing", "grok-4.7": "grok-4.7"}
    corrected = _registry(
        {"id": "declared-missing", "status": "active", "gateway_ids": ["vendor/ok"]},
        {"id": "grok-4.7", "status": "active", "gateway_ids": ["vendor/ok"]},
    )
    assert check_gateway_ids(corrected, catalog) == []


def test_cli_ids_are_not_read_as_gateway_ids() -> None:
    registry = _registry(
        {"id": "m", "status": "active", "gateway_ids": ["vendor/ok"], "cli_ids": ["cli-only-slug"]},
    )
    assert list(active_gateway_ids(registry)) == [("m", "vendor/ok")]
    assert check_gateway_ids(registry, {"vendor/ok"}) == []


def test_cli_json_exits_with_findings_and_with_empty_catalog(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(_registry({"id": "m", "status": "active", "gateway_ids": ["vendor/missing"]})),
        encoding="utf-8",
    )
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"model_ids": ["vendor/ok"]}), encoding="utf-8")

    code = main(["--registry", str(registry_path), "--snapshot", str(snapshot_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_FINDINGS
    assert payload["ok"] is False
    assert payload["findings"][0]["model"] == "m"
    assert UPDATE_COMMAND in payload["findings"][0]["message"]

    empty_path = tmp_path / "empty.json"
    empty_path.write_text(json.dumps({"data": []}), encoding="utf-8")
    code = main(["--registry", str(registry_path), "--snapshot", str(empty_path), "--json"])
    error = json.loads(capsys.readouterr().out)
    assert code == EXIT_ERROR
    assert error["error"]["code"] == "empty_catalog"


def test_fetch_uses_ledger_when_the_network_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ledger = tmp_path / "latest.json"
    ledger.write_text(
        json.dumps({"models": {"vendor/from-ledger": {"model_id": "vendor/from-ledger"}}}),
        encoding="utf-8",
    )

    def _down(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError("timed out talking to upstream")

    monkeypatch.setattr(urllib.request, "urlopen", _down)
    ids = fetch_catalog_ids(ledger_path=ledger)
    assert ids == frozenset({"vendor/from-ledger"})


def test_fetch_without_network_or_ledger_is_a_structured_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    def _down(*_args: object, **_kwargs: object) -> None:
        raise urllib.error.URLError("secret-token-must-not-leak")

    monkeypatch.setattr(urllib.request, "urlopen", _down)
    with pytest.raises(CatalogError) as raised:
        fetch_catalog_ids(ledger_path=tmp_path / "missing-ledger.json")
    assert raised.value.code == "catalog_unavailable"
    assert "secret-token-must-not-leak" not in str(raised.value)

    code = main(["--write-snapshot", "--snapshot", str(tmp_path / "out.json"), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_ERROR
    assert payload["error"]["code"] == "catalog_unavailable"
    assert "secret-token-must-not-leak" not in payload["error"]["message"]
    assert not (tmp_path / "out.json").exists()


def test_write_snapshot_rejects_an_empty_catalog(tmp_path: Path) -> None:
    with pytest.raises(CatalogError) as raised:
        write_snapshot(tmp_path / "snap.json", catalog={"data": []}, source=OPENROUTER_MODELS_URL)
    assert raised.value.code == "empty_catalog"


def test_write_snapshot_records_probe_metadata(tmp_path: Path) -> None:
    destination = write_snapshot(
        tmp_path / "snap.json",
        catalog={"data": [{"id": "vendor/a"}, {"id": "vendor/b"}]},
        source=OPENROUTER_MODELS_URL,
        collected_at="2026-11-10T12:00:00Z",
    )
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["collected_at"] == "2026-11-10T12:00:00Z"
    assert payload["source"] == OPENROUTER_MODELS_URL
    assert payload["generated_by"] == UPDATE_COMMAND
    assert payload["model_ids"] == ["vendor/a", "vendor/b"]


def test_cli_json_passes_when_the_snapshot_covers_the_registry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(_registry({"id": "m", "status": "active", "gateway_ids": ["vendor/ok"]})),
        encoding="utf-8",
    )
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps({"model_ids": ["vendor/ok"]}), encoding="utf-8")
    code = main(["--registry", str(registry_path), "--snapshot", str(snapshot_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == EXIT_OK
    assert payload == {"ok": True, "findings": []}


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("DARKFAC_LIVE_OPENROUTER") != "1",
    reason="opt-in live catalog check: set DARKFAC_LIVE_OPENROUTER=1",
)
def test_live_openrouter_catalog_covers_active_gateways() -> None:
    catalog_ids = fetch_catalog_ids()
    registry = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    assert len(catalog_ids) > 100
    assert check_gateway_ids(registry, catalog_ids) == []
