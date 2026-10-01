"""Regression tests for the Git Bash path-conversion incident (USR-76)."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import subprocess

import pytest

from core.git.autonomy import DEFAULT_PROTECTED_STATE_PATHS, GitAutonomyManager
from core.git.safe_show import safe_show
from core.line.workspace import RunWorkspace, WorkspaceError, commit, commit_paths


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        encoding="utf-8", check=True,
    )


@pytest.fixture
def state_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "DarkFac Test")
    _git(repo, "config", "user.email", "test@darkfac.internal")
    _git(repo, "config", "commit.gpgsign", "false")
    for path in DEFAULT_PROTECTED_STATE_PATHS:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(f"line {i}\n" for i in range(10)), encoding="utf-8")
    (repo / "feature.py").write_text("original\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "initial")
    return repo


@pytest.mark.parametrize("path", DEFAULT_PROTECTED_STATE_PATHS)
@pytest.mark.parametrize("remaining", [0, 4])
def test_commit_rejects_major_state_loss(state_repo: Path, path: str, remaining: int) -> None:
    original = _git(state_repo, "rev-parse", "HEAD").stdout.strip()
    (state_repo / path).write_text("".join(f"line {i}\n" for i in range(remaining)), encoding="utf-8")
    with pytest.raises(RuntimeError, match="Refusing to commit protected state file"):
        GitAutonomyManager(state_repo).commit_ticket("USR-76", "State guard")
    assert _git(state_repo, "rev-parse", "HEAD").stdout.strip() == original
    assert _git(state_repo, "diff", "--cached", "--name-only").stdout.strip() == path


def test_commit_allows_half_removed_and_custom_guard(state_repo: Path) -> None:
    path = DEFAULT_PROTECTED_STATE_PATHS[0]
    (state_repo / path).write_text("".join(f"line {i}\n" for i in range(5)), encoding="utf-8")
    assert GitAutonomyManager(state_repo).commit_ticket("USR-76", "Half removed")

    custom = "custom.json"
    (state_repo / custom).write_text("a\nb\nc\nd\n", encoding="utf-8")
    _git(state_repo, "add", custom)
    _git(state_repo, "commit", "--quiet", "-m", "custom initial")
    (state_repo / custom).write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="custom.json"):
        GitAutonomyManager(state_repo, protected_state_paths=(custom,)).commit_ticket("USR-76", "Custom")


def test_guard_checks_already_staged_files_outside_requested_paths(state_repo: Path) -> None:
    path = DEFAULT_PROTECTED_STATE_PATHS[0]
    (state_repo / path).write_text("", encoding="utf-8")
    _git(state_repo, "add", path)
    (state_repo / "feature.py").write_text("updated\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="demands.json"):
        GitAutonomyManager(state_repo).commit_ticket("USR-76", "Feature", paths=["feature.py"])


def test_commit_rejects_deleting_state_file(state_repo: Path) -> None:
    path = DEFAULT_PROTECTED_STATE_PATHS[0]
    (state_repo / path).unlink()
    with pytest.raises(RuntimeError, match="demands.json"):
        GitAutonomyManager(state_repo).commit_ticket("USR-76", "Delete ledger")
    assert _git(state_repo, "cat-file", "-e", f"HEAD:{path}").returncode == 0


@pytest.mark.parametrize("commit_selected_paths", [False, True])
def test_line_workspace_commit_rejects_empty_ledger(state_repo: Path, commit_selected_paths: bool) -> None:
    path = DEFAULT_PROTECTED_STATE_PATHS[0]
    target = state_repo / path
    target.write_text("", encoding="utf-8")
    ws = RunWorkspace(
        project_id="darkfac", run_id="test", branch="test", path=state_repo,
        base_sha=_git(state_repo, "rev-parse", "HEAD").stdout.strip(),
    )
    with pytest.raises(WorkspaceError, match="demands.json"):
        if commit_selected_paths:
            commit_paths(ws, "Empty state", "USR-76", [target])
        else:
            commit(ws, "Empty state", "USR-76")


def test_safe_show_uses_one_argv_item_without_shell(state_repo: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert cwd == state_repo
        calls.append(list(args))
        return _git(cwd, *args)

    result = safe_show("HEAD", DEFAULT_PROTECTED_STATE_PATHS[0], cwd=state_repo, runner=runner)
    assert result.stdout.startswith("line 0\n")
    assert calls == [["show", "HEAD:.factory/demands/demands.json"]]


def test_no_shell_git_revision_path_commands_in_factory() -> None:
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    sources = [*root.joinpath("core").rglob("*.py"), *root.joinpath("scripts").rglob("*.py"), root / "run_ticket.py"]
    for source in sources:
        source_text = source.read_text(encoding="utf-8-sig")
        tree = ast.parse(source_text, filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            command = ast.get_source_segment(source_text, node.args[0]) or ""
            shell = next((kw.value for kw in node.keywords if kw.arg == "shell"), None)
            if isinstance(shell, ast.Constant) and shell.value is True:
                if re.search(r"\bgit\s+show\s+\S+:\S+", command):
                    offenders.append(f"{source.relative_to(root)}:{node.lineno}")
            if source.name == "safe_show.py" or not isinstance(node.args[0], ast.List):
                continue
            values = node.args[0].elts
            for index, value in enumerate(values[:-1]):
                if not isinstance(value, ast.Constant) or value.value != "show":
                    continue
                revision_path = values[index + 1]
                if isinstance(revision_path, ast.JoinedStr) and any(
                    isinstance(part, ast.Constant) and ":" in str(part.value)
                    for part in revision_path.values
                ):
                    offenders.append(f"{source.relative_to(root)}:{node.lineno}")
    assert not offenders, f"Git rev:path in shell command: {offenders}"
