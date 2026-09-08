from __future__ import annotations

import json
from pathlib import Path

from core.orchestrator.guard import audit_paths


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
HARNESS_CONFIG_PATH = PROJECT_ROOT / "harness.config.json"


def _workflow() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_all_validation_control_files_are_protected() -> None:
    controls = [
        "AGENTS.md",
        "MISSION.md",
        "FACTORY_RULES.md",
        "harness.config.json",
        ".github/workflows/ci.yml",
        "core/harness/runner.py",
        "core/orchestrator/guard.py",
        "tests/test_ci_policy.py",
        "tests/test_governance_guard.py",
        "tests/test_harness_contract.py",
    ]

    assert audit_paths(controls) == controls


def test_pull_request_policy_uses_base_owned_verifier_and_exact_shas() -> None:
    workflow = _workflow()

    assert "  pull_request:\n" in workflow
    assert "pull_request_target:" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.pull_request.head.sha" in workflow
    assert "path: trusted" in workflow
    assert "path: candidate" in workflow
    assert workflow.count("persist-credentials: false") >= 4

    trusted_policy = workflow[
        workflow.index("  trusted-pr-policy:") : workflow.index("  pr-validation:")
    ]
    assert "pip install" not in trusted_policy
    assert "runner.py" not in trusted_policy

    guard_command = "python ../trusted/core/orchestrator/guard.py"
    materialize_command = "cp -R ../trusted/core/harness/. core/harness/"
    harness_command = "python core/harness/runner.py --quick"
    validation_position = workflow.index("  pr-validation:")
    guard_position = workflow.index(guard_command, validation_position)
    materialize_position = workflow.index(materialize_command, validation_position)
    harness_position = workflow.index(harness_command, materialize_position)

    assert guard_position < materialize_position < harness_position
    assert "working-directory: candidate" in workflow


def test_harness_configuration_has_explicit_step_kinds() -> None:
    payload = json.loads(HARNESS_CONFIG_PATH.read_text(encoding="utf-8"))

    assert payload["steps"]
    assert {step["kind"] for step in payload["steps"]} == {"check", "test"}
    assert all(step.get("quick") is True for step in payload["steps"])
