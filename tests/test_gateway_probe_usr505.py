"""Sonda offline de gateways OpenRouter (USR-505 T3 e T4).

A execucao padrao le o snapshot versionado de 2026-11-10 e nao abre rede.
A chamada viva fica atras de ``DARKFAC_LIVE_OPENROUTER=1`` e nao entra no portao.
T4: declaracao explicita vale em qualquer status; modelo nao ativo sem
``gateway_ids`` nao herda o id canonico.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import model_id_census  # noqa: E402

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
    claimed_gateway_ids,
    fetch_catalog_ids,
    main,
    probe_registry,
    write_snapshot,
)
from core.models.registry import embedded_registry, parse_registry

_CITED_WITHOUT_CATALOG_GATEWAY = (
    "gemini-3.1-pro",
    "gpt-image-2",
    "claude-3-7-sonnet",
    "gemini-2.0-flash-001",
    "deepseek/deepseek-r1",
    "deepseek-coder",
    "claude-fable-5-1",
)

_CATALOGUED_ACTIVE = {
    "claude-opus-5-5": "anthropic/claude-opus-5.5",
    "claude-sonnet-5-5": "anthropic/claude-sonnet-5.5",
    "claude-haiku-5-5": "anthropic/claude-haiku-5.5",
    "gpt-6-luna": "openai/gpt-6-luna",
    "gpt-sol": "openai/gpt-5.6-sol",
    "grok-4.6": "x-ai/grok-4.6",
    "grok-4.7": "x-ai/grok-4.7",
    "gemini-3.8-flash": "google/gemini-3.8-flash",
    "deepseek/deepseek-v4.1-flash": "deepseek/deepseek-v4.1-flash",
    "openai/gpt-4o": "openai/gpt-4o",
    "qwen/qwen-2.5-coder-32b-instruct": "qwen/qwen-2.5-coder-32b-instruct",
}
_NO_OPENROUTER_GATEWAY = {"gemini-4-neon", "gemini-4-argon-high", "gemini-4-hydrogen-high"}


def _registry(*models: dict) -> dict:
    return {"models": list(models)}


def _committed_registry() -> dict:
    raw = subprocess.check_output(
        ["git", "show", "HEAD:.factory/config/model_registry.json"],
        cwd=DEFAULT_REGISTRY_PATH.parents[2],
    )
    return json.loads(raw.decode("utf-8"))


def test_default_probe_uses_the_versioned_snapshot_and_does_not_call_the_network(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    snapshot = json.loads(DEFAULT_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    registry = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    catalog = set(snapshot["model_ids"])

    assert snapshot["generated_by"] == UPDATE_COMMAND
    assert snapshot["source"] == OPENROUTER_MODELS_URL
    collected_at = snapshot["collected_at"]
    assert isinstance(collected_at, str)
    assert collected_at.endswith("Z")
    assert datetime.fromisoformat(collected_at.replace("Z", "+00:00")).tzinfo is not None
    assert len(catalog) == 469
    assert set(_CATALOGUED_ACTIVE.values()) <= catalog

    by_model: dict[str, list[str]] = {}
    for model, gateway in active_gateway_ids(registry):
        by_model.setdefault(model, []).append(gateway)
        assert gateway in catalog

    for model, wire in _CATALOGUED_ACTIVE.items():
        assert by_model[model] == [wire]
    assert _NO_OPENROUTER_GATEWAY.isdisjoint(by_model)
    assert probe_registry() == []

    text_code = main([])
    text = capsys.readouterr().out
    assert text_code == EXIT_OK
    assert "offline snapshot" in text

    json_code = main(["--json"])
    payload = json.loads(capsys.readouterr().out)
    assert json_code == EXIT_OK
    assert payload == {"ok": True, "findings": []}


def test_roles_and_review_pairs_stay_equal_to_the_committed_registry() -> None:
    current = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    committed = _committed_registry()
    assert current["roles"] == committed["roles"]
    assert current["review_pairs"] == committed["review_pairs"]


def test_catalog_snapshot_is_outside_the_model_id_census() -> None:
    assert model_id_census.is_allowlisted(".factory/config/openrouter_catalog.json")


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
    with pytest.raises(CatalogError) as blank:
        check_gateway_ids(_registry({"id": "m", "status": "active", "gateway_ids": ["vendor/m"]}), ["", "  "])
    assert blank.value.code == "empty_catalog"


def test_unreadable_catalog_is_a_structured_error_and_never_an_empty_success(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(_registry({"id": "m", "status": "active", "gateway_ids": ["vendor/m"]})),
        encoding="utf-8",
    )
    cases = {
        tmp_path / "broken.json": "{",
        tmp_path / "shape.json": json.dumps({"nope": 1}),
        tmp_path / "list.json": json.dumps(["vendor/m"]),
    }
    for path, text in cases.items():
        path.write_text(text, encoding="utf-8")
        code = main(["--registry", str(registry_path), "--snapshot", str(path), "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert code == EXIT_ERROR
        assert payload["ok"] is False
        assert payload["error"]["code"] == "catalog_unavailable"
        assert "findings" not in payload

    code = main(["--registry", str(registry_path), "--snapshot", str(tmp_path / "missing.json"), "--json"])
    missing = json.loads(capsys.readouterr().out)
    assert code == EXIT_ERROR
    assert missing["ok"] is False
    assert missing["error"]["code"] == "catalog_unavailable"


def test_declared_gateway_and_slash_canonical_are_findings_and_cli_ids_are_not() -> None:
    catalog = {"vendor/ok"}
    registry = _registry(
        {"id": "declared-missing", "status": "active", "gateway_ids": ["vendor/missing", "vendor/also"]},
        {"id": "deepseek/canonical", "status": "active"},
        {"id": "grok-4.7", "status": "active"},
        {"id": "kept", "status": "active", "gateway_ids": ["vendor/ok"], "cli_ids": ["not-a-wire-id"]},
        {"id": "retired", "status": "deprecated", "gateway_ids": ["vendor/gone"]},
        {"id": "candidate", "status": "candidate", "gateway_ids": ["vendor/gone"]},
        {"id": "deepseek/deepseek-r1", "status": "deprecated"},
        {"id": "deepseek-coder", "status": "deprecated"},
        {"id": "claude-fable-5-1", "status": "candidate"},
        {"id": "kept-candidate", "status": "candidate", "gateway_ids": ["vendor/ok"]},
    )

    findings = check_gateway_ids(registry, catalog)

    by_model: dict[str, list[str]] = {}
    for finding in findings:
        by_model.setdefault(finding.model, []).append(finding.gateway_id)
    assert by_model == {
        "declared-missing": ["vendor/missing", "vendor/also"],
        "deepseek/canonical": ["deepseek/canonical"],
        "retired": ["vendor/gone"],
        "candidate": ["vendor/gone"],
    }
    assert list(active_gateway_ids(_registry(
        {"id": "kept", "status": "active", "gateway_ids": ["vendor/ok"], "cli_ids": ["not-a-wire-id"]},
    ))) == [("kept", "vendor/ok")]
    assert list(claimed_gateway_ids(_registry(
        {"id": "deepseek/deepseek-r1", "status": "deprecated"},
        {"id": "kept-candidate", "status": "candidate", "gateway_ids": ["vendor/ok"]},
    ))) == [("kept-candidate", "vendor/ok")]
    corrected = _registry(
        {"id": "declared-missing", "status": "active", "gateway_ids": ["vendor/ok"]},
        {"id": "deepseek/canonical", "status": "active", "gateway_ids": ["vendor/ok"]},
        {"id": "grok-4.7", "status": "active"},
        {"id": "retired", "status": "deprecated", "gateway_ids": ["vendor/ok"]},
        {"id": "candidate", "status": "candidate"},
    )
    assert check_gateway_ids(corrected, catalog) == []


def test_cli_json_exits_with_findings_and_with_empty_catalog(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    assert payload["findings"][0]["gateway_id"] == "vendor/missing"
    assert UPDATE_COMMAND in payload["findings"][0]["message"]

    empty_path = tmp_path / "empty.json"
    empty_path.write_text(json.dumps({"data": []}), encoding="utf-8")
    code = main(["--registry", str(registry_path), "--snapshot", str(empty_path), "--json"])
    error = json.loads(capsys.readouterr().out)
    assert code == EXIT_ERROR
    assert error["ok"] is False
    assert error["error"]["code"] == "empty_catalog"
    assert "findings" not in error


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
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
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
    assert payload["ok"] is False
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
        catalog={"data": [{"id": "vendor/b"}, {"id": "vendor/a"}, {"id": ""}]},
        source=OPENROUTER_MODELS_URL,
        collected_at="2026-11-10T12:00:00Z",
    )
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["collected_at"] == "2026-11-10T12:00:00Z"
    assert payload["source"] == OPENROUTER_MODELS_URL
    assert payload["generated_by"] == UPDATE_COMMAND
    assert payload["model_ids"] == ["vendor/a", "vendor/b"]


def test_cli_json_passes_when_the_snapshot_covers_the_registry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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


def test_cited_non_active_models_declare_no_gateway_absent_from_the_snapshot() -> None:
    registry = json.loads(DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8"))
    snapshot = json.loads(DEFAULT_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    catalog = set(snapshot["model_ids"])
    committed = _committed_registry()
    parsed = parse_registry(registry)
    embedded = embedded_registry()
    file_by_id = {model["id"]: model for model in registry["models"]}
    committed_by_id = {model["id"]: model for model in committed["models"]}
    embedded_by_id = {model.id: model for model in embedded.models}

    assert registry["roles"] == committed["roles"]
    assert registry["review_pairs"] == committed["review_pairs"]
    assert {model.id: list(model.gateway_ids) for model in parsed.models} == {
        model.id: list(model.gateway_ids) for model in embedded.models
    }
    assert {model.id: model.status for model in parsed.models} == {
        model["id"]: model["status"] for model in committed["models"]
    }
    assert {model.id: list(model.aliases) for model in parsed.models} == {
        model["id"]: list(model.get("aliases") or []) for model in committed["models"]
    }

    claims = list(claimed_gateway_ids(registry))
    for model_id in _CITED_WITHOUT_CATALOG_GATEWAY:
        record = file_by_id[model_id]
        assert record["status"] != "active"
        assert list(record.get("gateway_ids") or []) == []
        assert list(embedded_by_id[model_id].gateway_ids) == []
        assert record["status"] == committed_by_id[model_id]["status"]
        assert record.get("aliases") == committed_by_id[model_id].get("aliases")
        assert model_id not in {model for model, _gateway in claims}

    assert ("deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro") not in claims
    assert ("deepseek/deepseek-v4.1-flash", "deepseek/deepseek-v4.1-flash") in claims
    assert ("claude-opus-5", "anthropic/claude-opus-5") in claims
    assert ("gpt-6-astra", "openai/gpt-6-astra") in claims
    assert ("gemini-3.1-flash-lite-image", "google/gemini-3.1-flash-lite-image") in claims
    assert {gateway for _model, gateway in claims} <= catalog
    assert probe_registry() == []


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
