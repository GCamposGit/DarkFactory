"""USR-174: HF-24 data-protection policy is consumed by real routing and delivery paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.enterprise.enforcement import (
    DataPolicyError,
    DataPolicyUnavailable,
    enforce_data_policy,
    provider_allowed,
)
from core.git.autonomy import GitAutonomyManager
from core.line.routing import load_routing_config, pick

CAPS = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]


def _write(tmp_path: Path, items: list[dict]) -> Path:
    f = tmp_path / "enterprise" / "configs.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps(items), encoding="utf-8")
    return f


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path))
    return tmp_path


def _pick(tmp_path: Path, project_id):
    return pick(
        "development", CAPS, config=load_routing_config(), quota_lookup=lambda _p: 90.0,
        cooldown_path=tmp_path / "cd.json", project_id=project_id,
    )


def test_default_policy_keeps_routing_behavior(state):
    assert _pick(state, "darkfac") == _pick(state, None)
    assert _pick(state, "darkfac") is not None


def test_local_only_policy_blocks_cloud_harness_in_pick(state):
    _write(state, [{"project_id": "secure", "enabled": True, "residency_mode": "local_only"}])
    assert _pick(state, "secure") is None
    assert _pick(state, "darkfac") is not None


def test_allowlist_permits_named_harness(state):
    _write(state, [{"project_id": "p", "enabled": True, "residency_mode": "local_only",
                    "allowed_providers": ["claude"]}])
    assert _pick(state, "p")[0] == "claude"


def test_unreadable_policy_fails_closed_everywhere(state):
    f = state / "enterprise" / "configs.json"
    f.parent.mkdir(parents=True)
    f.write_text("{not json", encoding="utf-8")
    assert _pick(state, "darkfac") is None
    with pytest.raises(DataPolicyUnavailable) as ei:
        enforce_data_policy("darkfac", "export", provider="github")
    assert ei.value.to_dict()["error"] == "data_policy_unavailable"
    assert provider_allowed("darkfac", "claude") is False


def test_minimum_protection_blocks_secret_for_all_projects(state):
    with pytest.raises(DataPolicyError) as ei:
        enforce_data_policy("darkfac", "export", payload_text="token ghp_" + "a" * 30)
    assert ei.value.code == "data_policy_secret_in_payload"


def test_delivery_refused_when_policy_forbids(state, tmp_path):
    _write(state, [{"project_id": "secure", "enabled": True, "residency_mode": "eu_only"}])
    report = GitAutonomyManager(root=tmp_path).deliver_branch(
        "USR-1", "t", cwd=tmp_path, gh_runner=lambda *a, **k: pytest.fail("gh must not run"),
        project_id="secure",
    )
    assert report.ok is False and "data policy" in report.message


def test_delivery_unreadable_policy_refused(state, tmp_path):
    f = state / "enterprise" / "configs.json"
    f.parent.mkdir(parents=True)
    f.write_text("garbage", encoding="utf-8")
    report = GitAutonomyManager(root=tmp_path).deliver_branch(
        "USR-1", "t", cwd=tmp_path, gh_runner=lambda *a, **k: pytest.fail("gh must not run"),
    )
    assert report.ok is False and "data_policy_unavailable" in report.message
