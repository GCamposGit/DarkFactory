import pytest

from core.infra import secrets_vault
from core.line import pilot_secrets as ps


def test_mansaodolago_readiness_filters_required_keys_by_prefix(monkeypatch):
    monkeypatch.setattr(
        secrets_vault,
        "required_keys",
        lambda path=secrets_vault.REQUIRED_FILE: [
            "GH_APP_ID",
            "MANSAODOLAGO_SEARCH_CONSOLE_CREDENTIALS_JSON",
            "MANSAODOLAGO_INSTAGRAM_GRAPH_ACCESS_TOKEN",
            "MANSAODOLAGO_GOOGLE_ADS_REFRESH_TOKEN",
        ],
    )
    monkeypatch.setattr(
        secrets_vault,
        "decrypt",
        lambda vault=secrets_vault.VAULT_FILE: {
            "GH_APP_ID": "123",
            "MANSAODOLAGO_SEARCH_CONSOLE_CREDENTIALS_JSON": '{"ok":true}',
        },
    )
    report = ps.mansaodolago_readiness()
    assert report.project_id == "mansaodolago"
    assert {s.key for s in report.keys} == {
        ps.MANSAODOLAGO_SEARCH_CONSOLE_KEY,
        ps.MANSAODOLAGO_INSTAGRAM_GRAPH_KEY,
        ps.MANSAODOLAGO_GOOGLE_ADS_KEY,
    }
    assert not report.ready
    assert report.missing_keys == [ps.MANSAODOLAGO_INSTAGRAM_GRAPH_KEY, ps.MANSAODOLAGO_GOOGLE_ADS_KEY]


def test_mansaodolago_readiness_ready_when_all_present(monkeypatch):
    monkeypatch.setattr(
        secrets_vault,
        "required_keys",
        lambda path=secrets_vault.REQUIRED_FILE: [ps.MANSAODOLAGO_SEARCH_CONSOLE_KEY],
    )
    monkeypatch.setattr(
        secrets_vault,
        "decrypt",
        lambda vault=secrets_vault.VAULT_FILE: {ps.MANSAODOLAGO_SEARCH_CONSOLE_KEY: "secret-value"},
    )
    report = ps.mansaodolago_readiness()
    assert report.ready
    assert report.missing_keys == []


def test_require_pilot_secret_returns_value_when_present(monkeypatch):
    monkeypatch.setattr(secrets_vault, "decrypt", lambda vault=secrets_vault.VAULT_FILE: {"K": "v"})
    assert ps.require_pilot_secret("K") == "v"


def test_require_pilot_secret_fails_closed_without_mock(monkeypatch):
    monkeypatch.setattr(secrets_vault, "decrypt", lambda vault=secrets_vault.VAULT_FILE: {})
    with pytest.raises(ps.PilotSecretMissingError) as excinfo:
        ps.require_pilot_secret(ps.MANSAODOLAGO_GOOGLE_ADS_KEY)
    assert excinfo.value.key == ps.MANSAODOLAGO_GOOGLE_ADS_KEY


def test_cli_never_prints_secret_value_and_exit_code_reflects_readiness(monkeypatch, capsys):
    monkeypatch.setattr(
        secrets_vault,
        "required_keys",
        lambda path=secrets_vault.REQUIRED_FILE: [ps.MANSAODOLAGO_SEARCH_CONSOLE_KEY, ps.MANSAODOLAGO_GOOGLE_ADS_KEY],
    )
    monkeypatch.setattr(
        secrets_vault,
        "decrypt",
        lambda vault=secrets_vault.VAULT_FILE: {ps.MANSAODOLAGO_SEARCH_CONSOLE_KEY: "super-secret-value"},
    )
    exit_code = ps.main([])
    out = capsys.readouterr().out
    assert exit_code == 1
    assert "super-secret-value" not in out
    assert ps.MANSAODOLAGO_SEARCH_CONSOLE_KEY in out
    assert ps.MANSAODOLAGO_GOOGLE_ADS_KEY in out
