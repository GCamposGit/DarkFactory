"""Reachability contract test for USR-AUTO: Bug em inicialização do terminal.

Verifies that:
1. Terminal environment validation detects and remediates directory hijacking.
2. PowerShell profile interference analysis properly identifies unconditional Set-Location risks.
3. PowerShell commands are safely wrapped with -NoProfile to guarantee working directory preservation.
4. First-read inventory files (MISSION.md, FACTORY_RULES.md, AGENTS.md, runner.py) are accessible.
5. scripts/init_terminal.ps1 executes successfully and outputs the deterministic [TERMINAL_INIT_OK] marker.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.harness.terminal_env import (
    ROOT_ANCHOR_FILES,
    detect_powershell_profile_interferences,
    ensure_clean_working_directory,
    get_project_root,
    run_guarded_command,
    validate_terminal_environment,
    wrap_powershell_command,
)


def test_get_project_root_locates_darkfac() -> None:
    root = get_project_root()
    assert root.exists()
    assert (root / "MISSION.md").exists()
    assert (root / "core" / "harness" / "runner.py").exists()


def test_ensure_clean_working_directory_anchors_root() -> None:
    root = get_project_root()
    original_cwd = Path.cwd()

    try:
        # Intentionally change directory to parent if possible, or verify anchoring
        parent_dir = root.parent
        if parent_dir.exists():
            os.chdir(str(parent_dir))
            assert Path.cwd().resolve() == parent_dir.resolve()

        # Call ensure_clean_working_directory
        anchored = ensure_clean_working_directory(root)
        assert anchored.resolve() == root.resolve()
        assert Path.cwd().resolve() == root.resolve()
        assert os.environ.get("DARKFAC_ROOT") == str(root)
        assert os.environ.get("PYTHONIOENCODING") == "utf-8"
        assert os.environ.get("PYTHONUTF8") == "1"
    finally:
        # Restore cwd to root
        os.chdir(str(root))


def test_validate_terminal_environment_healthy() -> None:
    root = get_project_root()
    ensure_clean_working_directory(root)
    status = validate_terminal_environment(root)

    assert status.is_at_project_root is True
    assert status.is_cwd_valid is True
    assert status.critical_files_accessible is True
    assert len(status.missing_critical_files) == 0
    assert status.project_root == str(root)


def test_first_read_inventory_files_accessible() -> None:
    """Proves that initial inventory files read by agents are fully readable without terminal error."""
    root = get_project_root()
    for rel_path in ROOT_ANCHOR_FILES:
        target = root / rel_path
        assert target.exists(), f"Anchor file missing: {rel_path}"
        content = target.read_text(encoding="utf-8", errors="replace")
        assert len(content.strip()) > 0, f"Anchor file is empty: {rel_path}"


def test_wrap_powershell_command_includes_noprofile() -> None:
    cmd_str = "Get-Content MISSION.md"
    wrapped = wrap_powershell_command(cmd_str)

    assert "-NoProfile" in wrapped
    assert "-NonInteractive" in wrapped
    assert "-ExecutionPolicy" in wrapped
    assert "Bypass" in wrapped
    assert "-Command" in wrapped
    assert cmd_str in wrapped


def test_detect_powershell_profile_interferences() -> None:
    """Inspect profile warnings. If on Windows, profile scanner must run without crashing."""
    issues = detect_powershell_profile_interferences()
    assert isinstance(issues, list)
    if os.name == "nt":
        # On this machine, System32 profile has unconditional Set-Location
        system_profile = Path(os.environ.get("SystemRoot", r"C:\Windows")) / r"System32\WindowsPowerShell\v1.0\Microsoft.PowerShell_profile.ps1"
        if system_profile.exists():
            content = system_profile.read_text(encoding="utf-8", errors="replace")
            if "Set-Location" in content and "if (" not in content.lower():
                assert any("Set-Location" in issue for issue in issues)


def test_run_guarded_command() -> None:
    root = get_project_root()
    cmd = [sys.executable, "-c", "import os, sys; print('CWD:', os.getcwd()); print('UTF8:', sys.flags.utf8_mode)"]
    res = run_guarded_command(cmd, cwd=root)

    assert res.returncode == 0
    assert str(root) in res.stdout


@pytest.mark.skipif(os.name != "nt", reason="PowerShell script test applicable on Windows")
def test_init_terminal_script_execution() -> None:
    root = get_project_root()
    script_path = root / "scripts" / "init_terminal.ps1"
    assert script_path.exists()

    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        "-CheckOnly",
    ]

    res = subprocess.run(
        cmd,
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )

    assert res.returncode == 0, f"init_terminal.ps1 failed with stderr: {res.stderr}"
    assert "[TERMINAL_INIT_OK]" in res.stdout
