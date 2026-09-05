#!/usr/bin/env python3
"""
Dark Factory Guardrail & Protected Path Auditor
Ensures autonomous agents NEVER tamper with the rules they are judged by:
- MISSION.md (Scope & Non-Goals)
- FACTORY_RULES.md (Autonomous operating rules)
- Protected test harnesses and gates

If an agent attempts to edit any protected path in a PR or working tree,
the guard fails with exit code 1 immediately.
"""

import sys
import os
import subprocess
import fnmatch
from typing import List

PROTECTED_PATTERNS = [
    "MISSION.md",
    "FACTORY_RULES.md",
    "FACTORY_GOVERNANCE.md",
    ".agents/rules/*",
    "core/harness/*",
    "core/orchestrator/guard.py",
    ".factory/holdout/*"
]

def get_modified_files(base_ref: str = "HEAD") -> List[str]:
    """Gets list of modified files from git diff or staging."""
    try:
        # Check uncommitted staged/unstaged changes
        res = subprocess.run(
            ["git", "diff", "--name-only", base_ref],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False
        )
        files = res.stdout.strip().splitlines()
        # Also check untracked or staged if base_ref is HEAD
        res_cached = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False
        )
        files.extend(res_cached.stdout.strip().splitlines())
        return list(set(f.strip() for f in files if f.strip()))
    except Exception as e:
        print(f"[ERROR] Failed to query git diff: {e}", file=sys.stderr)
        return []

def audit_paths(file_list: List[str]) -> List[str]:
    """Returns any files in file_list that violate protected patterns."""
    violations = []
    for filepath in file_list:
        norm_path = filepath.replace("\\", "/")
        for pattern in PROTECTED_PATTERNS:
            if fnmatch.fnmatch(norm_path, pattern) or fnmatch.fnmatch(os.path.basename(norm_path), pattern):
                violations.append(filepath)
                break
    return violations

def main():
    base_ref = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    modified = get_modified_files(base_ref)
    violations = audit_paths(modified)

    if violations:
        print("==================================================")
        print("GUARD VIOLATION: Agent attempted to modify protected governance files!")
        print("==================================================")
        for v in violations:
            print(f"  [BLOCKED] {v}")
        print("\nThese files can ONLY be modified by a direct human commit.")
        sys.exit(1)
    else:
        print("[GUARD PASS] No protected governance paths modified.")
        sys.exit(0)

if __name__ == "__main__":
    main()
