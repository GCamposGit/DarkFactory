"""USR-175: the official gate runs on an immutable candidate in an isolated worktree.

Incident (USR-134, 2026-10-08): the gate ran in the SHARED checkout, ``main`` advanced under it
(0d71e59 -> 72c40a4) and the harness correctly refused to emit evidence. The fix is operational:
a detached worktree pinned to the candidate SHA. These tests use a fake gate command and a
temporary repository; the real harness is never invoked.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.git import ticket_workspace
from core.git.gate_isolation import (
    EXIT_CANDIDATE_MOVED,
    EXIT_SETUP_ERROR,
    run_isolated_gate,
)
from scripts import gate_isolated


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    )
    return res.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> SimpleNamespace:
    local = tmp_path / "local"
    local.mkdir()
    _git(local, "init", "--quiet", "-b", "main")
    _git(local, "config", "user.name", "DarkFac Test")
    _git(local, "config", "user.email", "test@darkfac.internal")
    _git(local, "config", "commit.gpgsign", "false")
    (local / "README.md").write_text("base\n", encoding="utf-8")
    _git(local, "add", "README.md")
    _git(local, "commit", "--quiet", "-m", "chore: initial")
    return SimpleNamespace(local=local, tmp=tmp_path, sha=_git(local, "rev-parse", "HEAD"))


def _py(code: str, *extra: str):
    """Command builder running ``python -c code [extra...]`` inside the isolated worktree."""

    def build(_worktree: Path) -> list[str]:
        return [sys.executable, "-c", code, *extra]

    return build


def _advance(repo_path: Path, name: str) -> None:
    (repo_path / name).write_text(name, encoding="utf-8")
    _git(repo_path, "add", name)
    _git(repo_path, "commit", "--quiet", "-m", f"feat: {name}")


_COMMIT = "['git','-c','user.name=x','-c','user.email=x@x','commit','--allow-empty','-q','-m','m']"


def test_gate_runs_in_detached_worktree_pinned_to_candidate_and_removes_it(repo: SimpleNamespace) -> None:
    probe = repo.tmp / "probe.txt"
    code = (
        "import pathlib, subprocess, sys;"
        "head = subprocess.run(['git','rev-parse','HEAD'], capture_output=True, text=True).stdout.strip();"
        "branch = subprocess.run(['git','symbolic-ref','-q','HEAD'], capture_output=True, text=True).returncode;"
        "pathlib.Path(sys.argv[1]).write_text(head + '|' + str(branch) + '|' + str(pathlib.Path.cwd()), encoding='utf-8')"
    )

    result = run_isolated_gate(cwd=repo.local, command_builder=_py(code, str(probe)))

    assert result.ok and result.returncode == 0
    head, symbolic, cwd = probe.read_text(encoding="utf-8").split("|")
    assert head == repo.sha == result.candidate_sha
    assert symbolic != "0"  # detached HEAD: no branch a concurrent session could move
    assert Path(cwd).resolve() == result.worktree.resolve()
    assert result.worktree.resolve() != repo.local.resolve()
    assert not result.worktree.exists() and result.cleaned
    registered = [ticket_workspace._norm(e["worktree"]) for e in ticket_workspace.list_worktrees(repo.local)]
    assert ticket_workspace._norm(result.worktree) not in registered


def test_concurrent_main_update_neither_changes_candidate_nor_invalidates_result(repo: SimpleNamespace) -> None:
    code = f"import subprocess, sys; subprocess.run(['git','-C',sys.argv[1]] + {_COMMIT}[1:], check=True)"

    result = run_isolated_gate(cwd=repo.local, command_builder=_py(code, str(repo.local)))

    assert result.ok and result.returncode == 0
    assert result.candidate_sha == repo.sha
    assert result.head_after == repo.sha  # the isolated candidate did not move
    assert _git(repo.local, "rev-parse", "HEAD") != repo.sha  # the shared checkout did advance


def test_shared_checkout_is_never_modified(repo: SimpleNamespace) -> None:
    (repo.local / "wip.txt").write_text("other session work", encoding="utf-8")
    before = _git(repo.local, "status", "--porcelain")

    result = run_isolated_gate(cwd=repo.local, command_builder=_py("pass"))

    assert result.ok
    assert _git(repo.local, "status", "--porcelain") == before
    assert (repo.local / "wip.txt").read_text(encoding="utf-8") == "other session work"
    assert _git(repo.local, "rev-parse", "HEAD") == repo.sha


def test_candidate_change_inside_worktree_blocks_success(repo: SimpleNamespace) -> None:
    code = f"import subprocess; subprocess.run({_COMMIT}, check=True)"

    result = run_isolated_gate(cwd=repo.local, command_builder=_py(code))

    assert not result.ok
    assert result.returncode == EXIT_CANDIDATE_MOVED
    assert result.head_after != result.candidate_sha
    assert "Candidate HEAD changed" in result.reason
    assert not result.worktree.exists()


def test_dirtying_the_candidate_worktree_blocks_success(repo: SimpleNamespace) -> None:
    code = "import pathlib; pathlib.Path('stray.txt').write_text('x')"

    result = run_isolated_gate(cwd=repo.local, command_builder=_py(code))

    assert not result.ok and result.returncode == EXIT_CANDIDATE_MOVED
    assert "dirty" in result.reason


def test_failing_gate_propagates_exit_code_and_still_cleans_up(repo: SimpleNamespace) -> None:
    result = run_isolated_gate(cwd=repo.local, command_builder=_py("raise SystemExit(7)"))

    assert not result.ok and result.returncode == 7
    assert not result.worktree.exists()


def test_ref_selects_another_commit_without_touching_current_head(repo: SimpleNamespace) -> None:
    _git(repo.local, "branch", "feature")
    _advance(repo.local, "later.txt")
    probe = repo.tmp / "probe.txt"
    code = "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text(str(pathlib.Path('later.txt').exists()))"

    result = run_isolated_gate(cwd=repo.local, ref="feature", command_builder=_py(code, str(probe)))

    assert result.ok and result.candidate_sha == repo.sha
    assert probe.read_text(encoding="utf-8") == "False"


def test_unknown_ref_is_a_setup_error(repo: SimpleNamespace) -> None:
    result = run_isolated_gate(cwd=repo.local, ref="no-such-ref", command_builder=_py("pass"))

    assert not result.ok and result.returncode == EXIT_SETUP_ERROR
    assert "no-such-ref" in result.reason


def test_inherited_project_root_env_is_scrubbed(repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DARKFAC_PROJECT_ROOT", str(repo.local))
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(repo.local / ".factory"))
    probe = repo.tmp / "env.txt"
    code = (
        "import os, pathlib, sys;"
        "pathlib.Path(sys.argv[1]).write_text(repr([os.environ.get('DARKFAC_PROJECT_ROOT'), os.environ.get('DARKFAC_STATE_ROOT')]))"
    )

    result = run_isolated_gate(cwd=repo.local, command_builder=_py(code, str(probe)))

    assert result.ok
    assert probe.read_text(encoding="utf-8") == "[None, None]"


def test_default_command_targets_official_runner_quick() -> None:
    from core.git.gate_isolation import default_gate_command

    cmd = default_gate_command(Path("X"))
    assert cmd[0] == sys.executable
    assert Path(cmd[1]) == Path("X") / "core" / "harness" / "runner.py"
    assert cmd[2:] == ["--quick"]


def test_cli_returns_exit_code_and_prints_summary(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo.local)
    monkeypatch.setattr(
        "core.git.gate_isolation.default_gate_command", lambda wt: [sys.executable, "-c", "raise SystemExit(0)"]
    )

    code = gate_isolated.main(["--ref", "HEAD"])

    out = capsys.readouterr().out
    assert code == 0
    assert repo.sha[:12] in out
    assert "ISOLATED_GATE_PASS" in out


def test_runbook_and_skill_point_to_the_isolated_gate() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    runbook = (repo_root / "docs" / "runbooks" / "gate_isolated.md").read_text(encoding="utf-8")
    skill = (repo_root / ".agents" / "skills" / "19-run-ticket" / "SKILL.md").read_text(encoding="utf-8")
    assert "gate_isolated.py --ref" in skill and "docs/runbooks/gate_isolated.md" in skill
    assert "Candidate HEAD changed" in runbook and "--detach" in runbook
    assert (repo_root / "scripts" / "gate_isolated.py").is_file()
