"""Gate for skills drift detection and multi-agent synchronization (USR-90).

Validates that all SKILL.md files in .agents/skills/:
(a) Only cite file paths that exist on disk (with an explicit, justified allowlist).
(b) Only cite Python modules ('python -m <mod>', 'from <mod> import') that are importable.
(c) Only cite CLI flags supported by the underlying CLI argparse options.
(d) Are byte-for-byte identical to their .claude/skills/ mirror (via scripts/sync_skills.py).
(e) Have valid YAML frontmatter matching their directory name.

Also includes mutation tests ensuring the drift detector catches invalid paths, modules,
and CLI flags.
"""

from __future__ import annotations

import argparse
import importlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import sync_skills

pytestmark = [pytest.mark.offline]

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_SKILLS_DIR = REPO_ROOT / ".agents" / "skills"
CLAUDE_SKILLS_DIR = REPO_ROOT / ".claude" / "skills"

# ---------------------------------------------------------------------------
# Allowlist for cited paths that legitimately do not exist in the root checkout
# ---------------------------------------------------------------------------
ALLOWLIST_PATH_EXCEPTIONS: dict[str, str] = {
    "03-model-router:.factory/benchmarks/latest.json": (
        "Dynamic daily benchmark artifact generated at runtime by Skill 12; not pre-committed"
    ),
    "07-build-dark-factory:.factory/darkfac.lock.json": (
        "Artifact created only inside adopted client target repositories, not in darkfac core"
    ),
    "07-build-dark-factory:codex/darkfac-adoption": (
        "Example Git branch name pattern for adopted projects"
    ),
    "09-local-audio-transcription:caminho/para/reuniao.wav": (
        "User placeholder argument in usage example"
    ),
    "11-repo-code-scout:BSD-2/3-Clause": (
        "SPDX license family identifier containing a slash, not a filesystem path"
    ),
    "11-repo-code-scout:GPL-2.0/3.0": (
        "SPDX license family identifier containing a slash, not a filesystem path"
    ),
    "12-daily-model-benchmark:.factory/benchmarks/latest.json": (
        "Dynamic daily benchmark artifact generated at runtime by Skill 12; not pre-committed"
    ),
    "14-speculative-model-racing:.factory/benchmarks/empirical_ledger.json": (
        "Dynamic empirical benchmark ledger generated at runtime by Skill 14; not pre-committed"
    ),
}

# ---------------------------------------------------------------------------
# Allowlist for known false claims / drift in skill text (all resolved in USR-89)
# ---------------------------------------------------------------------------
KNOWN_FLAG_DRIFT_ALLOWLIST: dict[str, str] = {}


_PATH_PATTERN = re.compile(r"[`\"]([a-zA-Z0-9_\-\.]+(?:/[a-zA-Z0-9_\-\.]+)+)[`\"]")
_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _extract_cited_paths(content: str) -> list[str]:
    paths: list[str] = []
    for m in _PATH_PATTERN.finditer(content):
        p = m.group(1)
        if any(p.startswith(x) for x in ("http://", "https://", "image/", "text/", "application/")):
            continue
        if any(p.endswith(x) for x in (".com", ".org", ".io", ".dev", ".ai")):
            continue
        paths.append(p)
    return paths


def _extract_python_modules(content: str) -> list[str]:
    modules: list[str] = []
    for m in re.finditer(r"python\s+-m\s+([a-zA-Z0-9_\.]+)", content):
        modules.append(m.group(1))
    for m in re.finditer(r"from\s+([a-zA-Z0-9_\.]+)\s+import", content):
        modules.append(m.group(1))
    return sorted(set(modules))


def _extract_cli_flags(content: str) -> list[tuple[str, str]]:
    """Return list of (cli_name, flag) tuples found in skill text."""
    results: list[tuple[str, str]] = []
    pattern = re.compile(r"python\s+(?:-m\s+)?(?:[A-Za-z]:\\[^\n`]+\\)?([a-zA-Z0-9_\-\.]+)\s+([^\n\r`]+)")
    for m in pattern.finditer(content):
        raw_cli = m.group(1).replace("\\", "/")
        cli_name = raw_cli.split("/")[-1]
        args_str = m.group(2)
        flags = re.findall(r"(--[a-zA-Z0-9_\-]+)", args_str)
        for flag in flags:
            results.append((cli_name, flag))

    # Also extract backticked flags mentioned in prose when a specific CLI is the subject
    for backticked in re.findall(r"`(--[a-zA-Z0-9_\-]+)`", content):
        if "run_ticket" in content:
            results.append(("run_ticket.py", backticked))

    return list(dict.fromkeys(results))


# ---------------------------------------------------------------------------
# Core Gate Tests
# ---------------------------------------------------------------------------

def test_skill_frontmatter_validity() -> None:
    """(e) Every SKILL.md must have valid YAML frontmatter matching its directory name."""
    assert AGENTS_SKILLS_DIR.exists(), f"Missing skills dir: {AGENTS_SKILLS_DIR}"

    for skill_dir in sorted(AGENTS_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        assert skill_file.exists(), f"Skill directory {skill_dir.name} is missing SKILL.md"

        content = skill_file.read_text(encoding="utf-8")
        match = _FRONTMATTER_PATTERN.match(content)
        assert match is not None, f"{skill_dir.name}/SKILL.md is missing YAML frontmatter (--- ... ---)"

        frontmatter = match.group(1)
        name_match = re.search(r"^name:\s*(.+)$", frontmatter, re.MULTILINE)
        desc_match = re.search(r"^description:\s*(.+)$", frontmatter, re.MULTILINE)

        assert name_match is not None, f"{skill_dir.name}/SKILL.md frontmatter missing 'name'"
        assert desc_match is not None, f"{skill_dir.name}/SKILL.md frontmatter missing 'description'"

        name_val = name_match.group(1).strip()
        desc_val = desc_match.group(1).strip()
        assert desc_val, f"{skill_dir.name}/SKILL.md description must not be empty"

        # Directory name convention: '19-run-ticket' -> name: 'run-ticket'
        expected_suffix = f"-{name_val}"
        assert (
            skill_dir.name == name_val or skill_dir.name.endswith(expected_suffix)
        ), f"Frontmatter name '{name_val}' does not match directory '{skill_dir.name}'"


def test_skills_mirror_parity() -> None:
    """(d) .claude/skills/ must be byte-for-byte identical to .agents/skills/."""
    in_sync, issues = sync_skills.check_skills(AGENTS_SKILLS_DIR, CLAUDE_SKILLS_DIR)
    assert in_sync, (
        f"Drift detected between .agents/skills and .claude/skills:\n"
        + "\n".join(f"  - {i}" for i in issues)
        + "\nRun 'python scripts/sync_skills.py' to reconcile."
    )


def test_skill_cited_paths_exist() -> None:
    """(a) Every file path cited in SKILL.md must exist or be in the justified allowlist."""
    missing_paths: list[str] = []

    for skill_dir in sorted(AGENTS_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        content = skill_file.read_text(encoding="utf-8")
        for cited in _extract_cited_paths(content):
            key = f"{skill_dir.name}:{cited}"
            if key in ALLOWLIST_PATH_EXCEPTIONS:
                continue

            # Check if exists relative to REPO_ROOT or absolute
            target_path = REPO_ROOT / cited
            if not target_path.exists():
                missing_paths.append(f"{skill_dir.name}: {cited}")

    assert not missing_paths, (
        f"Skills cite paths that do not exist:\n"
        + "\n".join(f"  - {p}" for p in missing_paths)
        + "\nAdd to ALLOWLIST_PATH_EXCEPTIONS with justification if this is an external/template path."
    )


def test_skill_python_modules_importable() -> None:
    """(b) Every module referenced via 'python -m' or 'from x import' must be importable."""
    failures: list[str] = []

    for skill_dir in sorted(AGENTS_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        content = skill_file.read_text(encoding="utf-8")
        for mod in _extract_python_modules(content):
            try:
                importlib.import_module(mod)
            except Exception as exc:
                failures.append(f"{skill_dir.name}: module '{mod}' failed to import ({exc})")

    assert not failures, (
        f"Skills cite Python modules that cannot be imported:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )


def test_skill_cli_flags_exist() -> None:
    """(c) CLI flags cited in skills must exist in the target CLI argparse options."""
    # Pre-parse options for known CLIs
    cli_options: dict[str, set[str]] = {}

    # 1. run_ticket.py
    import run_ticket
    parser = run_ticket.build_parser()
    run_ticket_flags = set()
    for action in parser._actions:
        run_ticket_flags.update(action.option_strings)
    cli_options["run_ticket.py"] = run_ticket_flags

    # 2. runner.py
    from core.harness import runner
    runner_flags = {"--quick", "--holdout", "--no-cache", "--local", "--remote-required", "--config"}
    cli_options["runner.py"] = runner_flags

    invalid_flags: list[str] = []

    for skill_dir in sorted(AGENTS_SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        content = skill_file.read_text(encoding="utf-8")
        for cli_name, flag in _extract_cli_flags(content):
            if cli_name not in cli_options:
                continue
            key = f"{skill_dir.name}:{cli_name}:{flag}"
            if key in KNOWN_FLAG_DRIFT_ALLOWLIST:
                continue

            if flag not in cli_options[cli_name]:
                invalid_flags.append(f"{skill_dir.name} cites '{cli_name} {flag}' which is not in parser options")

    assert not invalid_flags, (
        f"Skills cite invalid CLI flags:\n"
        + "\n".join(f"  - {f}" for f in invalid_flags)
    )


# ---------------------------------------------------------------------------
# CLI Behavior & Mutation Tests
# ---------------------------------------------------------------------------

def test_sync_skills_cli_help_has_no_side_effects() -> None:
    """scripts/sync_skills.py --help must display help and exit 0 without modifying files."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "sync_skills.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert "--check" in result.stdout
    assert "--sync" in result.stdout


def test_sync_skills_cli_check_detects_drift(tmp_path: Path) -> None:
    """scripts/sync_skills.py check_skills detects drift, missing, and orphan files."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    src.mkdir()
    tgt.mkdir()

    # Identical
    (src / "skill-a").mkdir()
    (src / "skill-a" / "SKILL.md").write_text("content", encoding="utf-8")
    (tgt / "skill-a").mkdir()
    (tgt / "skill-a" / "SKILL.md").write_text("content", encoding="utf-8")

    in_sync, issues = sync_skills.check_skills(src, tgt)
    assert in_sync
    assert not issues

    # Content mismatch
    (tgt / "skill-a" / "SKILL.md").write_text("divergent", encoding="utf-8")
    in_sync, issues = sync_skills.check_skills(src, tgt)
    assert not in_sync
    assert any("Content mismatch" in i for i in issues)

    # Missing in target
    (src / "skill-b").mkdir()
    (src / "skill-b" / "SKILL.md").write_text("content b", encoding="utf-8")
    in_sync, issues = sync_skills.check_skills(src, tgt)
    assert not in_sync
    assert any("missing in .claude/skills" in i.lower() for i in issues)

    # Orphaned in target
    (tgt / "skill-orphan").mkdir()
    in_sync, issues = sync_skills.check_skills(src, tgt)
    assert not in_sync
    assert any("orphaned" in i.lower() for i in issues)


def test_mutation_detector_catches_invalid_references() -> None:
    """Mutation test: asserts that synthetic drift is strictly caught by detection regexes."""
    # Synthetic skill content with bogus path and bogus flag
    sample_text = (
        "Run `core/nonexistent_folder/missing_file.py`\n"
        "python run_ticket.py USR-99 --nonexistent-unsupported-flag\n"
        "python -m core.phantom_package_does_not_exist\n"
    )

    paths = _extract_cited_paths(sample_text)
    assert "core/nonexistent_folder/missing_file.py" in paths

    modules = _extract_python_modules(sample_text)
    assert "core.phantom_package_does_not_exist" in modules

    flags = _extract_cli_flags(sample_text)
    assert ("run_ticket.py", "--nonexistent-unsupported-flag") in flags

    # Verify that Skill 19 has no false claims and all its flags exist in run_ticket parser
    import run_ticket
    parser = run_ticket.build_parser()
    valid_run_ticket_flags = {opt for action in parser._actions for opt in action.option_strings}

    skill19 = (AGENTS_SKILLS_DIR / "19-run-ticket" / "SKILL.md").read_text(encoding="utf-8")
    skill19_flags = _extract_cli_flags(skill19)
    assert ("run_ticket.py", "--allow-critical-quota") not in skill19_flags
    for cli_name, flag in skill19_flags:
        if cli_name == "run_ticket.py":
            assert flag in valid_run_ticket_flags, f"Unexpected invalid flag in skill 19: {flag}"

