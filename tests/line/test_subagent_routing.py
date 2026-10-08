"""USR-168: model policy for development subagents (Haiku 5.5 eligibility).

Pure-function tests: no network, no quota, no filesystem beyond the committed config and docs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from core.line import subagent_routing as sr
from core.line.subagent_routing import (
    SubagentModelDecision,
    SubagentRoutingConfig,
    SubagentTask,
    default_config,
    load_subagent_routing_config,
    parse_config,
    pick_subagent_model,
)

pytestmark = [pytest.mark.offline]

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_JSON = REPO_ROOT / ".factory" / "config" / "subagent_model_routing.json"

HAIKU_ID = "claude-haiku-5-5"
SONNET_ID = "claude-sonnet-5-5"
OPUS_ID = "claude-opus-5-5"


def _task(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "stage": "development",
        "task_type": "mechanical_edit",
        "complexity": "low",
        "files": ["core/line/example.py"],
        "expected_changed_lines": 20,
        "has_executable_acceptance": True,
        "ambiguity_resolved": True,
        "tags": [],
        "previous_haiku_failures": 0,
    }
    base.update(overrides)
    return base


def _pick(**overrides: Any) -> SubagentModelDecision:
    return pick_subagent_model(SubagentTask(**_task(**overrides)))


# ---------------------------------------------------------------------------
# Eligible case
# ---------------------------------------------------------------------------


def test_eligible_task_goes_to_haiku_with_sonnet_escalation() -> None:
    decision = _pick()
    assert decision.model == "haiku"
    assert decision.model_id == HAIKU_ID
    assert decision.eligible_for_haiku is True
    assert decision.reason_codes == [sr.HAIKU_ELIGIBLE]
    assert decision.escalate_to == "sonnet"


@pytest.mark.parametrize(
    "task_type",
    [
        "mechanical_edit",
        "docs_sync",
        "test_from_spec",
        "config_data",
        "boilerplate_from_template",
        "test_run_distill",
        "ledger_update",
    ],
)
def test_every_eligible_task_type_low_complexity(task_type: str) -> None:
    assert _pick(task_type=task_type).model == "haiku"


def test_limits_are_inclusive() -> None:
    decision = _pick(files=["a.py", "b.py", "c.py"], expected_changed_lines=150)
    assert decision.model == "haiku"


def test_accepts_plain_mapping_input() -> None:
    assert pick_subagent_model(_task()).model == "haiku"


# ---------------------------------------------------------------------------
# Complexity: medium only for docs_sync / test_run_distill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("task_type", ["docs_sync", "test_run_distill"])
def test_medium_complexity_allowed_only_for_docs_sync_and_test_run_distill(task_type: str) -> None:
    assert _pick(task_type=task_type, complexity="medium").model == "haiku"


@pytest.mark.parametrize(
    "task_type",
    ["mechanical_edit", "test_from_spec", "config_data", "boilerplate_from_template", "ledger_update"],
)
def test_medium_complexity_rejected_for_other_types(task_type: str) -> None:
    decision = _pick(task_type=task_type, complexity="medium")
    assert decision.model == "sonnet"
    assert sr.COMPLEXITY_NOT_ALLOWED in decision.reason_codes


@pytest.mark.parametrize("complexity", ["high", "critical"])
@pytest.mark.parametrize("task_type", ["docs_sync", "test_run_distill", "mechanical_edit"])
def test_high_and_critical_never_haiku(task_type: str, complexity: str) -> None:
    decision = _pick(task_type=task_type, complexity=complexity)
    assert decision.model == "sonnet"
    assert sr.COMPLEXITY_NOT_ALLOWED in decision.reason_codes


def test_task_type_outside_list_goes_to_sonnet() -> None:
    decision = _pick(task_type="feature_implementation")
    assert decision.model == "sonnet"
    assert decision.reason_codes == [sr.TASK_TYPE_NOT_ELIGIBLE]
    assert decision.eligible_for_haiku is False
    assert decision.escalate_to is None


# ---------------------------------------------------------------------------
# One test per prohibition / reason
# ---------------------------------------------------------------------------

FORBIDDEN_TAGS = [
    "security",
    "credentials",
    "auth",
    "payments",
    "concurrency",
    "locking",
    "transactions",
    "migration",
    "data_deletion",
    "public_contract",
    "routing",
    "quota",
    "flaky_test",
    "root_cause_debug",
    "architecture",
]


@pytest.mark.parametrize("tag", FORBIDDEN_TAGS)
def test_each_risk_tag_forces_sonnet(tag: str) -> None:
    decision = _pick(tags=[tag])
    assert decision.model == "sonnet"
    assert decision.model_id == SONNET_ID
    assert sr.RISK_TAG in decision.reason_codes


def test_risk_tag_match_is_case_insensitive_and_ignores_harmless_tags() -> None:
    assert _pick(tags=["SECURITY"]).model == "sonnet"
    assert _pick(tags=["cosmetic", "docs"]).model == "haiku"


@pytest.mark.parametrize("stage", ["grill", "review", "integration"])
def test_forbidden_stages_force_sonnet(stage: str) -> None:
    decision = _pick(stage=stage)
    assert decision.model == "sonnet"
    assert sr.STAGE_FORBIDDEN in decision.reason_codes
    assert decision.eligible_for_haiku is False


def test_planning_goes_to_opus_never_haiku() -> None:
    decision = _pick(stage="planning")
    assert decision.model == "opus"
    assert decision.model_id == OPUS_ID
    assert sr.OPUS_STAGE in decision.reason_codes
    assert decision.eligible_for_haiku is False
    assert decision.escalate_to is None


@pytest.mark.parametrize(
    "path",
    [
        "AGENTS.md",
        "FACTORY_RULES.md",
        "MISSION.md",
        "harness.config.json",
        ".github/workflows/ci.yml",
        ".agents/rules/01-core.md",
        "core/harness/runner.py",
        "core/orchestrator/guard.py",
        ".factory/holdout/case.json",
        "tests/test_governance_guard.py",
    ],
)
def test_protected_paths_force_sonnet(path: str) -> None:
    decision = _pick(files=["core/line/example.py", path])
    assert decision.model == "sonnet"
    assert sr.PROTECTED_PATH in decision.reason_codes


def test_protected_path_with_windows_separators() -> None:
    assert sr.PROTECTED_PATH in _pick(files=["core\\harness\\runner.py"]).reason_codes


def test_more_than_three_files_forces_sonnet() -> None:
    decision = _pick(files=["a.py", "b.py", "c.py", "d.py"])
    assert decision.model == "sonnet"
    assert sr.TOO_MANY_FILES in decision.reason_codes


def test_more_than_150_lines_forces_sonnet() -> None:
    decision = _pick(expected_changed_lines=151)
    assert decision.model == "sonnet"
    assert sr.TOO_MANY_LINES in decision.reason_codes


def test_missing_executable_acceptance_forces_sonnet() -> None:
    decision = _pick(has_executable_acceptance=False)
    assert decision.model == "sonnet"
    assert sr.NO_EXECUTABLE_ACCEPTANCE in decision.reason_codes


def test_pending_ambiguity_forces_sonnet() -> None:
    decision = _pick(ambiguity_resolved=False)
    assert decision.model == "sonnet"
    assert sr.AMBIGUITY_PENDING in decision.reason_codes


def test_previous_haiku_failure_forces_sonnet() -> None:
    decision = _pick(previous_haiku_failures=1)
    assert decision.model == "sonnet"
    assert sr.PRIOR_HAIKU_FAILURE in decision.reason_codes
    assert decision.escalate_to is None


def test_haiku_never_escalates_to_opus() -> None:
    for failures in (0, 1, 2, 5):
        decision = _pick(previous_haiku_failures=failures)
        assert decision.model != "opus"
        assert decision.escalate_to in (None, "sonnet")


def test_multiple_reasons_are_all_reported() -> None:
    decision = _pick(tags=["auth"], expected_changed_lines=999, ambiguity_resolved=False)
    assert {sr.RISK_TAG, sr.TOO_MANY_LINES, sr.AMBIGUITY_PENDING} <= set(decision.reason_codes)


# ---------------------------------------------------------------------------
# Fail-closed on invalid input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {},
        _task(complexity="trivial"),
        _task(expected_changed_lines=-1),
        _task(has_executable_acceptance="yes"),
        _task(ambiguity_resolved=None),
        _task(files="not-a-list"),
        _task(unknown_field=1),
        _task(stage=""),
        _task(previous_haiku_failures=-1),
    ],
)
def test_invalid_input_fails_closed_to_sonnet(bad: dict[str, Any]) -> None:
    decision = pick_subagent_model(bad)
    assert decision.model == "sonnet"
    assert decision.model_id == SONNET_ID
    assert decision.eligible_for_haiku is False
    assert decision.reason_codes == [sr.INVALID_INPUT]


def test_non_mapping_input_fails_closed() -> None:
    assert pick_subagent_model(None).model == "sonnet"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_repo_json_validates_and_matches_embedded_defaults() -> None:
    raw = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    parsed = parse_config(raw)
    assert parsed == default_config()
    assert load_subagent_routing_config() == default_config()
    assert parsed.models == {"haiku": HAIKU_ID, "sonnet": SONNET_ID, "opus": OPUS_ID}
    assert (parsed.max_files, parsed.max_changed_lines, parsed.max_haiku_attempts) == (3, 150, 1)


def test_missing_config_falls_back_to_defaults(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="core.line.subagent_routing"):
        cfg = load_subagent_routing_config(tmp_path / "absent.json")
    assert cfg == default_config()
    assert "not found" in caplog.text


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"models": {"haiku": "x"}}),
    ],
)
def test_invalid_config_falls_back_to_defaults(tmp_path: Path, content: str, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "cfg.json"
    path.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="core.line.subagent_routing"):
        cfg = load_subagent_routing_config(path)
    assert cfg == default_config()
    assert "invalid" in caplog.text


def _mutated(**changes: Any) -> dict[str, Any]:
    raw = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
    raw.update(changes)
    return raw


def test_config_cannot_relax_deny_lists(tmp_path: Path) -> None:
    for field in ("forbidden_tags", "forbidden_stages", "forbidden_autonomous_models"):
        path = tmp_path / f"{field}.json"
        path.write_text(json.dumps(_mutated(**{field: []})), encoding="utf-8")
        assert load_subagent_routing_config(path) == default_config()


def test_config_with_forbidden_model_id_is_rejected(tmp_path: Path) -> None:
    raw = _mutated(models={"haiku": "claude-fable", "sonnet": SONNET_ID, "opus": OPUS_ID})
    path = tmp_path / "fable.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    cfg = load_subagent_routing_config(path)
    assert cfg == default_config()
    with pytest.raises(ValueError):
        parse_config(raw)


def test_valid_custom_config_is_honoured(tmp_path: Path) -> None:
    raw = _mutated(limits={"max_files": 1, "max_changed_lines": 10, "max_haiku_attempts": 1})
    path = tmp_path / "tight.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    cfg = load_subagent_routing_config(path)
    assert cfg.max_files == 1
    task = SubagentTask(**_task(files=["a.py", "b.py"]))
    assert pick_subagent_model(task, cfg).model == "sonnet"
    assert pick_subagent_model(task, default_config()).model == "haiku"


def test_zero_haiku_attempts_disables_haiku() -> None:
    raw = _mutated(limits={"max_files": 3, "max_changed_lines": 150, "max_haiku_attempts": 0})
    cfg = parse_config(raw)
    assert pick_subagent_model(SubagentTask(**_task()), cfg).model == "sonnet"


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def test_forbidden_models_are_never_returned() -> None:
    cfg = default_config()
    forbidden = set(cfg.forbidden_autonomous_models)
    assert {"fable", "claude-fable", "astra", "gpt-6-astra"} <= forbidden
    for model_id in cfg.models.values():
        assert not any(entry in model_id.lower() for entry in forbidden)


def test_decision_never_returns_id_outside_config() -> None:
    cfg = default_config()
    allowed_ids = set(cfg.models.values())
    stages = ["development", "planning", "grill", "review", "integration", "test"]
    task_types = [*cfg.eligible_task_types, "feature_implementation"]
    complexities = ["low", "medium", "high", "critical"]
    for stage in stages:
        for task_type in task_types:
            for complexity in complexities:
                for tags in ([], ["security"]):
                    for failures in (0, 1):
                        decision = _pick(
                            stage=stage,
                            task_type=task_type,
                            complexity=complexity,
                            tags=tags,
                            previous_haiku_failures=failures,
                        )
                        assert decision.model_id in allowed_ids
                        assert decision.model_id == cfg.models[decision.model]
                        assert decision.eligible_for_haiku == (decision.model == "haiku")
                        if decision.model == "haiku":
                            assert stage not in cfg.forbidden_stages
                            assert not tags and failures == 0


def test_pick_is_pure_and_deterministic() -> None:
    task = SubagentTask(**_task(tags=["auth"]))
    first = pick_subagent_model(task)
    assert first == pick_subagent_model(task)
    assert task.tags == ["auth"]


def test_config_model_is_frozen() -> None:
    cfg = default_config()
    with pytest.raises(Exception):
        cfg.max_files = 99  # type: ignore[misc]
    assert isinstance(cfg, SubagentRoutingConfig)


# ---------------------------------------------------------------------------
# Consistency with skills / docs / agent definitions
# ---------------------------------------------------------------------------


def test_model_ids_match_agent_definition_skills_and_docs() -> None:
    cfg = default_config()
    test_runner = (REPO_ROOT / ".claude" / "agents" / "test-runner.md").read_text(encoding="utf-8")
    assert "claude-haiku-5-5" in test_runner
    assert f"model: {cfg.models['haiku']}" in test_runner
    assert "claude-3-5-haiku" not in test_runner

    router_skill = (REPO_ROOT / ".agents" / "skills" / "03-model-router" / "SKILL.md").read_text(encoding="utf-8")
    run_ticket_skill = (REPO_ROOT / ".agents" / "skills" / "19-run-ticket" / "SKILL.md").read_text(encoding="utf-8")
    test_skill = (REPO_ROOT / ".agents" / "skills" / "17-specialized-test-subagent" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for text in (router_skill, run_ticket_skill, test_skill):
        assert cfg.models["haiku"] in text
    assert "claude-3-5-haiku" not in test_skill
    assert "core/line/subagent_routing.py" in router_skill
    assert ".factory/config/subagent_model_routing.json" in router_skill

    claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "docs" / "MODEL_SELECTION_GUIDE.md").read_text(encoding="utf-8")
    assert cfg.models["haiku"] in claude_md
    assert cfg.models["haiku"] in guide
