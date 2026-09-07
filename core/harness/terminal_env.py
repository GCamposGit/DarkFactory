"""Terminal environment validation, diagnostics, and defensive execution guard for DarkFac.

Prevents and remediates terminal startup errors, working directory hijacking by
PowerShell profiles, and relative path lookup failures across all agent harnesses.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from pydantic import BaseModel, Field

logger = logging.getLogger("core.harness.terminal_env")

# Critical anchor files that confirm repository root
ROOT_ANCHOR_FILES: tuple[str, ...] = (
    "MISSION.md",
    "FACTORY_RULES.md",
    "AGENTS.md",
    "harness.config.json",
    "core/harness/runner.py",
)


class TerminalEnvironmentStatus(BaseModel):
    """Detailed status report of the terminal environment and filesystem anchors."""

    project_root: str = Field(..., description="Canonical path to repository root")
    current_working_dir: str = Field(..., description="Current working directory at inspection")
    is_cwd_valid: bool = Field(..., description="True if cwd equals or is inside project root")
    is_at_project_root: bool = Field(..., description="True if cwd exactly matches project root")
    critical_files_accessible: bool = Field(..., description="True if all anchor files are readable")
    missing_critical_files: List[str] = Field(default_factory=list, description="Anchor files not found")
    detected_profile_issues: List[str] = Field(default_factory=list, description="Detected PowerShell profile risks")
    python_utf8_configured: bool = Field(..., description="True if UTF-8 environment is properly set")
    recommendations: List[str] = Field(default_factory=list, description="Remediation recommendations")


def get_project_root(start_path: Path | str | None = None) -> Path:
    """Locate the DarkFac repository root by traversing parent directories for anchor files."""
    if start_path is not None:
        current = Path(start_path).resolve()
    else:
        # Default starting from this file: core/harness/terminal_env.py -> 2 levels up
        current = Path(__file__).resolve().parent.parent.parent

    # Traverse upward up to 5 levels to verify anchor files
    check_dir = current
    for _ in range(6):
        if (check_dir / "MISSION.md").exists() and (check_dir / "core" / "harness").exists():
            return check_dir
        if check_dir.parent == check_dir:
            break
        check_dir = check_dir.parent

    return current


def ensure_clean_working_directory(target_root: Path | str | None = None) -> Path:
    """Ensure process working directory matches the repository root.

    If current working directory has drifted or was hijacked (e.g. by a PowerShell profile
    running `Set-Location "C:\\dev"`), this forcibly changes cwd back to target_root.
    """
    root = Path(target_root).resolve() if target_root else get_project_root()
    current_cwd = Path.cwd().resolve()

    if current_cwd != root:
        logger.warning(
            "CWD mismatch detected. Current: %s, Expected root: %s. Re-aligning working directory.",
            current_cwd,
            root,
        )
        os.chdir(str(root))

    # Set defensive environment variables
    os.environ["DARKFAC_ROOT"] = str(root)
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"

    return root


def detect_powershell_profile_interferences() -> list[str]:
    """Inspect known Windows PowerShell profile locations for hazardous unconditional directory shifts."""
    issues: list[str] = []

    if os.name != "nt":
        return issues

    profile_candidates: list[Path] = []

    # AllUsersCurrentHost and AllUsersAllHosts
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    profile_candidates.extend([
        Path(system_root) / r"System32\WindowsPowerShell\v1.0\Microsoft.PowerShell_profile.ps1",
        Path(system_root) / r"System32\WindowsPowerShell\v1.0\profile.ps1",
    ])

    # CurrentUser profiles
    onedrive_docs = Path.home() / "OneDrive" / "Documentos" / "WindowsPowerShell"
    regular_docs = Path.home() / "Documents" / "WindowsPowerShell"
    profile_candidates.extend([
        onedrive_docs / "Microsoft.PowerShell_profile.ps1",
        onedrive_docs / "profile.ps1",
        regular_docs / "Microsoft.PowerShell_profile.ps1",
        regular_docs / "profile.ps1",
    ])

    for profile_path in profile_candidates:
        if profile_path.exists():
            try:
                content = profile_path.read_text(encoding="utf-8", errors="replace")
                # Look for unconditional Set-Location or cd
                lines = [line.strip() for line in content.splitlines() if line.strip() and not line.strip().startswith("#")]
                for line in lines:
                    if re.search(r"\b(Set-Location|cd)\b", line, re.IGNORECASE):
                        # Check if guarded by an if statement
                        if "if (" not in line.lower() and "$pwd" not in line.lower():
                            issues.append(
                                f"Unconditional directory shift in {profile_path}: '{line}'. "
                                "Hijacks subprocess working directories unless -NoProfile is used."
                            )
            except Exception as exc:
                logger.debug("Could not inspect profile %s: %s", profile_path, exc)

    return issues


def validate_terminal_environment(root_dir: Path | str | None = None) -> TerminalEnvironmentStatus:
    """Perform a comprehensive health check of the terminal execution environment."""
    root = Path(root_dir).resolve() if root_dir else get_project_root()
    cwd = Path.cwd().resolve()

    is_at_root = cwd == root
    is_cwd_valid = is_at_root or (root in cwd.parents)

    missing_anchors: list[str] = []
    for anchor in ROOT_ANCHOR_FILES:
        anchor_path = root / anchor
        if not anchor_path.exists():
            missing_anchors.append(anchor)

    profile_issues = detect_powershell_profile_interferences()

    python_utf8 = (
        os.environ.get("PYTHONIOENCODING", "").lower() == "utf-8"
        or os.environ.get("PYTHONUTF8") == "1"
        or (hasattr(sys.stdout, "encoding") and sys.stdout.encoding.lower() in ("utf-8", "utf8"))
    )

    recommendations: list[str] = []
    if not is_at_root:
        recommendations.append(f"Execute `ensure_clean_working_directory()` to anchor cwd at '{root}'.")
    if profile_issues:
        recommendations.append(
            "Always invoke PowerShell with `-NoProfile -NonInteractive -ExecutionPolicy Bypass` "
            "to prevent system profiles from hijacking the working directory."
        )
    if missing_anchors:
        recommendations.append(f"Anchor files missing: {', '.join(missing_anchors)}. Verify repository integrity.")
    if not python_utf8:
        recommendations.append("Set environment variables PYTHONIOENCODING=utf-8 and PYTHONUTF8=1.")

    return TerminalEnvironmentStatus(
        project_root=str(root),
        current_working_dir=str(cwd),
        is_cwd_valid=is_cwd_valid,
        is_at_project_root=is_at_root,
        critical_files_accessible=len(missing_anchors) == 0,
        missing_critical_files=missing_anchors,
        detected_profile_issues=profile_issues,
        python_utf8_configured=bool(python_utf8),
        recommendations=recommendations,
    )


def wrap_powershell_command(
    command_str: str,
    *,
    use_pwsh: bool = False,
    extra_flags: Sequence[str] | None = None,
) -> list[str]:
    """Wrap a command string into a hardened PowerShell invocation that bypasses profile directory overrides."""
    executable = "pwsh.exe" if use_pwsh else "powershell.exe"
    base_args = [
        executable,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
    ]
    if extra_flags:
        base_args.extend(extra_flags)

    base_args.extend(["-Command", command_str])
    return base_args


def run_guarded_command(
    cmd: list[str] | str,
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60.0,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Execute a command guaranteed to run from the project root with UTF-8 encoding and sanitized environment."""
    root = ensure_clean_working_directory(cwd)
    work_dir = Path(cwd).resolve() if cwd else root

    merged_env = os.environ.copy()
    merged_env["DARKFAC_ROOT"] = str(root)
    merged_env["PYTHONIOENCODING"] = "utf-8"
    merged_env["PYTHONUTF8"] = "1"
    if env:
        merged_env.update(env)

    return subprocess.run(
        cmd,
        cwd=str(work_dir),
        env=merged_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=check,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI diagnostics and remediation entrypoint."""
    parser = argparse.ArgumentParser(description="Terminal Environment Guard for DarkFac")
    parser.add_argument("--check", action="store_true", help="Perform terminal environment validation check")
    parser.add_argument("--fix", action="store_true", help="Remediate working directory and environment variables")
    parser.add_argument("--json", action="store_true", help="Output status report as JSON")
    args = parser.parse_args(argv)

    if args.fix:
        ensure_clean_working_directory()
        print("[TERMINAL_ENV_FIXED] Working directory anchored and UTF-8 environment set.")

    status = validate_terminal_environment()

    if args.json:
        print(status.model_dump_json(indent=2))
    else:
        if status.is_at_project_root and status.critical_files_accessible:
            print("[TERMINAL_ENV_PASS] Terminal environment is healthy and anchored.")
        else:
            print("[TERMINAL_ENV_WARN] Terminal environment has warnings or misalignments.")
        print(f"Project Root : {status.project_root}")
        print(f"Current CWD  : {status.current_working_dir}")
        print(f"Anchor Files : {'All accessible' if status.critical_files_accessible else f'Missing {status.missing_critical_files}'}")
        if status.detected_profile_issues:
            print("Profile Warnings:")
            for issue in status.detected_profile_issues:
                print(f"  - {issue}")
        if status.recommendations:
            print("Recommendations:")
            for rec in status.recommendations:
                print(f"  - {rec}")

    return 0 if (status.is_cwd_valid and status.critical_files_accessible) else 1


if __name__ == "__main__":
    sys.exit(main())
