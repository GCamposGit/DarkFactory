"""USR-232: a provider id from POST /api/usage/sync names a file, so it must never leave its directory.

`HubService.sync_usage_data` takes `provider_id` from the request body and the monitors write
`<dir>/<provider_id>.json`. Before this check an id such as `../../x` wrote outside the monitor
directory. The endpoint now skips unsafe ids (the valid items of the same payload are still saved and
counted), and both monitors refuse them on their own (`ValueError`) as defense in depth.

Reachability Contract: python -m pytest tests/test_usage_sync_provider_id.py -q
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Set

import pytest
from fastapi.testclient import TestClient

from core.usage.api_credits import ApiCreditsMonitor, CreditAccountUpdateRequest
from core.usage.monitor import (
    PROVIDER_ID_PATTERN,
    AccountUsageMonitor,
    is_valid_provider_id,
    snapshot_path,
)
from hub.backend.api import get_hub_service
from hub.backend.main import app
from hub.backend.models import UsageSyncPayload
from hub.backend.service import HubService

# "ABSOLUTE" stands for an absolute path under tmp_path (resolved per test); the other absolute-looking
# ids are a Windows drive path and a POSIX path.
UNSAFE_PROVIDER_IDS = [
    "../x",
    "..\\x",
    "a/b",
    ".hidden",
    "",
    "a" * 65,
    "C:\\x",
    "/etc/x",
    "ABSOLUTE",
    None,
    123,
]


def unsafe_id(raw: Any, tmp_path: Path) -> Any:
    return str((tmp_path / "outside" / "abs").resolve()) if raw == "ABSOLUTE" else raw


def param_id(value: Any) -> str:
    return repr(value)[:14]


def tree(root: Path) -> Set[str]:
    """Every file and directory under `root`, as POSIX paths relative to it."""
    return {path.relative_to(root).as_posix() for path in root.rglob("*")}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DARKHUB_USAGE_DIR", "DARKFAC_PROJECT_ROOT", "DARKFAC_OPENAI_CREDITS_JSON", "DARKFAC_OPENROUTER_CREDITS_JSON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DARKHUB_QUOTA_SOURCE", "snapshot")


@pytest.fixture
def service(tmp_path: Path) -> HubService:
    root = tmp_path / "container"
    data_dir = root / "data"
    (data_dir / "demands").mkdir(parents=True)
    (data_dir / "demands" / "demands.json").write_text("[]", encoding="utf-8")
    (tmp_path / "outside").mkdir()
    usage_dir = tmp_path / "volume" / "usage"
    (usage_dir / "providers").mkdir(parents=True)  # exist before the sync, like on a used volume
    return HubService(data_dir=data_dir, usage_dir=usage_dir, project_root=root)


def valid_account() -> dict[str, Any]:
    return {
        "provider_id": "openai",
        "status": "connected",
        "windows": [{"quota_id": "codex:weekly", "label": "1 semana", "used_percent": 53.0, "remaining_percent": 47.0}],
        "message": "Leitura do host medidor.",
    }


def valid_credit() -> dict[str, Any]:
    return {"provider_id": "openrouter", "status": "active", "is_connected": True, "available_credit_usd": 42.5}


@pytest.mark.parametrize("raw_id", UNSAFE_PROVIDER_IDS, ids=param_id)
def test_sync_skips_unsafe_ids_for_accounts_and_credits_and_saves_the_valid_items(
    tmp_path: Path, service: HubService, caplog: pytest.LogCaptureFixture, raw_id: Any
) -> None:
    bad_id = unsafe_id(raw_id, tmp_path)
    before = tree(tmp_path)

    with caplog.at_level(logging.WARNING, logger="darkhub.service"):
        response = service.sync_usage_data(
            UsageSyncPayload(
                client_node_id="notebook",
                accounts=[{"provider_id": bad_id, "status": "connected", "message": "evil"}, valid_account()],
                credits=[{"provider_id": bad_id, "status": "active", "available_credit_usd": 1.0}, valid_credit()],
            )
        )

    # No exception; only the valid items are counted and written.
    assert response.accounts_updated == 1
    assert response.credits_updated == 1
    account_file = service.usage_dir / "providers" / "openai.json"
    credit_file = service.usage_dir / "credits" / "openrouter.json"
    assert json.loads(account_file.read_text(encoding="utf-8"))["windows"][0]["remaining_percent"] == 47.0
    assert json.loads(credit_file.read_text(encoding="utf-8"))["available_credit_usd"] == 42.5

    # Nothing else appeared anywhere in the tree (in particular nothing outside the monitor directories).
    created = tree(tmp_path) - before
    assert created == {
        account_file.relative_to(tmp_path).as_posix(),
        credit_file.relative_to(tmp_path).as_posix(),
    }
    assert tree(tmp_path / "outside") == set()

    # Both skips are logged, and the raw value is never echoed beyond a short repr.
    assert "Ignoring account snapshot with invalid provider_id" in caplog.text
    assert "Ignoring credit snapshot with invalid provider_id" in caplog.text
    assert "a" * 41 not in caplog.text


@pytest.mark.parametrize("raw_id", UNSAFE_PROVIDER_IDS, ids=param_id)
def test_monitors_refuse_unsafe_ids_with_value_error_and_write_nothing(
    tmp_path: Path, raw_id: Any
) -> None:
    bad_id = unsafe_id(raw_id, tmp_path)
    (tmp_path / "outside").mkdir()
    accounts = AccountUsageMonitor(tmp_path / "usage" / "providers", adapters=[])
    credits = ApiCreditsMonitor(tmp_path / "usage" / "credits")
    before = tree(tmp_path)

    with pytest.raises(ValueError):
        accounts.save_snapshot(bad_id, {"status": "connected"})
    with pytest.raises(ValueError):
        credits.save_snapshot(bad_id, {"status": "active"})
    with pytest.raises(ValueError):
        # The credit endpoint calls this with the id taken from the URL.
        credits.update_account(bad_id, CreditAccountUpdateRequest(available_credit_usd=1.0))
    with pytest.raises(ValueError):
        credits._read_snapshot(bad_id)

    assert tree(tmp_path) == before


@pytest.mark.parametrize("good_id", ["openai", "openrouter", "deepseek", "a", "A-b_9", "x" * 64, "0day"])
def test_safe_ids_still_round_trip_through_both_monitors(tmp_path: Path, good_id: str) -> None:
    assert is_valid_provider_id(good_id)
    assert PROVIDER_ID_PATTERN.fullmatch(good_id)
    assert snapshot_path(tmp_path, good_id) == tmp_path / f"{good_id}.json"

    accounts = AccountUsageMonitor(tmp_path / "providers", adapters=[])
    accounts.save_snapshot(good_id, {"status": "connected", "message": "first"})
    accounts.save_snapshot(good_id, {"message": "second"})
    saved = json.loads((tmp_path / "providers" / f"{good_id}.json").read_text(encoding="utf-8"))
    assert (saved["status"], saved["message"]) == ("connected", "second")

    credits = ApiCreditsMonitor(tmp_path / "credits")
    credits.save_snapshot(good_id, {"available_credit_usd": 3.0})
    assert credits._read_snapshot(good_id) == {"available_credit_usd": 3.0}


@pytest.mark.parametrize("trailing", ["openai\n", "openai ", " openai", "openai.json", "ópenai"])
def test_ids_that_merely_look_like_slugs_are_refused(trailing: str) -> None:
    assert not is_valid_provider_id(trailing)


def test_resolved_path_outside_the_directory_is_refused_on_any_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same guard as the symlink case below, without needing symlink privileges (Windows)."""
    providers = tmp_path / "providers"
    providers.mkdir()
    escaped = tmp_path / "outside" / "victim.json"
    real_resolve = Path.resolve

    def resolve_evil_elsewhere(self: Path, *args: Any, **kwargs: Any) -> Path:
        return escaped if self.name == "evil.json" else real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_evil_elsewhere)

    with pytest.raises(ValueError):
        snapshot_path(providers, "evil")
    assert snapshot_path(providers, "fine") == providers / "fine.json"


def test_symlink_that_leaves_the_directory_is_refused(tmp_path: Path) -> None:
    providers = tmp_path / "providers"
    providers.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim.json"
    victim.write_text("{}", encoding="utf-8")
    try:
        (providers / "evil.json").symlink_to(victim)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this machine")

    with pytest.raises(ValueError):
        snapshot_path(providers, "evil")
    with pytest.raises(ValueError):
        AccountUsageMonitor(providers, adapters=[]).save_snapshot("evil", {"status": "connected"})
    with pytest.raises(ValueError):
        ApiCreditsMonitor(providers).save_snapshot("evil", {"status": "active"})
    assert victim.read_text(encoding="utf-8") == "{}"


def test_sync_endpoint_saves_the_valid_item_next_to_unsafe_ones(
    tmp_path: Path, service: HubService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DARKFAC_TELEMETRY_KEY", "usr-232-telemetry-key")
    before = tree(tmp_path)
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        response = TestClient(app).post(
            "/api/usage/sync",
            json={
                "client_node_id": "notebook",
                "accounts": [{"provider_id": "../../escape", "status": "connected"}, valid_account()],
                "credits": [{"provider_id": "..\\escape", "status": "active"}, {"provider_id": None}, valid_credit()],
            },
            headers={"X-DarkFac-Telemetry-Key": "usr-232-telemetry-key"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["accounts_updated"] == 1
    assert response.json()["credits_updated"] == 1
    assert tree(tmp_path) - before == {
        (service.usage_dir / "providers" / "openai.json").relative_to(tmp_path).as_posix(),
        (service.usage_dir / "credits" / "openrouter.json").relative_to(tmp_path).as_posix(),
    }


@pytest.mark.parametrize("unsafe_id", [".hidden", "x" * 65, "-leading", "with space"])
def test_credit_account_endpoint_answers_400_for_an_unsafe_provider_id(
    tmp_path: Path, service: HubService, unsafe_id: str
) -> None:
    """The guard raises ValueError; the endpoint must turn it into a client error, never a 500."""
    before = tree(tmp_path)
    app.dependency_overrides[get_hub_service] = lambda: service
    try:
        response = TestClient(app).post(f"/api/credits/accounts/{unsafe_id}", json={"available_credit_usd": 1.0})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert tree(tmp_path) == before
