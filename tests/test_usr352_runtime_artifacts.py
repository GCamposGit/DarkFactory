"""USR-352: artefatos de runtime nao bloqueiam run_ticket nem aparecem no git status."""

import subprocess
from pathlib import Path

import run_ticket

ROOT = Path(run_ticket.__file__).resolve().parent


def test_runtime_artifacts_recognized() -> None:
    for p in (
        ".factory/executions/active/exec-1.json",
        ".factory/hub_availability.json",
        ".factory/owner_actions/OA-059-shot.png",
        ".factory/owner_actions/OA-059-log.jsonl",
        ".factory/owner_actions/OA-059-fix.ps1",
        ".factory/reports/x.md",
    ):
        assert run_ticket.is_runtime_artifact(p), p
    for p in ("core/x.py", ".factory/owner_actions/OA-claude-md-pos-v0.md", ".factory/demands/demands.json"):
        assert not run_ticket.is_runtime_artifact(p), p


def test_gitignore_covers_runtime_paths() -> None:
    out = subprocess.run(
        ["git", "check-ignore", ".factory/executions/active/exec-1.json", ".factory/hub_availability.json",
         ".factory/owner_actions/OA-059-a.png"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert len(out.stdout.split()) == 3
