from __future__ import annotations

import json
from pathlib import Path

from core.orchestrator.guard import audit_paths


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
POLICY_PATH = PROJECT_ROOT / ".github" / "workflows" / "trusted-policy.yml"
HARNESS_CONFIG_PATH = PROJECT_ROOT / "harness.config.json"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _policy_workflow() -> str:
    return POLICY_PATH.read_text(encoding="utf-8")


def test_all_validation_control_files_are_protected() -> None:
    controls = [
        "AGENTS.md",
        "MISSION.md",
        "FACTORY_RULES.md",
        ".github/workflows/ci.yml",
        "core/orchestrator/guard.py",
    ]

    assert audit_paths(controls) == controls


def test_pull_request_policy_uses_base_owned_verifier_and_exact_shas() -> None:
    policy = _policy_workflow()

    assert "pull_request_target:" in policy
    assert "permissions:\n  contents: read" in policy
    assert "github.event.pull_request.base.sha" in policy
    assert "github.event.pull_request.head.sha" in policy
    assert "path: trusted" in policy
    assert "path: candidate" in policy
    assert policy.count("persist-credentials: false") >= 2

    trusted_policy = policy[policy.index("  trusted-pr-policy:") :]
    assert "pip install" not in trusted_policy
    assert "runner.py" not in trusted_policy
    assert "python ../trusted/core/orchestrator/guard.py" in trusted_policy
    assert "working-directory: candidate" in trusted_policy

    workflow = _workflow()
    assert "pull_request_target" not in workflow
    assert "  pull_request:\n" in workflow
    assert "  push:\n" in workflow
    assert "  schedule:\n" in workflow

    # USR-300: the heavy job is `pr-tests`; the required check names
    # `pr-validation (ubuntu-latest)` / `pr-validation (windows-latest)` are aggregators.
    for name in ("ubuntu-latest", "windows-latest"):
        assert f"name: pr-validation ({name})" in workflow
    aggregators = workflow[workflow.index("  pr-validation-ubuntu:") : workflow.index("  main-tests:")]
    assert aggregators.count('needs.pr-tests.result }}" = "success" ] || exit 1') == 2
    assert aggregators.count('needs.ledger-fast.result }}" = "success" ] || exit 1') == 2

    guard_command = "python ../trusted/core/orchestrator/guard.py"
    materialize_guard = "cp ../trusted/core/orchestrator/guard.py core/orchestrator/guard.py"
    materialize_governance = "cp -R ../trusted/core/governance/. core/governance/"
    harness_command = "python core/harness/runner.py --profile pr"
    tests_start = workflow.index("  pr-tests:")
    tests_end = workflow.index("  pr-validation-ubuntu:")
    pr_tests = workflow[tests_start:tests_end]
    guard_position = pr_tests.index(guard_command)
    materialize_position = pr_tests.index(materialize_guard)
    assert materialize_governance in pr_tests
    assert "cp -R ../trusted/core/harness" not in pr_tests
    harness_position = pr_tests.index(harness_command, materialize_position)

    assert guard_position < materialize_position < harness_position
    assert "working-directory: candidate" in pr_tests

def test_harness_configuration_has_explicit_step_kinds() -> None:
    payload = json.loads(HARNESS_CONFIG_PATH.read_text(encoding="utf-8"))

    assert payload["steps"]
    assert {step["kind"] for step in payload["steps"]} == {"check", "test"}
    assert all(step.get("quick") is True for step in payload["steps"])
