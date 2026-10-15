from __future__ import annotations

import subprocess
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.projects.models import ProjectDescriptor
from core.projects.ticket_execution import (
    ProjectExecutionError,
    project_checkout,
    setup_project,
    validate_project_commands,
)
from core.git.ticket_workspace import create_workspace
from core.git.autonomy import DeliveryReport, GitAutonomyManager
from core.demands.models import UserTicket


def _git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _project(tmp_path: Path, *, validate: list[str]) -> tuple[ProjectDescriptor, Path]:
    remote = tmp_path / "project.git"
    checkout = tmp_path / "project"
    _git(["init", "--bare", str(remote)])
    _git(["clone", str(remote), str(checkout)])
    _git(["config", "user.email", "tests@example.invalid"], checkout)
    _git(["config", "user.name", "Project Test"], checkout)
    (checkout / "README.md").write_text("project", encoding="utf-8")
    _git(["add", "README.md"], checkout)
    _git(["commit", "-m", "initial"], checkout)
    _git(["branch", "-M", "trunk"], checkout)
    _git(["push", "-u", "origin", "trunk"], checkout)
    project = ProjectDescriptor(
        id="fake-client",
        name="Fake Client",
        path=str(checkout),
        repo_url=str(remote),
        default_branch="trunk",
        commands={"setup": ["python -c \"print('setup ran')\""], "validate": validate},
    )
    return project, checkout


def test_project_checkout_uses_registered_repo_and_runs_declared_validation(tmp_path: Path) -> None:
    project, checkout = _project(tmp_path, validate=["echo client-validate"])

    resolved = project_checkout(project)
    setup = setup_project(project, resolved)
    validation = validate_project_commands(project, resolved)

    assert resolved == checkout.resolve()
    assert _git(["remote", "get-url", "origin"], resolved) == str(tmp_path / "project.git")
    assert setup.ok
    assert validation.ok
    assert "client-validate" in validation.combined_output


def test_project_validation_failure_fails_the_gate(tmp_path: Path) -> None:
    project, checkout = _project(tmp_path, validate=['python -c "raise SystemExit(17)"'])

    result = validate_project_commands(project, checkout)

    assert not result.ok
    assert result.exit_code == 17


def test_ticket_worktree_is_created_under_the_registered_project_repository(tmp_path: Path) -> None:
    project, checkout = _project(tmp_path, validate=["echo ok"])

    workspace = create_workspace("USR-259", cwd=project_checkout(project), base=project.default_branch)

    assert workspace.path.parent == checkout / ".worktrees"
    assert workspace.main_root == checkout.resolve()
    assert workspace.base_ref == "origin/trunk"
    assert Path(_git(["rev-parse", "--show-toplevel"], workspace.path)).resolve() == workspace.path.resolve()


def test_project_without_declared_validate_is_rejected_with_onboarding_instruction(tmp_path: Path) -> None:
    project, checkout = _project(tmp_path, validate=[])

    with pytest.raises(ProjectExecutionError, match="Project Adoption Gateway.*skill 07"):
        validate_project_commands(project, checkout)


def test_registry_null_commands_load_as_unconfigured_commands(tmp_path: Path) -> None:
    project = ProjectDescriptor.model_validate({
        "id": "fake-client", "name": "Fake Client", "commands": None,
    })

    assert project.commands.setup == []
    assert project.commands.validate_cmds == []


@pytest.mark.parametrize(
    ("commands", "expected_ok", "expected_cause"),
    [
        (["echo client-gate"], True, None),
        (["python -c \"raise SystemExit(9)\""], False, "project_validate_failed"),
        ([], False, "project_validate_missing"),
    ],
)
def test_external_ticket_gate_runs_declared_validate_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commands: list[str],
    expected_ok: bool,
    expected_cause: str | None,
) -> None:
    import core.projects.registry as registry
    import run_ticket

    project = ProjectDescriptor(
        id="fake-client", name="Fake Client", path=str(tmp_path),
        commands={"validate": commands},
    )
    monkeypatch.setattr(registry, "get_project_registry", lambda: SimpleNamespace(get_project=lambda _id: project))
    progress = SimpleNamespace(phase=Mock())
    ticket = UserTicket(id="USR-259", project_id="fake-client", title="client change")

    result = run_ticket._run_gate_with_self_correction(
        tmp_path, "ticket/usr-259", "abc", ticket, "resume", False, True, True,
        progress, lambda: None, [],
    )

    if expected_ok:
        assert result is None
    else:
        assert isinstance(result, dict)
        assert result["cause"] == expected_cause


def test_external_delivery_opens_pr_from_project_worktree_against_registered_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import core.projects.registry as registry
    import run_ticket

    project_root = tmp_path / "client-repo"
    project_root.mkdir()
    worktree = project_root / ".worktrees" / "usr-259"
    worktree.mkdir(parents=True)
    project = ProjectDescriptor(
        id="fake-client", name="Fake Client", path=str(project_root), default_branch="trunk",
        commands={"validate": ["echo ok"]},
    )
    monkeypatch.setattr(registry, "get_project_registry", lambda: SimpleNamespace(get_project=lambda _id: project))
    monkeypatch.setattr(run_ticket, "rebase_on_project_base", lambda _cwd, _base: (True, [], "ok"))
    monkeypatch.setattr(run_ticket, "_run_gate_with_self_correction", lambda *args, **kwargs: None)
    monkeypatch.setattr(run_ticket, "_cancelled", lambda: False)

    manager = GitAutonomyManager(project_root)
    delivery = Mock(return_value=DeliveryReport(
        ok=True, action="merged", pr_url="https://example.invalid/pr/7", merge_sha="abc123",
    ))
    manager.is_dirty = Mock(return_value=False)  # type: ignore[method-assign]
    manager.get_current_sha = Mock(return_value="abc123")  # type: ignore[method-assign]
    manager.deliver_branch = delivery  # type: ignore[method-assign]
    manager.sweep_stale = Mock()  # type: ignore[method-assign]
    progress = SimpleNamespace(phase=Mock(), set_outcome=Mock())
    ticket = UserTicket(id="USR-259", project_id="fake-client", title="client change")

    result = run_ticket._deliver_worktree(
        manager, ticket, worktree, "ticket/usr-259", "resume", False, False, False,
        True, True, progress, lambda: {"exit_code": 130},
    )

    assert result["ok"] is True
    delivery.assert_called_once_with(
        ticket.id, ticket.title, cwd=worktree, base="trunk", merge=True, kind="implementation",
    )
