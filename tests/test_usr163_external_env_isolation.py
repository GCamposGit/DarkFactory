"""USR-163: Telegram/R2 settings of the host machine must not leak into the offline suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.infra.r2_client import R2StorageClient
from core.integrations import telegram as telegram_module
from core.integrations.telegram import load_telegram_config
from tests.conftest import IMPORT_ROOT

SYNTHETIC_TOKEN = "synthetic-token-123"


def test_no_external_service_env_reaches_a_test() -> None:
    leaked = sorted(
        key
        for key in os.environ
        if key.startswith(("TELEGRAM_", "R2_"))
        or key in {"DARKHUB_TELEGRAM_AUTO_WEBHOOK", "DARKFAC_BACKUP_ENCRYPTION_KEY"}
    )
    assert leaked == []


def test_r2_client_defaults_to_the_offline_mock() -> None:
    client = R2StorageClient()
    assert client.is_mock is True
    assert client.configured is False


def test_test_can_still_set_its_own_telegram_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_OPS_BOT_TOKEN", SYNTHETIC_TOKEN)
    assert load_telegram_config(role="ops").bot_token == SYNTHETIC_TOKEN


def test_real_checkout_dotenv_is_ignored_but_tmp_root_dotenv_is_read(tmp_path: Path) -> None:
    assert telegram_module._read_env_fallback(IMPORT_ROOT) == {}
    (tmp_path / ".env").write_text(f"TELEGRAM_OPS_BOT_TOKEN={SYNTHETIC_TOKEN}\n", encoding="utf-8")
    assert telegram_module._read_env_fallback(tmp_path) == {"TELEGRAM_OPS_BOT_TOKEN": SYNTHETIC_TOKEN}
