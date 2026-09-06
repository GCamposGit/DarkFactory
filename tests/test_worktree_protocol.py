"""Regression contract for DF-23 worktree instructions."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = Path("skills/04-autonomous-piv-loop/SKILL.md")
REFERENCE = Path("skills/04-autonomous-piv-loop/references/worktree-parallelism.md")


def _read(tree: str, relative: Path) -> str:
    return (ROOT / tree / relative).read_text(encoding="utf-8")


def test_worktree_protocol_is_complete_and_mirrored() -> None:
    canonical_skill = _read(".agents", SKILL)
    mirrored_skill = _read(".claude", SKILL)
    canonical_protocol = _read(".agents", REFERENCE)
    mirrored_protocol = _read(".claude", REFERENCE)

    assert canonical_skill == mirrored_skill
    assert canonical_protocol == mirrored_protocol
    assert "references/worktree-parallelism.md" in canonical_skill

    controls = [f"WT-{number:02d}" for number in range(1, 13)]
    headings = [
        line.split(" — ", 1)[0].removeprefix("### ")
        for line in canonical_protocol.splitlines()
        if line.startswith("### WT-")
    ]
    assert headings == controls

    normalized_protocol = " ".join(canonical_protocol.split())
    required_evidence = (
        "startingState: working-tree",
        "dirty-state",
        "fencing token",
        "heartbeat periódico",
        "lock de ownership",
        "python -m pytest --version",
        "commit seletivo",
        "Task sem resposta final",
        "escritor ativo",
        "RCA antes do retry",
        "ordem de dependência",
        "alcançável pela branch de integração",
    )
    assert all(evidence in normalized_protocol for evidence in required_evidence)

    roadmap = (ROOT / "docs" / "DEVELOPMENT_PLAN_2026-09-05.md").read_text(encoding="utf-8")
    assert roadmap.count("| DF-23 |") == 1
