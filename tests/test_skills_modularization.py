"""Automated verification suite for HF-06: Skills Modularization (00-17).

Validates:
- Complete presence of all 18 canonical skills in .agents/skills/ and .claude/skills/.
- YAML frontmatter integrity (name, description).
- Mirror parity between .agents/skills/ and .claude/skills/.
- Removal of conflicting triggers (e.g. "segundo prompt", "2º prompt", prompt-waiting).
- Normative contract consumption and structured evidence generation.
- Strict enforcement of ReadinessGate and absence of policy bypass flags.
"""

from __future__ import annotations

import re
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENTS_SKILLS_DIR = ROOT / ".agents" / "skills"
CLAUDE_SKILLS_DIR = ROOT / ".claude" / "skills"

EXPECTED_SKILLS = [
    "00-continuous-self-improvement",
    "01-prime-intelligence",
    "02-plan-product-architecture",
    "03-model-router",
    "04-autonomous-piv-loop",
    "05-validation-harness",
    "06-adversarial-review",
    "07-build-dark-factory",
    "08-meta-skills-evolver",
    "09-local-audio-transcription",
    "10-topic-deep-research",
    "11-repo-code-scout",
    "12-daily-model-benchmark",
    "13-session-learning-pack",
    "14-speculative-model-racing",
    "15-anti-slop-content-engine",
    "16-visual-asset-studio",
    "17-specialized-test-subagent",
]


def _read_skill(base_dir: Path, skill_slug: str) -> str:
    path = base_dir / skill_slug / "SKILL.md"
    assert path.is_file(), f"Skill file not found: {path}"
    return path.read_text(encoding="utf-8")


def _extract_frontmatter(content: str) -> dict[str, str]:
    pattern = r"^---\r?\n(.*?)\r?\n---"
    match = re.search(pattern, content, re.DOTALL)
    assert match, "YAML frontmatter missing from SKILL.md"
    fm_lines = match.group(1).splitlines()
    data: dict[str, str] = {}
    current_key: str | None = None
    for line in fm_lines:
        if ":" in line and not line.startswith(" "):
            key, val = line.split(":", 1)
            current_key = key.strip()
            data[current_key] = val.strip()
        elif current_key and line.startswith(" "):
            data[current_key] += " " + line.strip()
    return data


@pytest.mark.parametrize("skill_slug", EXPECTED_SKILLS)
def test_skill_structure_and_frontmatter(skill_slug: str) -> None:
    content = _read_skill(AGENTS_SKILLS_DIR, skill_slug)
    frontmatter = _extract_frontmatter(content)
    assert "name" in frontmatter, f"Frontmatter 'name' missing in {skill_slug}"
    assert "description" in frontmatter, f"Frontmatter 'description' missing in {skill_slug}"
    assert len(frontmatter["name"]) > 0
    assert len(frontmatter["description"]) > 10


@pytest.mark.parametrize("skill_slug", EXPECTED_SKILLS)
def test_skill_mirror_parity(skill_slug: str) -> None:
    canonical = _read_skill(AGENTS_SKILLS_DIR, skill_slug)
    mirrored = _read_skill(CLAUDE_SKILLS_DIR, skill_slug)
    assert canonical == mirrored, (
        f"Mirror disparity in {skill_slug}: .claude/skills/{skill_slug}/SKILL.md does not match .agents/skills"
    )


@pytest.mark.parametrize("skill_slug", EXPECTED_SKILLS)
def test_no_conflicting_prompt_triggers(skill_slug: str) -> None:
    content = _read_skill(AGENTS_SKILLS_DIR, skill_slug)
    lowered = content.lower()
    assert "segundo prompt" not in lowered, f"Conflicting prompt trigger 'segundo prompt' found in {skill_slug}"
    assert "2º prompt" not in lowered, f"Conflicting prompt trigger '2º prompt' found in {skill_slug}"
    assert "2o prompt" not in lowered, f"Conflicting prompt trigger '2o prompt' found in {skill_slug}"


@pytest.mark.parametrize("skill_slug", EXPECTED_SKILLS)
def test_skills_declare_normative_sections(skill_slug: str) -> None:
    content = _read_skill(AGENTS_SKILLS_DIR, skill_slug)
    # Each modular skill must clearly define its contract boundaries
    has_inputs = "Inputs" in content or "Entradas" in content or "inputs" in content
    has_outputs = "Outputs" in content or "Saídas" in content or "outputs" in content
    has_actions = "Ações" in content or "Actions" in content or "Procedimento" in content or "procedimento" in content
    assert has_inputs, f"Skill {skill_slug} missing explicit Inputs section"
    assert has_outputs, f"Skill {skill_slug} missing explicit Outputs section"
    assert has_actions, f"Skill {skill_slug} missing explicit Actions / Procedure section"


def test_lifecycle_skills_reference_normative_stage_contracts() -> None:
    """Core workflow skills must explicitly reference HF-04 stage contracts."""
    skill_02 = _read_skill(AGENTS_SKILLS_DIR, "02-plan-product-architecture")
    assert "WorkflowHandoff" in skill_02
    assert "GrillRecord" in skill_02
    assert "ReadinessGate" in skill_02

    skill_04 = _read_skill(AGENTS_SKILLS_DIR, "04-autonomous-piv-loop")
    assert "WorkflowState" in skill_04
    assert "EnvironmentEvidence" in skill_04 or "EvidenceReceipt" in skill_04
    assert "ReadinessGate" in skill_04

    skill_05 = _read_skill(AGENTS_SKILLS_DIR, "05-validation-harness")
    assert "EnvironmentEvidence" in skill_05 or "EvidenceReceipt" in skill_05
    assert "ReadinessGate" in skill_05

    skill_06 = _read_skill(AGENTS_SKILLS_DIR, "06-adversarial-review")
    assert "EvidenceReceipt" in skill_06
    assert "VerificationContext" in skill_06 or "ReadinessGate" in skill_06


def test_no_skill_permits_gate_bypass() -> None:
    """Ensure no skill attempts to flexibilize rules or bypass ReadinessGate."""
    for skill_slug in EXPECTED_SKILLS:
        content = _read_skill(AGENTS_SKILLS_DIR, skill_slug)
        lowered = content.lower()
        assert "allow_delivery = true" not in lowered
        assert "bypass_gate" not in lowered
        assert "skip_verification = true" not in lowered
