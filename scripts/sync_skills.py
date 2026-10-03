#!/usr/bin/env python3
"""Skills Synchronizer (Universal Multi-Agent Compatibility).

Mirrors skills from .agents/skills/ into .claude/skills/ so the repository
works seamlessly in Antigravity, Claude Code, Cursor, and other agent environments.
Supports read-only drift verification via --check and side-effect-free --help.
"""

from __future__ import annotations

import argparse
import filecmp
import os
import shutil
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT_DIR / ".agents" / "skills"
TARGET_DIR = ROOT_DIR / ".claude" / "skills"


def check_skills(
    source_dir: Path = SOURCE_DIR,
    target_dir: Path = TARGET_DIR,
) -> tuple[bool, list[str]]:
    """Verify byte-for-byte parity between source and target skill directories.

    Returns (is_in_sync, list_of_discrepancies).
    """
    discrepancies: list[str] = []

    if not source_dir.exists():
        return False, [f"Source skills directory not found: {source_dir}"]
    if not target_dir.exists():
        return False, [f"Target skills directory not found: {target_dir}"]

    source_skills = {p.name: p for p in source_dir.iterdir() if p.is_dir()}
    target_skills = {p.name: p for p in target_dir.iterdir() if p.is_dir()}

    # 1. Missing skills in target
    for name in sorted(source_skills.keys() - target_skills.keys()):
        discrepancies.append(f"Skill missing in .claude/skills/: {name}")

    # 2. Orphaned skills in target
    for name in sorted(target_skills.keys() - source_skills.keys()):
        discrepancies.append(f"Orphaned skill in .claude/skills/: {name}")

    # 3. Content discrepancies in shared skills
    for name in sorted(source_skills.keys() & target_skills.keys()):
        s_skill = source_skills[name]
        t_skill = target_skills[name]

        s_files = {p.relative_to(s_skill): p for p in s_skill.rglob("*") if p.is_file()}
        t_files = {p.relative_to(t_skill): p for p in t_skill.rglob("*") if p.is_file()}

        for rel in sorted(s_files.keys() - t_files.keys()):
            discrepancies.append(f"File missing in target: {name}/{rel.as_posix()}")
        for rel in sorted(t_files.keys() - s_files.keys()):
            discrepancies.append(f"Orphaned file in target: {name}/{rel.as_posix()}")

        for rel in sorted(s_files.keys() & t_files.keys()):
            s_file = s_files[rel]
            t_file = t_files[rel]
            if not filecmp.cmp(s_file, t_file, shallow=False):
                discrepancies.append(f"Content mismatch: {name}/{rel.as_posix()}")

    return len(discrepancies) == 0, discrepancies


def sync_skills(
    source_dir: Path = SOURCE_DIR,
    target_dir: Path = TARGET_DIR,
) -> int:
    """Synchronize source skills to target directory. Returns count of synced skills."""
    if not source_dir.exists():
        print(f"[ERROR] Source skills directory not found: {source_dir}", file=sys.stderr)
        return 0

    os.makedirs(target_dir, exist_ok=True)
    synced_count = 0

    # Clean orphaned skills in target
    source_names = {p.name for p in source_dir.iterdir() if p.is_dir()}
    for target_skill in target_dir.iterdir():
        if target_skill.is_dir() and target_skill.name not in source_names:
            shutil.rmtree(target_skill)
            print(f"  [REMOVED ORPHAN] {target_skill.name} from .claude/skills/")

    for skill_path in source_dir.iterdir():
        if skill_path.is_dir():
            skill_name = skill_path.name
            dest_path = target_dir / skill_name
            if dest_path.exists():
                shutil.rmtree(dest_path)
            shutil.copytree(skill_path, dest_path)
            print(f"  [SYNCED] {skill_name} -> .claude/skills/{skill_name}")
            synced_count += 1

    print(f"\nSuccessfully synchronized {synced_count} skills to .claude/skills/.")
    return synced_count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize or verify skills from .agents/skills/ into .claude/skills/."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check parity between .agents/skills/ and .claude/skills/ (read-only; exits 1 on drift).",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Synchronize skills (default if --check is not specified).",
    )

    args = parser.parse_args(argv)

    if args.check:
        in_sync, issues = check_skills()
        if in_sync:
            print("[SYNC_CHECK_PASS] .claude/skills/ is fully synchronized with .agents/skills/.")
            return 0
        else:
            print("[SYNC_CHECK_FAIL] Skill drift detected between .agents/skills/ and .claude/skills/:", file=sys.stderr)
            for issue in issues:
                print(f"  - {issue}", file=sys.stderr)
            return 1

    # Default action: sync
    sync_skills()
    return 0


if __name__ == "__main__":
    sys.exit(main())
