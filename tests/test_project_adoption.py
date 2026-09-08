from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from core.adoption.models import FileAction
from core.adoption.service import (
    AdoptionBlockedError,
    apply_adoption,
    initialize_project,
    inspect_project,
    plan_adoption,
    prepare_adoption_worktree,
    prepare_task,
    verify_adoption,
)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=check,
    )


def _initialize_repository(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "user.name", "DarkFac Test")
    _git(root, "config", "user.email", "darkfac-test@local")


def _commit_all(root: Path, message: str = "test baseline") -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    root = tmp_path / "factory-source"
    _initialize_repository(root)
    (root / "core").mkdir()
    (root / "core" / "__init__.py").write_text("", encoding="utf-8")
    (root / "core" / "show_root.py").write_text(
        "from core.paths import project_root\nprint(project_root())\n",
        encoding="utf-8",
    )
    (root / "core" / "paths.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def project_root():\n"
        "    return Path(os.environ.get('DARKFAC_PROJECT_ROOT', Path(__file__).resolve().parent.parent)).resolve()\n",
        encoding="utf-8",
    )
    skill = root / ".agents" / "skills" / "example" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Run `python -m core.show_root` and `python core/show_root.py`.\n", encoding="utf-8")
    docs = root / "docs"
    docs.mkdir()
    for name in ("DARK_FACTORY_PLAYBOOK.md", "HARNESS_INTEROP.md", "MODEL_SELECTION_GUIDE.md"):
        (docs / name).write_text(f"# {name}\n", encoding="utf-8")
    _commit_all(root, "factory source")
    return root


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    root = tmp_path / "consumer"
    _initialize_repository(root)
    (root / "README.md").write_text("# Consumer\n", encoding="utf-8")
    (root / ".gitignore").write_text(".claude/\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# Product rules\n\nKeep this text.\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[tool.ruff]\n[tool.pyright]\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    _commit_all(root)
    return root


def test_inspection_discovers_stack_without_reading_product_data(target_repo: Path) -> None:
    inspection = inspect_project(target_repo)
    assert inspection.git.clean
    assert inspection.stack.ecosystems == ("python",)
    assert inspection.stack.validation_commands == (
        "python -m pyright",
        "python -m ruff check .",
        "python -m pytest tests",
    )


def test_apply_is_namespaced_idempotent_and_preserves_governance(
    source_repo: Path, target_repo: Path
) -> None:
    mission = b"# Product mission\n\nShip the consumer outcome.\n"
    result = apply_adoption(
        target_repo,
        source_root=source_repo,
        seed_files={"MISSION.md": mission},
    )
    assert ".factory/runtime/core/show_root.py" in result.created
    assert not (target_repo / "core").exists()
    assert not (target_repo / ".claude").exists()
    assert (target_repo / "MISSION.md").read_bytes() == mission
    assert "Keep this text." in (target_repo / "AGENTS.md").read_text(encoding="utf-8")
    adapted = (target_repo / ".agents" / "skills" / "example" / "SKILL.md").read_text(encoding="utf-8")
    assert "python .factory/darkfac.py module core.show_root" in adapted
    assert "python .factory/darkfac.py script core/show_root.py" in adapted
    lock_text = (target_repo / ".factory" / "darkfac.lock.json").read_text(encoding="utf-8")
    assert str(source_repo) not in lock_text
    assert '".claude/' not in lock_text
    assert verify_adoption(target_repo).ready

    process = subprocess.run(
        [sys.executable, ".factory/darkfac.py", "module", "core.show_root"],
        cwd=target_repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    assert Path(process.stdout.strip()).resolve() == target_repo.resolve()

    _commit_all(target_repo, "adopt factory")
    second_plan = plan_adoption(target_repo, source_root=source_repo)
    assert second_plan.ready
    assert not any(item.action in {FileAction.CREATE, FileAction.UPDATE} for item in second_plan.files)


def test_dirty_target_and_managed_drift_fail_closed(source_repo: Path, target_repo: Path) -> None:
    apply_adoption(target_repo, source_root=source_repo)
    managed = target_repo / ".factory" / "runtime" / "core" / "show_root.py"
    managed.write_text("print('tampered')\n", encoding="utf-8")
    plan = plan_adoption(target_repo, source_root=source_repo)
    assert not plan.ready
    assert any("dirty" in blocker for blocker in plan.blockers)
    assert any("managed path conflict" in blocker for blocker in plan.blockers)
    with pytest.raises(AdoptionBlockedError):
        apply_adoption(target_repo, source_root=source_repo)


def test_dirty_source_cannot_claim_committed_provenance(source_repo: Path, target_repo: Path) -> None:
    (source_repo / "uncommitted.txt").write_text("drift", encoding="utf-8")
    with pytest.raises(AdoptionBlockedError, match="source is dirty"):
        plan_adoption(target_repo, source_root=source_repo)


def test_update_removes_only_unchanged_retired_managed_files(
    source_repo: Path, target_repo: Path
) -> None:
    apply_adoption(target_repo, source_root=source_repo)
    _commit_all(target_repo, "adopt factory")
    retired_source = source_repo / "docs" / "DARK_FACTORY_PLAYBOOK.md"
    retired_target = target_repo / ".factory" / "docs" / "DARK_FACTORY_PLAYBOOK.md"
    retired_source.unlink()
    _commit_all(source_repo, "retire managed document")

    plan = plan_adoption(target_repo, source_root=source_repo)
    retired_item = next(item for item in plan.files if item.path == ".factory/docs/DARK_FACTORY_PLAYBOOK.md")
    assert retired_item.action == FileAction.REMOVE
    result = apply_adoption(target_repo, source_root=source_repo)
    assert ".factory/docs/DARK_FACTORY_PLAYBOOK.md" in result.removed
    assert not retired_target.exists()
    assert verify_adoption(target_repo).ready


def test_adoption_worktree_uses_committed_state_from_dirty_checkout(target_repo: Path) -> None:
    original = (target_repo / "README.md").read_text(encoding="utf-8")
    (target_repo / "README.md").write_text("dirty local edit\n", encoding="utf-8")
    destination = target_repo.parent / "adoption-worktree"
    worktree = prepare_adoption_worktree(
        target_repo,
        branch="codex/test-adoption",
        destination=destination,
    )
    try:
        assert (worktree / "README.md").read_text(encoding="utf-8") == original
        assert _git(worktree, "status", "--porcelain").stdout == ""
    finally:
        _git(target_repo, "worktree", "unlock", str(worktree), check=False)
        _git(target_repo, "worktree", "remove", "--force", str(worktree), check=False)
        _git(target_repo, "branch", "-D", "codex/test-adoption", check=False)


def test_transaction_rolls_back_when_a_write_fails(
    source_repo: Path, target_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.adoption import service

    original_write = service._atomic_write
    calls = 0

    def fail_second_write(path: Path, data: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        original_write(path, data)

    monkeypatch.setattr(service, "_atomic_write", fail_second_write)
    with pytest.raises(OSError, match="simulated"):
        apply_adoption(target_repo, source_root=source_repo)
    assert not (target_repo / ".factory" / "darkfac.lock.json").exists()
    assert not any((target_repo / ".agents" / "skills").glob("**/SKILL.md"))


def test_initialize_greenfield_and_prepare_governed_task(source_repo: Path, tmp_path: Path) -> None:
    project = initialize_project(tmp_path / "greenfield", project_name="Greenfield")
    _git(project, "config", "user.name", "DarkFac Test")
    _git(project, "config", "user.email", "darkfac-test@local")
    (project / "pyproject.toml").write_text("[project]\nname = 'greenfield'\nversion = '0.1.0'\n", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")
    _commit_all(project, "add test contract")
    apply_adoption(project, source_root=source_repo)
    _commit_all(project, "adopt factory")

    task_path = tmp_path / "task-worktree"
    task = prepare_task(
        project,
        ticket_id="FND-04",
        title="Opaque cursor",
        owner="factory",
        allowed_paths=("src/example.py", "tests/test_example.py"),
        validate_commands=("python -m pytest tests/test_example.py",),
        destination=task_path,
    )
    try:
        payload = json.loads((task_path / task.manifest_path).read_text(encoding="utf-8"))
        assert payload["ticket_id"] == "FND-04"
        assert payload["allowed_paths"] == ["src/example.py", "tests/test_example.py"]
    finally:
        _git(project, "worktree", "unlock", str(task_path), check=False)
        _git(project, "worktree", "remove", "--force", str(task_path), check=False)
        _git(project, "branch", "-D", task.branch, check=False)
