"""Offline configuration diagnostics for the live progress sink (USR-455 T1)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core.line import live_progress_doctor as doctor
from core.line import local_progress

def _postgres_url(host: str) -> str:
    """Return a harmless resolver input without storing a complete DSN or credentials."""
    return "postgresql" + "://" + host + ":5432/factory?sslmode=require"


CONTROL = _postgres_url("10.0.0.7")


def _assume_credentials_present(monkeypatch: pytest.MonkeyPatch) -> None:
    original = doctor._url_target

    def configured(
        url: str,
        source: str,
        timeout_s: float,
        statement_timeout_ms: int,
        environ: object,
    ) -> doctor.EffectiveTarget:
        return original(url, source, timeout_s, statement_timeout_ms, environ).model_copy(
            update={"credential_present": True}
        )

    monkeypatch.setattr(doctor, "_url_target", configured)


def test_matching_targets_are_reported_redacted_and_ok_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    _assume_credentials_present(monkeypatch)

    def no_connect(*args: object, **kwargs: object) -> None:
        pytest.fail("the offline doctor must not open a network connection")

    monkeypatch.setattr(local_progress.PostgresSink, "_connect", no_connect)
    result = doctor.diagnose({"DARKFAC_HF02_DATABASE_URL": CONTROL})
    assert result.cause == "ok"
    assert result.control_store.host == result.sink.host == "10.0.0.7"
    assert result.control_store.port == result.sink.port == 5432
    assert result.control_store.sslmode == result.sink.sslmode == "require"
    assert result.control_store.timeout_s == 5.0
    assert result.sink.timeout_s == 10.0
    assert result.control_store.statement_timeout_ms == 5000
    assert result.sink.statement_timeout_ms == 10000
    assert result.control_store.credential_present and result.sink.credential_present


def test_reproduces_usr455_same_target_with_shorter_historical_sink_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old 3 s/3000 ms sink budget could fail where the control store's 5 s/5000 ms worked."""
    _assume_credentials_present(monkeypatch)
    result = doctor.diagnose({
        "DARKFAC_HF02_DATABASE_URL": CONTROL,
        local_progress.ENV_CONNECT_TIMEOUT_S: "3",
        local_progress.ENV_STATEMENT_TIMEOUT_S: "3",
    })

    assert result.cause == "timeout_curto"
    assert result.control_store.source == result.sink.source == "DARKFAC_HF02_DATABASE_URL"
    assert result.control_store.host == result.sink.host == "10.0.0.7"
    assert result.control_store.port == result.sink.port == 5432
    assert result.control_store.database == result.sink.database == "factory"
    assert result.control_store.timeout_s == 5.0
    assert result.sink.timeout_s == 3.0
    assert result.control_store.statement_timeout_ms == 5000
    assert result.sink.statement_timeout_ms == 3000


def test_divergent_sink_is_classified_from_effective_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    _assume_credentials_present(monkeypatch)
    result = doctor.diagnose({
        "DARKFAC_HF02_DATABASE_URL": CONTROL,
        "DARKHUB_LINE_DATABASE_URL": _postgres_url("10.0.0.8"),
    })
    assert result.cause == "dsn_divergente"
    assert result.control_store.host == "10.0.0.7"
    assert result.sink.host == "10.0.0.8"


def test_explicit_line_url_overrides_the_control_store_fallback() -> None:
    result = doctor.diagnose({
        "DARKFAC_HF02_DATABASE_URL": CONTROL,
        "DARKHUB_LINE_DATABASE_URL": _postgres_url("10.0.0.8"),
    })
    assert result.control_store.host != result.sink.host
    assert result.control_store.source == "DARKFAC_HF02_DATABASE_URL"
    assert result.sink.source == "DARKHUB_LINE_DATABASE_URL"
    assert result.sink.host == "10.0.0.8"
    report = result.model_dump_json()
    assert "10.0.0.8" in report
    assert "postgresql" not in report


def test_local_sqlite_diagnostic_omits_machine_profile_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.paths

    monkeypatch.setattr(core.paths, "state_root", lambda: Path(r"C:\Users\private-user\.darkfac"))
    result = doctor.diagnose({})
    report = result.model_dump_json()
    assert result.control_store.database == "control.db"
    assert "private-user" not in report
    assert r"C:\Users" not in report


def test_shorter_sink_timeout_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    _assume_credentials_present(monkeypatch)
    result = doctor.diagnose({
        "DARKFAC_HF02_DATABASE_URL": CONTROL,
        local_progress.ENV_CONNECT_TIMEOUT_S: "3.5",
    })
    assert result.cause == "timeout_curto"
    assert result.sink.timeout_s == 3.5


def test_missing_sink_credentials_are_classified() -> None:
    result = doctor.diagnose({"DARKFAC_HF02_DATABASE_URL": _postgres_url("10.0.0.7")})
    assert result.cause == "credencial_ausente"
    assert result.sink.credential_present is False


def test_absent_sink_is_not_misreported_as_missing_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import core.paths

    monkeypatch.setattr(core.paths, "state_root", lambda: tmp_path)
    result = doctor.diagnose({"DARKFAC_HF02_DATABASE_URL": "mock://private-token"})

    assert result.cause == "no_url"
    assert result.sink.source == "disabled"
    assert "nenhum sink configurado" in result.detail
    report = result.model_dump_json()
    assert "private-token" not in report
    assert '"cause":"no_url"' in report


def test_malformed_host_is_structured_as_host_without_route() -> None:
    result = doctor.diagnose({"DARKFAC_HF02_DATABASE_URL": _postgres_url("bad%host")})
    assert result.cause == "host_sem_rota"
    assert "configuracao" in result.detail or "host" in result.detail


@pytest.mark.parametrize("mode", ["text", "json"])
def test_cli_never_prints_url_or_credentials(
    mode: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _assume_credentials_present(monkeypatch)
    args = ["--json"] if mode == "json" else []
    code = doctor.main(args, environ={"DARKFAC_HF02_DATABASE_URL": CONTROL})
    output = capsys.readouterr().out
    assert code == 0
    assert "password" not in output and "postgresql" not in output
    assert "10.0.0.7" in output
    if mode == "json":
        payload = json.loads(output)
        assert payload["cause"] == "ok"
        assert payload["sink"]["sslmode"] == "require"
        assert payload["sink"]["timeout_s"] == 10.0
    else:
        assert "Diagnostico offline" in output
        assert "timeout_s=10.0" in output


def test_degraded_cli_exit_code_is_one(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    _assume_credentials_present(monkeypatch)
    code = doctor.main([], environ={
        "DARKFAC_HF02_DATABASE_URL": CONTROL,
        local_progress.ENV_CONNECT_TIMEOUT_S: "2",
    })
    assert code == 1
    assert "causa: timeout_curto" in capsys.readouterr().out


def test_unexpected_resolver_failure_becomes_structured_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_url = "postgresql://writer:secret-pass@private-db:5432/factory?token=secret-token&sslpassword=secret-ssl"

    def fail(_environ: object) -> tuple[None, None]:
        raise RuntimeError(f"driver resolution failed for {secret_url}")

    monkeypatch.setattr(doctor, "resolve_sink_source", fail)
    result = doctor.diagnose({})
    report = result.model_dump_json()
    assert result.cause == "host_sem_rota"
    assert "RuntimeError" in result.detail
    for sensitive in (secret_url, "secret-pass", "secret-token", "secret-ssl", "private-db"):
        assert sensitive not in report


def test_effective_target_report_contains_only_allowlisted_connection_details() -> None:
    secret_url = (
        "postgresql://writer:secret-pass@private-db:5432/factory"
        "?sslmode=require&token=secret-token&sslpassword=secret-ssl&api_key=secret-key"
    )
    result = doctor.diagnose({"DARKFAC_HF02_DATABASE_URL": secret_url})
    report = result.model_dump_json()

    assert result.cause == "ok"
    assert result.control_store.host == "private-db"
    assert result.control_store.port == 5432
    assert result.control_store.database == "factory"
    assert result.control_store.sslmode == "require"
    for sensitive in (
        secret_url,
        "writer",
        "secret-pass",
        "secret-token",
        "secret-ssl",
        "secret-key",
        "token=",
        "sslpassword=",
        "api_key=",
    ):
        assert sensitive not in report


def test_script_entrypoint_runs_offline_and_emits_structured_json() -> None:
    # Covers scripts/live_progress_doctor.py itself; an intentionally invalid URL proves it is parsed,
    # never connected to, when publication is explicitly disabled.
    script = Path(__file__).resolve().parents[1] / "scripts" / "live_progress_doctor.py"
    env = os.environ.copy()
    env.update({"DARKFAC_LOCAL_PROGRESS": "off", "DARKFAC_HF02_DATABASE_URL": "not-a-valid-value"})
    completed = subprocess.run(
        [sys.executable, str(script), "--json"],
        cwd=script.parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["cause"] == "ok"
    assert "not-a-valid-value" not in completed.stdout
