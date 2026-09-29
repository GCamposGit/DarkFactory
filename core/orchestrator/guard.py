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

import fnmatch
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

PROTECTED_PATTERNS: tuple[str, ...] = (
    "AGENTS.md",
    "MISSION.md",
    "FACTORY_RULES.md",
    "FACTORY_GOVERNANCE.md",
    "harness.config.json",
    ".github/workflows/*",
    ".agents/rules/*",
    "core/harness/*",
    "core/orchestrator/guard.py",
    ".factory/holdout/*",
    "tests/test_ci_policy.py",
    "tests/test_governance_guard.py",
    "tests/test_harness_contract.py",
)


class GitQueryError(RuntimeError):
    """A fail-closed error raised when repository state cannot be trusted."""

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str],
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        self.command = tuple(command)
        self.returncode = returncode
        self.stderr = stderr.strip()
        detail = f"{message} (exit={returncode})" if returncode is not None else message
        if self.stderr:
            detail = f"{detail}: {self.stderr}"
        super().__init__(detail)


def _run_git(arguments: Sequence[str], repository: Path) -> subprocess.CompletedProcess[str]:
    command = ["git", *arguments]
    try:
        result = subprocess.run(
            command,
            cwd=repository,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise GitQueryError(
            "git is unavailable",
            command=command,
            stderr=str(exc),
        ) from exc
    if result.returncode != 0:
        raise GitQueryError(
            "git command failed",
            command=command,
            returncode=result.returncode,
            stderr=result.stderr,
        )
    return result


def _parse_name_status(output: str) -> list[str]:
    fields = [field for field in output.split("\0") if field]
    paths: list[str] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        if index + path_count > len(fields):
            raise GitQueryError(
                "malformed git diff output",
                command=("git", "diff", "--name-status"),
            )
        paths.extend(fields[index : index + path_count])
        index += path_count
    return paths

def get_modified_files(
    base_ref: str = "HEAD",
    *,
    repository: Path | None = None,
) -> list[str]:
    """Return tracked and untracked paths changed relative to a valid base ref.

    Git lookup failures raise :class:`GitQueryError`, ensuring callers block
    instead of treating an unknown repository state as a clean tree.
    """
    repo_path = repository if repository is not None else Path.cwd()
    verify_arguments = ["rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"]
    try:
        base_sha = _run_git(verify_arguments, repo_path).stdout.strip()
    except GitQueryError as exc:
        if exc.returncode is not None:
            raise GitQueryError(
                f"invalid base ref: {base_ref}",
                command=exc.command,
                returncode=exc.returncode,
                stderr=exc.stderr,
            ) from exc
        raise
    if not base_sha:
        raise GitQueryError("invalid base ref: empty object id", command=verify_arguments)

    diff = _run_git(
        ["diff", "--name-status", "-z", "--find-renames", base_sha, "--"],
        repo_path,
    )
    untracked = _run_git(
        ["ls-files", "--others", "--exclude-standard", "-z", "--"],
        repo_path,
    )
    paths = _parse_name_status(diff.stdout)
    paths.extend(path for path in untracked.stdout.split("\0") if path)
    return sorted(set(paths))

def audit_paths(file_list: Sequence[str]) -> list[str]:
    """Returns any files in file_list that violate protected patterns."""
    violations = []
    for filepath in file_list:
        norm_path = filepath.replace("\\", "/")
        for pattern in PROTECTED_PATTERNS:
            if fnmatch.fnmatch(norm_path, pattern) or fnmatch.fnmatch(os.path.basename(norm_path), pattern):
                violations.append(filepath)
                break
    return violations

def is_governance_evolution_authorized() -> bool:
    """Check if governance modification is explicitly authorized via env flag or verified ticket."""
    return os.environ.get("DARKFAC_ALLOW_GOVERNANCE_EVOLUTION", "").strip().lower() in ("1", "true", "yes")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    base_ref = "HEAD"
    repo_path: Path | None = None
    if arguments:
        if "--repo" in arguments:
            idx = arguments.index("--repo")
            if idx + 1 < len(arguments):
                repo_path = Path(arguments[idx + 1])
                arguments = arguments[:idx] + arguments[idx + 2 :]
        if arguments:
            base_ref = arguments[0]
    try:
        if repo_path is not None:
            modified = get_modified_files(base_ref, repository=repo_path)
        else:
            modified = get_modified_files(base_ref)
    except GitQueryError as exc:
        print(f"[GUARD ERROR] Unable to verify repository state: {exc}", file=sys.stderr)
        return 1
    violations = audit_paths(modified)

    if violations:
        if is_governance_evolution_authorized():
            print(f"[GUARD PASS] Governance modifications detected ({len(violations)} files), authorized by DARKFAC_ALLOW_GOVERNANCE_EVOLUTION.")
            return 0

        print("==================================================")
        print("GUARD VIOLATION: Agent attempted to modify protected governance files!")
        print("==================================================")
        for v in violations:
            print(f"  [BLOCKED] {v}")
        print("\nThese files can ONLY be modified by a direct human commit.")
        return 1

    print("[GUARD PASS] No protected governance paths modified.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
