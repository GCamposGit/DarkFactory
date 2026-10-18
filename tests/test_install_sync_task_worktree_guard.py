"""USR-240: install_sync_task.ps1 deve recusar rodar a partir de worktrees."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_sync_task.ps1"
SHELLS = [s for s in ("powershell", "pwsh") if shutil.which(s)]
PS = SHELLS[0] if SHELLS else None


def _run(shell: str, script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shutil.which(shell) or shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def test_guard_precedes_register_scheduled_task() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    guard = text.index("rev-parse")
    assert guard < text.index("Register-ScheduledTask")
    assert guard < text.index("New-ScheduledTaskAction")
    dry = text.index("if ($DryRun)")
    assert guard < dry
    first_cmdlet = min(text.index(c) for c in
                       ("New-ScheduledTaskAction", "New-ScheduledTaskTrigger",
                        "New-ScheduledTaskPrincipal", "New-ScheduledTaskSettingsSet",
                        "Register-ScheduledTask"))
    assert dry < first_cmdlet
    for marker in (".worktrees", ".claude", ".codex", "exit 2"):
        assert marker in text
    assert r"C:\dev\DarkFac\scripts\install_sync_task.ps1" in text


@pytest.mark.skipif(not SHELLS, reason="PowerShell indisponivel")
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("parts", [(".worktrees", "x"), (".claude", "worktrees", "x"),
                                   (".codex", "worktrees", "X")])
def test_refuses_inside_worktree_dirs(shell: str, parts: tuple[str, ...]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        scripts = Path(tmp).joinpath(*parts, "scripts")
        scripts.mkdir(parents=True)
        copy = scripts / "install_sync_task.ps1"
        shutil.copy(SCRIPT, copy)
        proc = _run(shell, copy, "-DryRun")  # -DryRun por seguranca; a guarda roda antes dele
        assert proc.returncode != 0
        assert "RECUSADO" in proc.stderr
        assert r"C:\dev\DarkFac\scripts\install_sync_task.ps1" in proc.stderr
        assert "registrada" not in proc.stdout


@pytest.mark.skipif(not SHELLS, reason="PowerShell indisponivel")
@pytest.mark.parametrize("shell", SHELLS)
def test_dry_run_passes_outside_worktree(shell: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        scripts = Path(tmp) / "repo" / "scripts"
        scripts.mkdir(parents=True)
        copy = scripts / "install_sync_task.ps1"
        shutil.copy(SCRIPT, copy)
        proc = _run(shell, copy, "-DryRun")
        assert proc.returncode == 0, proc.stderr
        assert "DRY-RUN" in proc.stdout
