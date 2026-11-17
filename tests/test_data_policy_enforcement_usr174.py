"""USR-174: HF-24 data-protection policy is consumed by real routing and delivery paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.enterprise.enforcement import (
    DataPolicyError,
    DataPolicyUnavailable,
    enforce_data_policy,
    load_project_policy,
    provider_allowed,
    resolve_project_policy,
)
from core.git.autonomy import GitAutonomyManager
from core.line.routing import load_routing_config, pick

CAPS = ["harness:claude", "harness:codex", "harness:grok", "harness:antigravity"]


@pytest.fixture(autouse=True)
def isolate_shared_policy_reference(tmp_path, monkeypatch):
    """Keep tests independent of any shared policy present in the developer's checkout."""
    from core.enterprise import enforcement

    monkeypatch.setattr(enforcement, "_shared_configs_file", lambda: tmp_path / "shared" / "data_policies.json")


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


def test_policy_loader_uses_explicit_root_independent_of_working_directory(tmp_path, monkeypatch):
    state_root = tmp_path / "isolated_state"
    _write(state_root, [{"project_id": "secure", "enabled": True, "residency_mode": "local_only"}])
    unrelated_cwd = tmp_path / "unrelated"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)

    cfg = load_project_policy("secure", root=state_root)

    assert cfg is not None and cfg.project_id == "secure"
    assert load_project_policy("missing", root=state_root) is None
    with pytest.raises(DataPolicyError):
        enforce_data_policy("secure", "inference", provider="claude", root=state_root)
    assert provider_allowed("secure", "claude", root=state_root) is False


def test_policy_resolution_returns_shared_source_for_project(tmp_path):
    shared_file = _write(
        tmp_path / "shared",
        [{"project_id": "secure", "enabled": True, "residency_mode": "global_allowed"}],
    )
    local_root = tmp_path / "host_state"

    resolution = resolve_project_policy(
        "secure", root=local_root, shared_configs_file=shared_file,
    )

    assert resolution.policy is not None
    assert resolution.source_path == shared_file.resolve()
    assert resolution.shared_source_path == shared_file.resolve()
    assert resolution.local_source_path.is_absolute()
    assert resolution.diagnostics == []


def test_policy_resolution_reports_behavior_changing_host_override(tmp_path, caplog):
    shared_file = _write(
        tmp_path / "shared",
        [{"project_id": "secure", "residency_mode": "global_allowed", "allowed_providers": ["claude"]}],
    )
    local_file = _write(
        tmp_path / "host_state",
        [{"project_id": "secure", "residency_mode": "local_only", "allowed_providers": ["ollama_local"]}],
    )

    resolution = resolve_project_policy(
        "secure", root=local_file.parent.parent, shared_configs_file=shared_file,
    )

    assert resolution.policy is not None and resolution.policy.residency_mode.value == "local_only"
    assert resolution.source_path == local_file.resolve()
    assert len(resolution.diagnostics) == 1
    diagnostic = resolution.diagnostics[0]
    assert diagnostic.code == "data_policy_host_divergence"
    assert diagnostic.project_id == "secure"
    assert diagnostic.shared_source_path == shared_file.resolve()
    assert diagnostic.local_source_path == local_file.resolve()
    assert diagnostic.effective_source_path == local_file.resolve()
    emitted = [record.data_policy_diagnostic for record in caplog.records if hasattr(record, "data_policy_diagnostic")]
    assert emitted[-1]["code"] == "data_policy_host_divergence"
    assert emitted[-1]["project_id"] == "secure"


def test_semantically_equivalent_policies_ignore_defaults_and_allowlist_order(tmp_path):
    shared_file = _write(
        tmp_path / "shared",
        [{
            "project_id": "secure",
            "enabled": True,
            "residency_mode": "local_only",
            "allowed_providers": ["ollama_local", "claude"],
            "max_rpo_minutes": 60,
            "max_rto_minutes": 30,
            "pii_sanitization_strict": True,
            "require_owner_signoff": True,
        }],
    )
    _write(
        tmp_path / "host_state",
        [{
            "project_id": "secure",
            "residency_mode": "local_only",
            "allowed_providers": ["claude", "ollama_local"],
        }],
    )

    resolution = resolve_project_policy(
        "secure", root=tmp_path / "host_state", shared_configs_file=shared_file,
    )

    assert resolution.diagnostics == []
    assert resolution.source_path == shared_file.resolve()


def test_local_policy_without_shared_reference_reports_host_divergence(tmp_path):
    local_file = _write(
        tmp_path / "host_state",
        [{"project_id": "darkfac", "residency_mode": "local_only", "allowed_providers": ["ollama_local"]}],
    )
    missing_shared_file = tmp_path / "shared" / "data_policies.json"

    resolution = resolve_project_policy(
        "darkfac", root=local_file.parent.parent, shared_configs_file=missing_shared_file,
    )

    assert resolution.source_path == local_file.resolve()
    assert resolution.diagnostics[0].code == "data_policy_host_divergence"
    assert resolution.diagnostics[0].shared_source_path == missing_shared_file.resolve()
    assert provider_allowed(
        "darkfac", "claude", root=local_file.parent.parent, shared_configs_file=missing_shared_file,
    ) is False
    assert provider_allowed(
        "darkfac", "ollama_local", root=local_file.parent.parent, shared_configs_file=missing_shared_file,
    ) is True


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
