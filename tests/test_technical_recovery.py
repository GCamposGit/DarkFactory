"""Behavioral coverage for bounded technical Git conflict recovery (USR-475 T3)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import run_ticket
from core.demands.models import DeliveryStatus, UserTicket


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return result.stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(repo, "add", name)


def _conflict_repo(
    tmp_path: Path,
    base: dict[str, str],
    ticket_side: dict[str, str],
    main_side: dict[str, str],
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Recovery Test")
    _git(repo, "config", "user.email", "recovery@darkfac.local")
    _write(repo, {"README.md": "# Test\n", **base})
    _git(repo, "commit", "-m", "common base")
    _git(repo, "checkout", "-b", "ticket/usr-475-test")
    _write(repo, ticket_side)
    _git(repo, "commit", "-m", "ticket work")
    _git(repo, "checkout", "main")
    _write(repo, main_side)
    _git(repo, "commit", "-m", "main work")
    _git(repo, "checkout", "ticket/usr-475-test")
    return repo


def _ticket() -> UserTicket:
    return UserTicket(
        id="USR-475-TEST", project_id="darkfac", title="Technical recovery",
        problem_statement="Recover technical rebase conflicts safely.",
        status=DeliveryStatus.PLANNED,
    )


def _rebase_active(repo: Path) -> bool:
    git_dir = Path(_git(repo, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = repo / git_dir
    return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def test_document_insertions_preserve_both_sides_and_current_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    ledger = ".factory/demands/demands.json"
    base = "# Motor\n\n## Base\ntexto comum\n"
    repo = _conflict_repo(
        tmp_path,
        {"docs/manual/motor.md": base, ledger: "ancestor\n"},
        {
            "docs/manual/motor.md": base + "\n## Ticket\nsecao do ticket\n",
            ledger: "ticket snapshot\n",
        },
        {
            "docs/manual/motor.md": base + "\n## Main\nsecao atual\n",
            ledger: "ledger atual\n",
        },
    )
    monkeypatch.setattr(
        run_ticket, "_invoke_fix_agent",
        lambda *_args, **_kwargs: pytest.fail("compatible documentation must not need inference"),
    )

    ok, files, message = run_ticket.rebase_on_origin_main(repo, keep_conflict=True)
    assert ok is False and set(files) == {"docs/manual/motor.md", ledger}
    done, remaining, reason = run_ticket.resolve_rebase_conflict(_ticket(), repo, files, message)

    text = (repo / "docs/manual/motor.md").read_text(encoding="utf-8")
    assert done is True and remaining == [] and reason == "rebased successfully"
    assert "texto comum" in text and "secao do ticket" in text and "secao atual" in text
    assert "<<<<<<<" not in text
    assert (repo / ledger).read_text(encoding="utf-8").strip() == "ledger atual"
    assert not _rebase_active(repo)


def test_eligible_harness_resolves_only_the_recorded_conflict_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(run_ticket, "pick", lambda *_args, **_kwargs: ("codex", "test-model"))
    repo = _conflict_repo(
        tmp_path, {"mod.py": "value = 0\n"},
        {"mod.py": "value = 1\n"}, {"mod.py": "value = 2\n"},
    )

    ok, files, message = run_ticket.rebase_on_origin_main(repo, keep_conflict=True)
    assert ok is False and files == ["mod.py"]

    def resolve_in_place(
        worktree: Path, ticket: UserTicket, stage: str, feedback: str, *, route: tuple[str, str],
    ) -> None:
        assert worktree.resolve() == repo.resolve()
        assert ticket.id == "USR-475-TEST" and stage == "rebase-conflito"
        assert route == ("codex", "test-model") and "mod.py" in feedback
        (worktree / "mod.py").write_text("value = 3\n", encoding="utf-8")
        _git(worktree, "add", "mod.py")

    monkeypatch.setattr(run_ticket, "_invoke_fix_agent", resolve_in_place)
    done, remaining, reason = run_ticket.resolve_rebase_conflict(_ticket(), repo, files, message)

    assert done is True and remaining == [] and reason == "rebased successfully"
    assert (repo / "mod.py").read_text(encoding="utf-8") == "value = 3\n"
    assert not _rebase_active(repo)


def test_harness_result_with_unresolved_markers_is_rejected_before_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(run_ticket, "pick", lambda *_args, **_kwargs: ("codex", "test-model"))
    repo = _conflict_repo(
        tmp_path, {"mod.py": "value = 0\n"},
        {"mod.py": "value = 1\n"}, {"mod.py": "value = 2\n"},
    )
    candidate = _git(repo, "rev-parse", "HEAD")
    ok, files, message = run_ticket.rebase_on_origin_main(repo, keep_conflict=True)
    assert ok is False and files == ["mod.py"]

    def incomplete_resolution(worktree: Path, *_args: object, **_kwargs: object) -> None:
        (worktree / "mod.py").write_text(
            "<<<<<<< HEAD\nvalue = 2\n=======\nvalue = 1\n>>>>>>> ticket\n", encoding="utf-8",
        )
        _git(worktree, "add", "mod.py")

    monkeypatch.setattr(run_ticket, "_invoke_fix_agent", incomplete_resolution)
    done, _remaining, reason = run_ticket.resolve_rebase_conflict(_ticket(), repo, files, message)

    assert done is False and "unresolved_markers" in reason
    assert not _rebase_active(repo) and _git(repo, "rev-parse", "HEAD") == candidate
    assert (repo / "mod.py").read_text(encoding="utf-8") == "value = 1\n"


def test_unregistered_ticket_branch_never_reaches_the_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    repo = _conflict_repo(
        tmp_path, {"mod.py": "value = 0\n"},
        {"mod.py": "value = 1\n"}, {"mod.py": "value = 2\n"},
    )
    _git(repo, "branch", "-m", "unrelated-branch")
    monkeypatch.setattr(
        run_ticket, "_invoke_fix_agent",
        lambda *_args, **_kwargs: pytest.fail("unregistered candidate must not reach a harness"),
    )
    ok, files, message = run_ticket.rebase_on_origin_main(repo, keep_conflict=True)
    assert ok is False and files == ["mod.py"]

    done, _remaining, reason = run_ticket.resolve_rebase_conflict(_ticket(), repo, files, message)

    assert done is False and reason == "rebase_worktree_unregistered_or_ticket_branch_mismatch"
    assert not _rebase_active(repo) and (repo / "mod.py").read_text(encoding="utf-8") == "value = 1\n"


def test_same_exhausted_conflict_does_not_repeat_inference_or_alert_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setattr(run_ticket, "pick", lambda *_args, **_kwargs: ("codex", "test-model"))
    from core.line import failure_alerts

    alerts: list[dict[str, Any]] = []
    monkeypatch.setattr(failure_alerts, "_emit_failure_alert", lambda **kwargs: alerts.append(kwargs) or True)
    repo = _conflict_repo(
        tmp_path, {"mod.py": "value = 0\n"},
        {"mod.py": "value = 1\n"}, {"mod.py": "value = 2\n"},
    )
    candidate = _git(repo, "rev-parse", "HEAD")
    calls = 0

    def unchanged_agent(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(run_ticket, "_invoke_fix_agent", unchanged_agent)

    def resume_conflict() -> tuple[bool, str]:
        ok, files, message = run_ticket.rebase_on_origin_main(repo, keep_conflict=True)
        assert ok is False and files == ["mod.py"]
        done, _remaining, reason = run_ticket.resolve_rebase_conflict(_ticket(), repo, files, message)
        return done, reason

    done, first = resume_conflict()
    assert done is False and "recovery_exhausted" in first
    assert not _rebase_active(repo) and _git(repo, "rev-parse", "HEAD") == candidate

    done, second = resume_conflict()
    assert done is False and "recovery_exhausted" in second
    assert calls == 1 and len(alerts) == 1
    assert alerts[0]["transition"] == "technical_hold"
    assert (repo / "mod.py").read_text(encoding="utf-8") == "value = 1\n"

    from core.line.admission_store import AdmissionStore

    rows = AdmissionStore(tmp_path / "state" / "admission.sqlite3").rebase_recovery_rows(
        project_id="darkfac", ticket_id="USR-475-TEST",
    )
    assert len(rows) == 1 and rows[0]["inferred"] == 1
    assert rows[0]["recovery_result"] == "technical_hold"
