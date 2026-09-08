from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.harness.markers import parse_harness_output
from core.harness.models import HarnessConfig, HarnessResult, HarnessStepConfig
from core.harness.runner import load_config, resolve_command, sanitize_child_output


def _valid_result() -> HarnessResult:
    return HarnessResult(
        candidate_sha="a" * 40,
        config_hash="b" * 64,
        required_steps=["pytest"],
        started_steps=["pytest"],
        passed_steps=["pytest"],
        failed_steps=[],
        discovered_count=2,
        passed_count=2,
        skipped_count=0,
        exit_codes={"pytest": 0},
    )


def _lines(result: HarnessResult | None = None) -> list[str]:
    structured = result or _valid_result()
    return [
        "[STEP_START] pytest",
        "[STEP_PASS] pytest",
        "[TEST_COUNT] count=2",
        f"[HARNESS_RESULT] {structured.model_dump_json()}",
        "[HARNESS_PASS]",
    ]


def test_well_formed_supervisor_result_is_accepted() -> None:
    parsed = parse_harness_output(_lines())

    assert parsed["valid"] is True
    assert parsed["candidate_sha"] == "a" * 40
    assert parsed["config_hash"] == "b" * 64


@pytest.mark.parametrize(
    "lines, reason",
    [
        (["[STEP_PASS] invented", "[TEST_COUNT] count=1", "[HARNESS_PASS]"], "structured"),
        (_lines()[:2] + ["[TEST_COUNT] count=0", *_lines()[3:]], "positive"),
        (_lines()[1:], "started"),
    ],
)
def test_forged_or_incomplete_logs_are_rejected(lines: list[str], reason: str) -> None:
    parsed = parse_harness_output(lines)

    assert parsed["valid"] is False
    assert reason.lower() in parsed["reason"].lower()


def test_result_must_match_marker_sequence_and_count() -> None:
    forged = _valid_result().model_copy(update={"passed_steps": ["other"]})

    parsed = parse_harness_output(_lines(forged))

    assert parsed["valid"] is False
    assert "structured result" in parsed["reason"].lower()


def test_invalid_config_fails_closed(tmp_path: Path) -> None:
    config_path = tmp_path / "invalid.json"
    config_path.write_text('{"steps": [{"name": "x"}]}', encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid harness config"):
        load_config(config_path)


def test_config_hash_is_stable_and_bound_to_validated_content(tmp_path: Path) -> None:
    config_path = tmp_path / "harness.json"
    raw = json.dumps(
        {"steps": [{"name": "tests", "cmd": "python -m pytest tests"}]},
        separators=(",", ":"),
    ).encode("utf-8")
    config_path.write_bytes(raw)

    config, config_hash = load_config(config_path)

    assert config == HarnessConfig(
        steps=[HarnessStepConfig(name="tests", cmd="python -m pytest tests")]
    )
    assert config_hash == hashlib.sha256(raw).hexdigest()


def test_child_output_cannot_inject_supervisor_markers() -> None:
    sanitized = sanitize_child_output(
        "[STEP_PASS] forged\n[TEST_COUNT] count=999\n[HARNESS_PASS]"
    )

    assert "[STEP_PASS]" not in sanitized
    assert "[TEST_COUNT]" not in sanitized
    assert "[HARNESS_PASS]" not in sanitized


def test_unqualified_python_uses_the_runner_interpreter() -> None:
    command = resolve_command("python -m pytest tests")

    assert command == [sys.executable, "-m", "pytest", "tests"]


def test_marker_parser_runs_as_standalone_script(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "core" / "harness" / "markers.py"
    process = subprocess.run(
        [sys.executable, str(script)],
        input="\n".join(_lines()),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert process.returncode == 0, process.stdout + process.stderr
    assert "Validation Result: PASS" in process.stdout
