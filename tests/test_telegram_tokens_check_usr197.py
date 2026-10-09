"""USR-197: scripts/telegram_tokens_check.py (hermetico: getMe injetado, nenhuma rede real)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from scripts import telegram_tokens_check as ttc

# Synthetic tokens only; they match the Telegram token format but belong to no bot.
OWNER_NEW = "111111111:" + "A" * 35
OWNER_OLD = "222222222:" + "B" * 35
OPS_NEW = "333333333:" + "C" * 35
OPS_OLD = "444444444:" + "D" * 35

ALL_KEYS = ("TELEGRAM_OWNER_BOT_TOKEN", "TELEGRAM_OPS_BOT_TOKEN", "TELEGRAM_BOT_TOKEN")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ALL_KEYS:
        monkeypatch.delenv(key, raising=False)


class FakeGetMe:
    """Injected fetch: tokens listed as live answer valid, everything else is revoked."""

    def __init__(self, live: dict[str, str], broken: set[str] | None = None) -> None:
        self.live = live
        self.broken = broken or set()
        self.calls: list[str] = []

    def __call__(self, token: str, timeout: float) -> tuple[str, str | None]:
        self.calls.append(token)
        if token in self.broken:
            return ("error", None)
        if token in self.live:
            return ("valid", self.live[token])
        return ("revoked", None)


def _write_json(root: Path, name: str, token: str | None) -> None:
    folder = root / ".factory" / "telegram"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(json.dumps({"bot_token": token}), encoding="utf-8")


def _no_user_env(name: str) -> str | None:
    return None


def _run(root: Path, fetch, **kwargs):
    return ttc.run_check(root=root, fetch=fetch, user_env_reader=kwargs.pop("user_env_reader", _no_user_env), **kwargs)


def test_all_valid_exit_zero_and_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", OWNER_NEW)
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", OPS_NEW)
    fetch = FakeGetMe({OWNER_NEW: "darkfac_bot", OPS_NEW: "darkfac_ops_bot"})
    lines, code = _run(tmp_path, fetch)
    text = "\n".join(lines)
    assert code == 0
    assert "VALIDO(@darkfac_bot)" in text and "VALIDO(@darkfac_ops_bot)" in text
    assert "EFETIVA" in text
    assert "papel" in lines[0] and "fonte" in lines[0] and "estado" in lines[0]
    assert "ausente" in text
    assert "OK (codigo 0)" in text


def test_revoked_secondary_source_fails_and_never_prints_full_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", OWNER_NEW)
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", OPS_NEW)
    _write_json(tmp_path, "owner_config.json", OWNER_OLD)
    _write_json(tmp_path, "ops_config.json", OPS_OLD)
    (tmp_path / ".env").write_text(f"TELEGRAM_OWNER_BOT_TOKEN={OWNER_OLD}\nTELEGRAM_OPS_BOT_TOKEN={OPS_OLD}\n", encoding="utf-8")
    fetch = FakeGetMe({OWNER_NEW: "darkfac_bot", OPS_NEW: "darkfac_ops_bot"})
    lines, code = _run(tmp_path, fetch)
    text = "\n".join(lines)
    assert code == 1
    assert text.count(ttc.REVOKED) >= 4  # 2 json + 2 .env
    assert "Fonte secundaria REVOGADA" in text
    for token in (OWNER_NEW, OWNER_OLD, OPS_NEW, OPS_OLD):
        assert token not in text
        assert token[:12] not in text  # not even a long prefix
    # a revoked value read twice (json + .env) is probed once
    assert fetch.calls.count(OWNER_OLD) == 1
    # the effective source is the process env, per loader precedence
    effective_lines = [ln for ln in lines if "EFETIVA" in ln]
    assert len(effective_lines) == 2 and all("env(processo)" in ln for ln in effective_lines)


def test_effective_source_revoked_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_json(tmp_path, "ops_config.json", OPS_OLD)  # only source -> effective via JSON
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", OWNER_NEW)
    fetch = FakeGetMe({OWNER_NEW: "darkfac_bot"})
    lines, code = _run(tmp_path, fetch)
    text = "\n".join(lines)
    assert code == 1
    assert "A fonte EFETIVA (.factory/telegram/ops_config.json) esta REVOGADA" in text


def test_loader_precedence_dotenv_beats_json(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"TELEGRAM_OPS_BOT_TOKEN={OPS_NEW}\nTELEGRAM_OWNER_BOT_TOKEN={OWNER_NEW}\n", encoding="utf-8")
    _write_json(tmp_path, "ops_config.json", OPS_OLD)
    fetch = FakeGetMe({OWNER_NEW: "o", OPS_NEW: "p"})
    lines, code = _run(tmp_path, fetch)
    eff = [ln for ln in lines if "EFETIVA" in ln]
    assert any(".env:TELEGRAM_OPS_BOT_TOKEN" in ln for ln in eff)
    assert code == 1  # the stale JSON is a revoked secondary source


def test_windows_user_scope_is_reported_but_not_effective(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", OWNER_NEW)
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", OPS_NEW)
    user_env = {"TELEGRAM_OPS_BOT_TOKEN": OPS_OLD}
    fetch = FakeGetMe({OWNER_NEW: "o", OPS_NEW: "p"})
    lines, code = _run(tmp_path, fetch, user_env_reader=user_env.get)
    user_lines = [ln for ln in lines if "env(User Windows):TELEGRAM_OPS_BOT_TOKEN" in ln]
    assert user_lines and ttc.REVOKED in user_lines[0] and "EFETIVA" not in user_lines[0]
    assert "nao lida pelo loader" in user_lines[0]
    assert code == 1


def test_whitespace_and_newline_warning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", OWNER_NEW + "\r\n")
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", " " + OPS_NEW)
    fetch = FakeGetMe({OWNER_NEW: "o", OPS_NEW: "p"})
    lines, code = _run(tmp_path, fetch)
    text = "\n".join(lines)
    assert "QUEBRA DE LINHA" in text
    assert "ESPACOS nas pontas" in text
    assert fetch.calls and all(t == t.strip() for t in fetch.calls)  # probed stripped
    assert code == 0  # warnings alone do not fail


def test_network_error_is_not_revoked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", OWNER_NEW)
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", OPS_NEW)
    lines, code = _run(tmp_path, FakeGetMe({}, broken={OWNER_NEW, OPS_NEW}))
    assert ttc.NETWORK_ERROR in "\n".join(lines)
    assert code == 0


def test_empty_and_missing_effective(tmp_path: Path) -> None:
    _write_json(tmp_path, "owner_config.json", "")
    lines, code = _run(tmp_path, FakeGetMe({}))
    text = "\n".join(lines)
    assert ttc.EMPTY in text
    assert "SEM token efetivo" in text
    assert code == 1


def test_offline_makes_no_network_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", OWNER_NEW)
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", OPS_NEW)
    _write_json(tmp_path, "ops_config.json", "not-a-token-format")

    def explode(token: str, timeout: float):
        raise AssertionError("rede nao deve ser usada em --offline")

    lines, code = _run(tmp_path, explode, offline=True)
    text = "\n".join(lines)
    assert ttc.FORMAT_OK in text and ttc.BAD_FORMAT in text
    assert "--offline" in text
    assert code == 1  # secondary source with invalid format


def test_offline_clean_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", OWNER_NEW)
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", OPS_NEW)
    _, code = _run(tmp_path, FakeGetMe({}), offline=True)
    assert code == 0


def test_loader_divergence_warning_is_not_emitted_by_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    monkeypatch.setenv("TELEGRAM_OWNER_BOT_TOKEN", OWNER_NEW)
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", OPS_NEW)
    _write_json(tmp_path, "owner_config.json", OWNER_OLD)
    with caplog.at_level(logging.WARNING):
        _run(tmp_path, FakeGetMe({OWNER_NEW: "o", OPS_NEW: "p"}))
    assert not any("diverge" in r.getMessage() for r in caplog.records)


def test_fetch_get_me_maps_http_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import urllib.error

    def raiser(code: int):
        def _open(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, code, "x", {}, io.BytesIO(b""))

        return _open

    monkeypatch.setattr(ttc.urllib.request, "urlopen", raiser(401))
    assert ttc.fetch_get_me(OPS_NEW, 1.0) == ("revoked", None)
    monkeypatch.setattr(ttc.urllib.request, "urlopen", raiser(500))
    assert ttc.fetch_get_me(OPS_NEW, 1.0) == ("error", None)

    def timeout(request, timeout=None):
        raise TimeoutError("boom")

    monkeypatch.setattr(ttc.urllib.request, "urlopen", timeout)
    assert ttc.fetch_get_me(OPS_NEW, 1.0) == ("error", None)


def test_main_returns_run_check_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr(ttc, "run_check", lambda **kw: (["linha"], 1))
    assert ttc.main(["--offline"]) == 1
    assert "linha" in capsys.readouterr().out
