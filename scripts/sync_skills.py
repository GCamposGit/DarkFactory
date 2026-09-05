#!/usr/bin/env python3
"""
Skills Synchronizer (Universal Multi-Agent Compatibility)
Mirrors skills from .agents/skills/ into .claude/skills/ so the repository
works seamlessly in Antigravity, Claude Code, Cursor, and other agent environments.
"""

import os
import shutil
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT_DIR / ".agents" / "skills"
TARGET_DIR = ROOT_DIR / ".claude" / "skills"

def sync_skills():
    if not SOURCE_DIR.exists():
        print(f"[ERROR] Source skills directory not found: {SOURCE_DIR}")
        return

    os.makedirs(TARGET_DIR, exist_ok=True)
    synced_count = 0

    for skill_path in SOURCE_DIR.iterdir():
        if skill_path.is_dir():
            skill_name = skill_path.name
            dest_path = TARGET_DIR / skill_name
            if dest_path.exists():
                shutil.rmtree(dest_path)
            shutil.copytree(skill_path, dest_path)
            print(f"  [SYNCED] {skill_name} -> .claude/skills/{skill_name}")
            synced_count += 1

    print(f"\nSuccessfully synchronized {synced_count} skills to .claude/skills/.")

if __name__ == "__main__":
    sync_skills()
