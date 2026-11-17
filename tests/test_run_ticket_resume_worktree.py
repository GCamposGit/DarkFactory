"""run_ticket reuses the worktree an interrupted attempt preserved (USR-225).

Incident 11/10/2026: `run_ticket USR-205 --harness grok` hit the 1800 s timeout with the implementation
already in its worktree; the retry policy preserved it, but the NEXT `run_ticket USR-205` created a brand
new worktree and the agent started from scratch. The USR-113 continuation notice only looked at the
worktree of the current run, so a second invocation was not covered.

Every repository here is a temporary one under ``tmp_path``; nothing touches the real ``.worktrees``.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import run_ticket
from core.git import ticket_workspace
from core.git.autonomy import GitAutonomyManager, SweepReport
from core.git.ticket_workspace import create_workspace, find_preserved_workspaces
from core.line import routing
from core.line.agent_cli import AgentRequest, AgentResult
from core.line.routing import _HARNESS_TO_PROVIDER

_TICKET = run_ticket.UserTicket(
    id="USR-205", project_id="darkfac", title="Ticket que estoura o tempo", problem_statement="problema"
)


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8", errors="replace", check=True
    )
    return res.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> SimpleNamespace:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--quiet", "-b", "main")
    local = tmp_path / "local"
    local.mkdir()
    _git(local, "init", "--quiet", "-b", "main")
    _git(local, "config", "user.name", "DarkFac Test")
    _git(local, "config", "user.email", "test@darkfac.internal")
    _git(local, "config", "commit.gpgsign", "false")
    (local / "README.md").write_text("base\n", encoding="utf-8")
    _git(local, "add", "README.md")
    _git(local, "commit", "--quiet", "-m", "chore: initial")
    _git(local, "remote", "add", "origin", str(bare))
    _git(local, "push", "--quiet", "-u", "origin", "main")
    return SimpleNamespace(local=local, bare=bare, tmp=tmp_path)


def _worktree_names(repo: SimpleNamespace) -> list[str]:
    root = repo.local / ".worktrees"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


# ---------------------------------------------------------------------------
# find_preserved_workspaces
# ---------------------------------------------------------------------------


def test_no_worktree_yields_no_candidate(repo: SimpleNamespace) -> None:
    lookup = find_preserved_workspaces("USR-205", cwd=repo.local)
    assert lookup.chosen is None and lookup.others == []


def test_untouched_worktree_is_not_reused(repo: SimpleNamespace) -> None:
    create_workspace("USR-205", cwd=repo.local)
    lookup = find_preserved_workspaces("USR-205", cwd=repo.local)
    assert lookup.chosen is None
    assert any("sem alteracoes" in note for note in lookup.notes)


def test_worktree_with_uncommitted_changes_is_found(repo: SimpleNamespace) -> None:
    ws = create_workspace("USR-205", cwd=repo.local)
    (ws.path / "impl.py").write_text("x = 1\n", encoding="utf-8")

    lookup = find_preserved_workspaces("USR-205", cwd=repo.local)

    assert lookup.chosen is not None and lookup.chosen.workspace.path == ws.path
    assert lookup.chosen.workspace.branch == ws.branch == "ticket/usr-205"
    assert lookup.chosen.changed_paths == ("impl.py",)
    assert lookup.chosen.workspace.base_sha == _git(repo.local, "rev-parse", "origin/main")
    assert lookup.chosen.workspace.main_root == repo.local


def test_worktree_with_commits_ahead_of_base_is_found(repo: SimpleNamespace) -> None:
    ws = create_workspace("USR-205", cwd=repo.local)
    (ws.path / "impl.py").write_text("x = 1\n", encoding="utf-8")
    _git(ws.path, "add", "impl.py")
    _git(ws.path, "commit", "--quiet", "-m", "wip")

    lookup = find_preserved_workspaces("USR-205", cwd=repo.local)

    assert lookup.chosen is not None and lookup.chosen.workspace.path == ws.path
    assert lookup.chosen.commits_ahead == 1 and lookup.chosen.changed_paths == ()


def test_ignored_paths_do_not_count_as_work(repo: SimpleNamespace) -> None:
    ws = create_workspace("USR-205", cwd=repo.local)
    (ws.path / ".factory" / "demands").mkdir(parents=True)
    (ws.path / ".factory" / "demands" / "demands.json").write_text("{}", encoding="utf-8")

    lookup = find_preserved_workspaces("USR-205", cwd=repo.local, ignore_paths=(run_ticket.LEDGER_RELATIVE,))

    assert lookup.chosen is None


def test_most_recent_worktree_with_work_wins_and_the_others_are_listed(repo: SimpleNamespace) -> None:
    from datetime import datetime, timezone

    stamps = [
        datetime(2026, 10, 10, 21, 28, 42, tzinfo=timezone.utc),
        datetime(2026, 10, 10, 21, 59, 56, tzinfo=timezone.utc),
        datetime(2026, 10, 10, 22, 30, 0, tzinfo=timezone.utc),
    ]
    old, newer, newest_untouched = (create_workspace("USR-205", cwd=repo.local, now_fn=lambda s=s: s) for s in stamps)
    (old.path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (newer.path / "b.py").write_text("b = 1\n", encoding="utf-8")
    assert newer.branch == "ticket/" + newer.path.name  # the second attempt got the timestamped branch

    lookup = find_preserved_workspaces("USR-205", cwd=repo.local)

    assert lookup.chosen is not None and lookup.chosen.workspace.path == newer.path
    assert [o.workspace.path for o in lookup.others] == [old.path]
    assert any(str(newest_untouched.path) in note for note in lookup.notes)  # newest but empty: explained, kept
    for ws in (old, newer, newest_untouched):
        assert ws.path.exists()  # nothing is ever deleted


def test_other_tickets_and_prefix_lookalikes_are_never_considered(repo: SimpleNamespace) -> None:
    other = create_workspace("USR-2050", cwd=repo.local)
    (other.path / "x.py").write_text("x = 1\n", encoding="utf-8")
    sibling = create_workspace("USR-20", cwd=repo.local)
    (sibling.path / "y.py").write_text("y = 1\n", encoding="utf-8")

    assert find_preserved_workspaces("USR-205", cwd=repo.local).chosen is None
    assert find_preserved_workspaces("USR-20", cwd=repo.local).chosen.workspace.path == sibling.path  # type: ignore[union-attr]


def test_locked_worktree_and_index_lock_mean_in_use(repo: SimpleNamespace) -> None:
    locked = create_workspace("USR-205", cwd=repo.local)
    (locked.path / "a.py").write_text("a = 1\n", encoding="utf-8")
    _git(repo.local, "worktree", "lock", str(locked.path))
    assert find_preserved_workspaces("USR-205", cwd=repo.local).chosen is None
    _git(repo.local, "worktree", "unlock", str(locked.path))
    assert find_preserved_workspaces("USR-205", cwd=repo.local).chosen is not None

    git_dir = Path(_git(locked.path, "rev-parse", "--absolute-git-dir"))
    (git_dir / "index.lock").write_text("", encoding="utf-8")
    lookup = find_preserved_workspaces("USR-205", cwd=repo.local)
    assert lookup.chosen is None and any("index.lock" in note for note in lookup.notes)


def test_registered_worktree_whose_directory_vanished_is_skipped(repo: SimpleNamespace) -> None:
    ws = create_workspace("USR-205", cwd=repo.local)
    (ws.path / "a.py").write_text("a = 1\n", encoding="utf-8")
    import shutil

    shutil.rmtree(ws.path)

    assert find_preserved_workspaces("USR-205", cwd=repo.local).chosen is None


# ---------------------------------------------------------------------------
# run_ticket: two invocations, the first one times out
# ---------------------------------------------------------------------------


def _quotas() -> dict[str, dict[str, Any]]:
    return {
        harness: {"provider": provider, "headroom": 60.0, "is_critical": False, "status": "SAUDAVEL"}
        for harness, provider in _HARNESS_TO_PROVIDER.items()
    }


class _Store:
    def __init__(self, _path: Path) -> None:
        pass

    def get_ticket(self, ticket_id: str):
        return _TICKET if ticket_id == _TICKET.id else None

    def list_tickets(self):
        return [_TICKET]


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch, repo: SimpleNamespace) -> SimpleNamespace:
    monkeypatch.setattr(run_ticket, "PROJECT_ROOT", repo.local)
    monkeypatch.setattr(run_ticket, "DemandsStore", _Store)
    monkeypatch.setattr(run_ticket, "inspect_quotas", _quotas)
    monkeypatch.setattr(routing, "default_cooldown_path", lambda: repo.tmp / "cooldowns.json")
    monkeypatch.setattr(run_ticket, "time", SimpleNamespace(sleep=lambda _s: None))
    monkeypatch.setattr(run_ticket, "check_optional_mcp_servers", lambda *a, **k: (True, []))
    monkeypatch.setattr(run_ticket, "record_ticket_quota_cost", lambda **k: {})
    monkeypatch.setattr(run_ticket, "format_ticket_quota_summary", lambda _r: "")
    monkeypatch.setattr(GitAutonomyManager, "sweep_stale", lambda self, *a, **k: SweepReport())

    requests: list[AgentRequest] = []
    state = {"write_file": True}

    def fake_agent(req: AgentRequest) -> AgentResult:
        requests.append(req)
        if state["write_file"]:
            (Path(req.cwd) / "impl.py").write_text("partial = True\n", encoding="utf-8")
            state["write_file"] = False
        return AgentResult(
            ok=False, harness=req.harness, error_kind="timeout", exit_code=None, stderr_tail="",
            duration_s=1800.0, text="",
        )

    monkeypatch.setattr(run_ticket, "run_agent", fake_agent)
    return SimpleNamespace(requests=requests)


def test_second_invocation_fails_closed_on_uncheckpointed_build_change(
    repo: SimpleNamespace, launcher: SimpleNamespace, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An in-flight slice that changed files but never reached its checkpoint is not safe to replay:
    # the second invocation must preserve the same worktree and stop with an actionable mismatch.
    monkeypatch.setattr(run_ticket.agent_retry, "default_worktree_has_changes", lambda cwd: False)

    assert run_ticket.main(["USR-205", "--harness", "grok"]) == 1  # attempt 1: timeout, work preserved
    first_cwd = Path(launcher.requests[-1].cwd)
    assert _worktree_names(repo) == [first_cwd.name]
    assert "AVISO DE CONTINUA" not in launcher.requests[0].prompt  # nothing existed yet
    first_calls = len(launcher.requests)
    capsys.readouterr()

    assert run_ticket.main(["USR-205", "--harness", "grok"]) == 1

    second_requests = launcher.requests[first_calls:]
    assert second_requests == []  # divergent partial output is never overwritten by another inference
    assert _worktree_names(repo) == [first_cwd.name]  # no new worktree was created
    captured = capsys.readouterr()
    assert "Reaproveitando a worktree preservada" in captured.err
    assert "BUILD preservado diverge" in captured.err
    assert (first_cwd / "impl.py").read_text(encoding="utf-8") == "partial = True\n"  # the work survived


def test_resume_green_candidate_after_invalid_review_skips_development_and_binds_to_digest(
    repo: SimpleNamespace,
    launcher: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.line import stage_review
    from core.line.stages import Stage, require_artifact
    from core.research.policy import ResearchArtifact

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(repo.tmp / "state"))
    ticket = run_ticket.UserTicket(
        id="USR-423", project_id="darkfac", title="Retomar revisao independente",
        problem_statement="candidate with green validation and invalid review response",
    )
    monkeypatch.setattr(_Store, "get_ticket", lambda _self, ticket_id: ticket if ticket_id == ticket.id else None)
    ws = create_workspace(ticket.id, cwd=repo.local)
    (ws.path / "impl.py").write_text("candidate = True\n", encoding="utf-8")
    _git(ws.path, "add", "impl.py")
    _git(ws.path, "commit", "--quiet", "-m", "validated candidate")

    rws = run_ticket.ticket_engine.run_workspace(ticket, ws.branch, ws.base_sha)
    run_ticket.ticket_engine.write_minimal_plan(rws, ticket)
    run_ticket.ticket_engine.write_research(
        rws, ResearchArtifact(kind="research_skipped", ticket_id=ticket.id, reason="small fix")
    )
    run_ticket.ticket_engine.write_build(
        rws, "grok", [ticket.id], plan_digest=run_ticket.ticket_engine.plan_digest(rws),
        candidate_digest="candidate-digest", worktree=str(ws.path),
    )
    run_ticket.ticket_engine.write_validation(rws, True, "core/harness/runner.py --quick")
    candidate_ws = rws.model_copy(update={"path": ws.path})
    digest = stage_review._candidate_digest(candidate_ws, "main")
    policy_digest = stage_review._policy_digest(run_ticket.load_routing_config())
    assert digest is not None
    from core.line import workspace as line_workspace

    context_dir = line_workspace.context_dir(rws)
    line_workspace.write_context(
        rws,
        "review_state.json",
        json.dumps({"invalid_response_routes": [{
            "harness": "claude", "candidate_digest": digest, "policy_digest": policy_digest,
        }]}),
    )
    line_workspace.write_context(rws, "review_invalid_response.json", '{"reason":"json_decode_error"}')

    monkeypatch.setattr(
        run_ticket, "_stage_plan",
        lambda *_args, **_kwargs: pytest.fail("resumed candidate was replanned/reset"),
    )
    monkeypatch.setattr(
        run_ticket, "_stage_build",
        lambda *_args, **_kwargs: pytest.fail("development was re-invoked for a validated candidate"),
    )
    delivery_calls: list[Path] = []

    def deliver(*args, **kwargs):
        delivery_calls.append(args[2].path)
        return 0, {"ok": True, "status": "completed"}

    monkeypatch.setattr(run_ticket, "_stage_deliver", deliver)

    assert run_ticket.main([ticket.id, "--harness", "grok"]) == 0
    assert launcher.requests == []
    assert delivery_calls == [ws.path]
    assert (context_dir / "review_invalid_response.json").is_file()
    assert require_artifact(Stage.VALIDATE, rws).passed is True

    # Any candidate change invalidates this resume proof; the normal path will revalidate it.
    (ws.path / "impl.py").write_text("candidate = False\n", encoding="utf-8")
    _git(ws.path, "add", "impl.py")
    _git(ws.path, "commit", "--quiet", "-m", "candidate changed")
    assert run_ticket._can_resume_invalid_review(ticket, ws) is False


def test_built_candidate_resumes_delivery_without_plan_or_build_only_when_clean_and_proven(
    repo: SimpleNamespace,
    launcher: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.research.policy import ResearchArtifact

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(repo.tmp / "state"))
    ticket = run_ticket.UserTicket(
        id="USR-484", project_id="darkfac", title="Retomar entrega", problem_statement="built candidate",
    )
    monkeypatch.setattr(_Store, "get_ticket", lambda _self, ticket_id: ticket if ticket_id == ticket.id else None)
    ws = create_workspace(ticket.id, cwd=repo.local)
    rws = run_ticket.ticket_engine.run_workspace(ticket, ws.branch, ws.base_sha)
    # No commit and no artifacts: normal path.
    assert run_ticket._can_resume_built_candidate(ticket, ws) is False

    (ws.path / "impl.py").write_text("candidate = True\n", encoding="utf-8")
    _git(ws.path, "add", "impl.py")
    _git(ws.path, "commit", "--quiet", "-m", "built candidate")
    assert run_ticket._can_resume_built_candidate(ticket, ws) is False  # commits but no artifacts

    run_ticket.ticket_engine.write_minimal_plan(rws, ticket)
    run_ticket.ticket_engine.write_research(
        rws, ResearchArtifact(kind="research_skipped", ticket_id=ticket.id, reason="small fix")
    )
    run_ticket.ticket_engine.write_build(
        rws, "grok", [ticket.id], plan_digest=run_ticket.ticket_engine.plan_digest(rws),
        candidate_digest="candidate-digest", worktree=str(ws.path),
    )
    assert run_ticket._can_resume_built_candidate(ticket, ws) is False  # unbound legacy artifacts
    head = _git(ws.path, "rev-parse", "HEAD").strip()
    run_ticket._record_build_binding(ticket, ws.path, head)
    assert run_ticket._can_resume_built_candidate(ticket, ws) is True
    from core.git import autonomy
    real_git = autonomy._run_git
    with monkeypatch.context() as patch:
        patch.setattr(autonomy, "_run_git", lambda args, **kw: SimpleNamespace(returncode=128, stdout="")
                      if args[0] == "status" else real_git(args, **kw))
        assert run_ticket._can_resume_built_candidate(ticket, ws) is False
    (ws.path / "partial.py").write_text("partial = True\n", encoding="utf-8")
    _git(ws.path, "add", "partial.py")
    _git(ws.path, "commit", "--quiet", "-m", "partial candidate")
    assert run_ticket._can_resume_built_candidate(ticket, ws) is False
    _git(ws.path, "reset", "--hard", head)
    other = ticket.model_copy(update={"project_id": "outro"})
    assert run_ticket._can_resume_built_candidate(other, ws) is False

    (ws.path / "stray.txt").write_text("dirty\n", encoding="utf-8")
    assert run_ticket._can_resume_built_candidate(ticket, ws) is False
    (ws.path / "stray.txt").unlink()

    # Commit do agente de conserto regrava o vinculo: o candidato reparado segue retomavel.
    (ws.path / "impl.py").write_text("candidate = 'repaired'" + chr(10), encoding="utf-8")
    assert run_ticket._commit_fix_agent_changes_safely(ws.path, ticket, "gate") == (True, None)
    assert run_ticket._can_resume_built_candidate(ticket, ws) is True
    head = _git(ws.path, "rev-parse", "HEAD").strip()

    monkeypatch.setattr(run_ticket, "_stage_plan", lambda *_a, **_k: pytest.fail("replanned"))
    monkeypatch.setattr(run_ticket, "_stage_build", lambda *_a, **_k: pytest.fail("rebuilt"))
    delivery_calls: list[Path] = []

    def deliver(*args, **kwargs):
        delivery_calls.append(args[2].path)
        return 0, {"ok": True, "status": "completed"}

    monkeypatch.setattr(run_ticket, "_stage_deliver", deliver)
    assert run_ticket.main([ticket.id, "--harness", "grok"]) == 0
    assert launcher.requests == []
    assert delivery_calls == [ws.path]


def test_committed_candidate_without_plan_build_evidence_fails_closed(
    repo: SimpleNamespace, launcher: SimpleNamespace, capsys: pytest.CaptureFixture[str],
) -> None:
    ws = create_workspace("USR-205", cwd=repo.local)
    (ws.path / "committed_impl.py").write_text("x = 1\n", encoding="utf-8")
    _git(ws.path, "add", "committed_impl.py")
    _git(ws.path, "commit", "--quiet", "-m", "wip")

    assert run_ticket.main(["USR-205", "--harness", "grok"]) == 1

    assert launcher.requests == []
    assert ws.path.exists() and (ws.path / "committed_impl.py").is_file()
    assert "commits fora da base: 1" in capsys.readouterr().err


def test_prepare_workspace_reports_the_other_preserved_worktrees_without_deleting_them(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import datetime, timezone

    monkeypatch.setattr(run_ticket, "PROJECT_ROOT", repo.local)
    monkeypatch.setattr(run_ticket, "DemandsStore", _Store)
    old = create_workspace("USR-205", cwd=repo.local, now_fn=lambda: datetime(2026, 10, 10, 21, 0, 0, tzinfo=timezone.utc))
    new = create_workspace("USR-205", cwd=repo.local, now_fn=lambda: datetime(2026, 10, 10, 22, 0, 0, tzinfo=timezone.utc))
    (old.path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (new.path / "b.py").write_text("b = 1\n", encoding="utf-8")
    args = run_ticket.build_parser().parse_args(["USR-205"])

    workspace = run_ticket._prepare_workspace(args, _TICKET)

    assert workspace.path == new.path
    err = capsys.readouterr().err
    assert str(old.path) in err and "nada foi apagado" in err
    assert old.path.exists() and new.path.exists()
    assert len(_worktree_names(repo)) == 2


def test_prepare_workspace_without_a_preserved_worktree_creates_a_new_one(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(run_ticket, "PROJECT_ROOT", repo.local)
    monkeypatch.setattr(run_ticket, "DemandsStore", _Store)
    untouched = create_workspace("USR-205", cwd=repo.local)
    args = run_ticket.build_parser().parse_args(["USR-205"])

    workspace = run_ticket._prepare_workspace(args, _TICKET)

    assert workspace.path != untouched.path and workspace.path.exists()
    assert len(_worktree_names(repo)) == 2
    assert os.path.normcase(str(workspace.path.parent)) == os.path.normcase(str(repo.local / ".worktrees"))


def test_build_checkpoint_selects_its_worktree_over_a_newer_preserved_attempt(
    repo: SimpleNamespace, launcher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(repo.tmp / "state"))
    ticket = run_ticket.UserTicket(
        id="USR-902", project_id="darkfac", title="Retomar checkpoint", problem_statement="teste",
    )
    older = create_workspace(
        ticket.id, cwd=repo.local,
        now_fn=lambda: datetime(2026, 10, 10, 21, 28, 42, tzinfo=timezone.utc),
    )
    newer = create_workspace(
        ticket.id, cwd=repo.local,
        now_fn=lambda: datetime(2026, 10, 10, 22, 30, 0, tzinfo=timezone.utc),
    )
    (older.path / "slice_one.py").write_text("done = True\n", encoding="utf-8")
    (newer.path / "unrelated_attempt.py").write_text("must_not_be_selected = True\n", encoding="utf-8")

    rws = run_ticket.ticket_engine.run_workspace(ticket, older.branch, older.base_sha)
    run_ticket.ticket_engine.write_build(
        rws, "grok", ["T1"], plan_digest="plan-digest", candidate_digest="candidate-digest",
        worktree=str(older.path),
    )

    resumed = run_ticket._resume_preserved_workspace(ticket, as_json=True)

    assert resumed is not None and resumed.path == older.path
    assert (older.path / "slice_one.py").is_file()
    assert (newer.path / "unrelated_attempt.py").is_file()


def test_empty_build_checkpoint_preserves_its_clean_worktree_before_first_slice(
    repo: SimpleNamespace, launcher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(repo.tmp / "state"))
    ticket = run_ticket.UserTicket(
        id="USR-903", project_id="darkfac", title="Checkpoint antes da fatia", problem_statement="teste",
    )
    original = create_workspace(ticket.id, cwd=repo.local)
    rws = run_ticket.ticket_engine.run_workspace(ticket, original.branch, original.base_sha)
    run_ticket.ticket_engine.write_build(
        rws, "grok", [], plan_digest="plan-digest", candidate_digest="candidate-digest",
        worktree=str(original.path),
    )

    resumed = run_ticket._resume_preserved_workspace(ticket, as_json=True)

    assert resumed is not None and resumed.path == original.path
    assert original.path.exists()
    assert _worktree_names(repo) == [original.path.name]


# ---------------------------------------------------------------------------
# BUILD resumes per slice instead of replanning (USR-475 T1)
# ---------------------------------------------------------------------------


def _write_two_slice_plan(rws: Any) -> None:
    from core.line import workspace as line_workspace

    line_workspace.write_context(rws, "SPEC.md", "# SPEC\n\n## Objetivo\nDuas fatias\n")
    line_workspace.write_context(
        rws, "tickets.json",
        json.dumps([
            {"id": "T1", "title": "Primeira fatia", "acceptance": ["T1 ok"]},
            {"id": "T2", "title": "Segunda fatia", "acceptance": ["T2 ok"]},
        ]),
    )


def _write_skipped_research(rws: Any) -> None:
    from core.line import workspace as line_workspace
    from core.research.policy import ResearchArtifact

    line_workspace.write_context(
        rws, "research.json",
        ResearchArtifact(kind="research_skipped", ticket_id=rws.run_id, reason="teste").model_dump_json(),
    )


def test_build_resume_skips_a_completed_slice_and_keeps_the_preserved_plan(
    repo: SimpleNamespace, launcher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """USR-475 T1: a BUILD interrupted after slice 1 resumes at slice 2, never replanning or
    rerunning the finished slice (evidence note: before this, a resumed run replayed PLAN and
    every already-finished slice because `progress.json` was only written at the very end)."""
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(repo.tmp / "state"))
    monkeypatch.setattr("core.line.fleet_dispatcher.PROJECT_ROOT", repo.local)
    ticket = run_ticket.UserTicket(
        id="USR-900", project_id="darkfac", title="Duas fatias",
        problem_statement="plano com duas fatias de desenvolvimento",
    )
    monkeypatch.setattr(_Store, "get_ticket", lambda _self, ticket_id: ticket if ticket_id == ticket.id else None)
    ws = create_workspace(ticket.id, cwd=repo.local)
    rws = run_ticket.ticket_engine.run_workspace(ticket, ws.branch, ws.base_sha)
    run_ticket.ticket_engine.write_demand(rws, ticket)
    _write_two_slice_plan(rws)
    _write_skipped_research(rws)
    (ws.path / "t1.py").write_text("slice_one = True\n", encoding="utf-8")  # slice 1's own, uncommitted work

    candidate_digest = run_ticket._build_candidate_digest(ticket.id, ws.path)
    plan_digest = run_ticket.ticket_engine.plan_digest(rws)
    run_ticket.ticket_engine.write_build(
        rws, "grok", ["T1"],
        plan_digest=plan_digest, candidate_digest=candidate_digest, worktree=str(ws.path),
    )

    from core.line import workspace as line_workspace

    spec_path = line_workspace.context_dir(rws) / "SPEC.md"
    tickets_path = line_workspace.context_dir(rws) / "tickets.json"
    spec_contents_before = spec_path.read_text(encoding="utf-8")
    tickets_contents_before = tickets_path.read_text(encoding="utf-8")

    calls: list[AgentRequest] = []

    def fake_agent(req: AgentRequest) -> AgentResult:
        checkpoint = json.loads((line_workspace.context_dir(rws) / "progress.json").read_text(encoding="utf-8"))
        assert checkpoint["tickets_done"] == ["T1"]  # saved before the next slice starts
        assert checkpoint["ticket_id"] == ticket.id
        assert checkpoint["completed_slice_count"] == 1
        assert checkpoint["worktree"] == str(ws.path)
        assert checkpoint["plan_digest"] == run_ticket.ticket_engine.plan_digest(rws)
        assert checkpoint["candidate_digest"] == run_ticket._build_candidate_digest(ticket.id, ws.path)
        calls.append(req)
        (Path(req.cwd) / "t2.py").write_text("slice_two = True\n", encoding="utf-8")
        return AgentResult(ok=True, harness=req.harness, model=req.model, duration_s=0.1, text="ok")

    monkeypatch.setattr(run_ticket, "run_agent", fake_agent)
    monkeypatch.setattr(run_ticket, "_plan_with_agent", lambda *a, **k: pytest.fail("PLAN was re-run on resume"))

    delivery_calls: list[Path] = []

    def deliver(*args, **kwargs):
        delivery_calls.append(args[2].path)
        return 0, {"ok": True, "status": "completed"}

    monkeypatch.setattr(run_ticket, "_stage_deliver", deliver)

    assert run_ticket.main([ticket.id, "--harness", "grok"]) == 0

    assert len(calls) == 1  # only the missing slice (T2) ran an agent; T1 was never replayed
    assert "T2" in calls[0].prompt and "[PLANO 2/2]" in calls[0].prompt
    assert (ws.path / "t1.py").is_file()  # slice 1's own work was never touched
    assert (ws.path / "t2.py").is_file()
    assert spec_path.read_text(encoding="utf-8") == spec_contents_before  # PLAN was not regenerated
    assert tickets_path.read_text(encoding="utf-8") == tickets_contents_before
    progress = json.loads((line_workspace.context_dir(rws) / "progress.json").read_text(encoding="utf-8"))
    assert progress["tickets_done"] == ["T1", "T2"]
    assert progress["ticket_id"] == ticket.id and progress["completed_slice_count"] == 2
    assert progress["worktree"] == str(ws.path)
    assert progress["candidate_digest"] == run_ticket._build_candidate_digest(ticket.id, ws.path)
    assert delivery_calls == [ws.path]


def test_build_resume_fails_closed_when_candidate_evidence_diverges(
    repo: SimpleNamespace, launcher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """USR-475 T1: a preserved BUILD artifact that no longer matches the current candidate ends
    the attempt with `build_evidence_mismatch` instead of silently discarding or replaying the
    finished slice -- the worktree and the canonical progress.json are left exactly as found."""
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(repo.tmp / "state"))
    monkeypatch.setattr("core.line.fleet_dispatcher.PROJECT_ROOT", repo.local)
    ticket = run_ticket.UserTicket(
        id="USR-901", project_id="darkfac", title="Fatia divergente",
        problem_statement="plano com duas fatias, candidato divergente do progress.json preservado",
    )
    monkeypatch.setattr(_Store, "get_ticket", lambda _self, ticket_id: ticket if ticket_id == ticket.id else None)
    ws = create_workspace(ticket.id, cwd=repo.local)
    rws = run_ticket.ticket_engine.run_workspace(ticket, ws.branch, ws.base_sha)
    _write_two_slice_plan(rws)
    _write_skipped_research(rws)
    (ws.path / "t1.py").write_text("slice_one = True\n", encoding="utf-8")

    plan_digest = run_ticket.ticket_engine.plan_digest(rws)
    candidate_digest = run_ticket._build_candidate_digest(ticket.id, ws.path)
    run_ticket.ticket_engine.write_build(
        rws, "grok", ["T1"],
        plan_digest=plan_digest, candidate_digest=candidate_digest, worktree=str(ws.path),
    )
    # A local edit after the checkpoint changes the candidate even though HEAD is unchanged.
    (ws.path / "t1.py").write_text("slice_one = changed_after_checkpoint\n", encoding="utf-8")

    from core.line import workspace as line_workspace

    progress_path = line_workspace.context_dir(rws) / "progress.json"
    progress_before = progress_path.read_text(encoding="utf-8")

    calls: list[AgentRequest] = []

    def fake_agent(req: AgentRequest) -> AgentResult:
        calls.append(req)
        return AgentResult(ok=True, harness=req.harness, model=req.model, duration_s=0.1, text="ok")

    monkeypatch.setattr(run_ticket, "run_agent", fake_agent)

    assert run_ticket.main([ticket.id, "--harness", "grok"]) == 1

    assert calls == []  # the mismatch is caught before any slice (even a missing one) runs
    assert ws.path.exists() and (ws.path / "t1.py").is_file()  # worktree and finished slice survive
    assert progress_path.read_text(encoding="utf-8") == progress_before  # canonical artifact untouched
    assert ticket_workspace.list_worktrees(repo.local)  # sanity: still a healthy repo


# ---------------------------------------------------------------------------
# USR-447: a relaunch resumes at the right stage (plan and finished slices are kept)
# ---------------------------------------------------------------------------


def _two_slice_plan(rws: Any, ticket: Any) -> None:
    from core.line import workspace as line_workspace
    from core.line.stage_planning import PlanningTicket

    run_ticket.ticket_engine.write_demand(rws, ticket)
    line_workspace.write_context(rws, "SPEC.md", "# SPEC\n\nDuas fatias\n")
    items = [PlanningTicket(id=f"{ticket.id}-S{n}", title=f"fatia {n}", goal="g") for n in (1, 2)]
    line_workspace.write_context(
        rws, "tickets.json", json.dumps([i.model_dump() for i in items], ensure_ascii=False)
    )


def _resume_setup(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, *, done: list[str]
) -> tuple[Any, Any, Any, list[str]]:
    """Preserved worktree with optional built slice, a two-slice plan and BUILD progress `done`."""
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(repo.tmp / "state"))
    monkeypatch.setattr(run_ticket, "PROJECT_ROOT", repo.local)
    ticket = run_ticket.UserTicket(
        id="USR-447", project_id="darkfac", title="Retomar na etapa certa", problem_statement="problema"
    )
    ws = create_workspace(ticket.id, cwd=repo.local)
    rws = run_ticket.ticket_engine.run_workspace(ticket, ws.branch, ws.base_sha)
    _two_slice_plan(rws, ticket)
    if done:
        (ws.path / "impl.py").write_text("slice1 = True\n", encoding="utf-8")
        run_ticket.ticket_engine.write_build(
            rws, "grok", done,
            plan_digest=run_ticket.ticket_engine.plan_digest(rws),
            candidate_digest=run_ticket._build_candidate_digest(ticket.id, ws.path),
            worktree=str(ws.path),
        )
    ran: list[str] = []

    def fake_dev_agent(args: Any, ticket: Any, workspace: Any, quotas: Any, harness: Any, model: Any,
                       progress: Any, extra_prompt: str = "", pinned: Any = None) -> Any:
        ran.append(extra_prompt)
        return SimpleNamespace(
            result=AgentResult(ok=True, harness=harness, text="ok", duration_s=1.0, model="m"),
            route=None, total_duration_s=1.0,
        )

    monkeypatch.setattr(run_ticket, "_run_dev_agent", fake_dev_agent)
    monkeypatch.setattr(run_ticket, "_resolve_development_slice", lambda *a, **k: ("grok", None, None, {}))
    monkeypatch.setattr(run_ticket, "_stage_agent_early_exit", lambda *a, **k: None)
    monkeypatch.setattr(run_ticket, "check_optional_mcp_servers", lambda *a, **k: (True, []))
    monkeypatch.setattr(run_ticket, "inspect_quotas", lambda: {})
    monkeypatch.setattr(run_ticket, "record_ticket_quota_cost", lambda **k: {})
    monkeypatch.setattr(run_ticket, "format_ticket_quota_summary", lambda _r: "")
    monkeypatch.setattr("core.line.recovery_receipt.record_implementation", lambda *a, **k: None)
    return ticket, ws, rws, ran


def _args() -> SimpleNamespace:
    return SimpleNamespace(json=True, dry_run=False)


def _progress() -> SimpleNamespace:
    return SimpleNamespace(phase=lambda *a, **k: None)


def test_relaunch_with_valid_plan_does_not_call_the_planner(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.research.policy import ResearchArtifact

    ticket, ws, rws, _ran = _resume_setup(repo, monkeypatch, done=[])
    monkeypatch.setattr(run_ticket, "_refuse_plan_outside_host_contract", lambda *a, **k: None)
    monkeypatch.setattr(run_ticket, "_plan_with_agent", lambda *a, **k: pytest.fail("replanned"))

    def research(t: Any, r: Any, _root: Any) -> Any:
        artifact = ResearchArtifact(kind="research_skipped", ticket_id=t.id, reason="x")
        run_ticket.ticket_engine.write_research(r, artifact)
        return artifact

    monkeypatch.setattr(run_ticket.ticket_extras, "run_research", research)
    assert run_ticket.ticket_engine.reusable_plan(rws, ticket) is True
    assert run_ticket._stage_plan(_args(), ticket, ws, {"ticket_size": "large"}, _progress()) is None
    assert [i.id for i in run_ticket.ticket_engine.plan_tickets(rws)] == ["USR-447-S1", "USR-447-S2"]


def test_plan_of_another_demand_is_not_reused(repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    ticket, _ws, rws, _ran = _resume_setup(repo, monkeypatch, done=[])
    changed = ticket.model_copy(update={"problem_statement": "outro problema"})
    assert run_ticket.ticket_engine.reusable_plan(rws, changed) is False


def test_admission_does_not_require_a_planning_route_when_the_plan_exists(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket, _ws, _rws, _ran = _resume_setup(repo, monkeypatch, done=[])
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        run_ticket.admission, "admit", lambda **kw: seen.update(kw) or SimpleNamespace(outcome="allow")
    )
    run_ticket._admission_impl(SimpleNamespace(fleet=True, harness=""), ticket)
    assert seen["plan_exists"] is True


def test_relaunch_with_first_slice_done_runs_the_agent_only_for_the_second(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket, ws, rws, ran = _resume_setup(repo, monkeypatch, done=["USR-447-S1"])
    built = run_ticket._stage_build(_args(), ticket, ws, {}, {"harness": "grok", "ticket_size": "large"}, _progress())
    assert isinstance(built, dict)
    assert len(ran) == 1 and "sub-ticket USR-447-S2" in ran[0]
    assert run_ticket.require_artifact(run_ticket.Stage.BUILD, rws).tickets_done == ["USR-447-S1", "USR-447-S2"]


def test_relaunch_with_every_slice_done_skips_the_agent_and_reaches_the_gate(
    repo: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticket, ws, _rws, ran = _resume_setup(repo, monkeypatch, done=["USR-447-S1", "USR-447-S2"])
    built = run_ticket._stage_build(_args(), ticket, ws, {}, {"harness": "grok", "ticket_size": "large"}, _progress())
    assert ran == []
    assert isinstance(built, dict) and built["agent_result"].ok and built["harness"] == "grok"


def test_completed_build_checkpoint_goes_to_delivery_without_replanning_or_rebuild(
    repo: SimpleNamespace, launcher: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2: a completed BUILD checkpoint is the boundary for delivery-only continuation."""
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(repo.tmp / "state"))
    monkeypatch.setattr("core.line.fleet_dispatcher.PROJECT_ROOT", repo.local)
    ticket = run_ticket.UserTicket(
        id="USR-475-COMPLETE", project_id="darkfac", title="Entregar BUILD concluido",
        problem_statement="preserve completed slices and continue delivery",
    )
    monkeypatch.setattr(
        _Store, "get_ticket", lambda _self, ticket_id: ticket if ticket_id == ticket.id else None,
    )
    ws = create_workspace(ticket.id, cwd=repo.local)
    rws = run_ticket.ticket_engine.run_workspace(ticket, ws.branch, ws.base_sha)
    run_ticket.ticket_engine.write_demand(rws, ticket)
    _write_two_slice_plan(rws)
    _write_skipped_research(rws)
    (ws.path / "t1.py").write_text("slice_one = True\n", encoding="utf-8")
    (ws.path / "t2.py").write_text("slice_two = True\n", encoding="utf-8")
    run_ticket.ticket_engine.write_build(
        rws, "grok", ["T1", "T2"],
        plan_digest=run_ticket.ticket_engine.plan_digest(rws),
        candidate_digest=run_ticket._build_candidate_digest(ticket.id, ws.path),
        worktree=str(ws.path),
    )

    from core.line import workspace as line_workspace

    context = line_workspace.context_dir(rws)
    canonical_before = {
        name: (context / name).read_bytes()
        for name in ("SPEC.md", "tickets.json", "research.json", "progress.json")
    }
    monkeypatch.setattr(run_ticket, "_plan_with_agent", lambda *_a, **_k: pytest.fail("PLAN repeated"))
    monkeypatch.setattr(run_ticket, "run_agent", lambda *_a, **_k: pytest.fail("completed slice repeated"))
    delivered: list[Path] = []

    def deliver(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        delivered.append(args[2].path)
        return 0, {"ok": True, "status": "completed"}

    monkeypatch.setattr(run_ticket, "_stage_deliver", deliver)

    assert run_ticket.main([ticket.id, "--harness", "grok"]) == 0

    assert launcher.requests == []
    assert delivered == [ws.path]
    assert all((context / name).read_bytes() == content for name, content in canonical_before.items())
    assert (ws.path / "t1.py").is_file() and (ws.path / "t2.py").is_file()


@pytest.mark.parametrize("commit_change", [False, True], ids=["dirty-worktree", "commit-ahead"])
def test_missing_build_artifacts_do_not_reset_preserved_candidate(
    repo: SimpleNamespace,
    launcher: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    commit_change: bool,
) -> None:
    """T2: a candidate without its PLAN/BUILD proof fails closed before reset or new PLAN."""
    monkeypatch.setenv("DARKFAC_STATE_ROOT", str(repo.tmp / "state"))
    monkeypatch.setattr("core.line.fleet_dispatcher.PROJECT_ROOT", repo.local)
    ticket = run_ticket.UserTicket(
        id="USR-475-MISSING", project_id="darkfac", title="Checkpoint ausente",
        problem_statement="candidate preserved without its canonical PLAN/BUILD evidence",
    )
    monkeypatch.setattr(
        _Store, "get_ticket", lambda _self, ticket_id: ticket if ticket_id == ticket.id else None,
    )
    ws = create_workspace(ticket.id, cwd=repo.local)
    (ws.path / "implementation.py").write_text("completed = True\n", encoding="utf-8")
    if commit_change:
        _git(ws.path, "add", "implementation.py")
        _git(ws.path, "commit", "--quiet", "-m", "preserved implementation")

    monkeypatch.setattr(
        run_ticket.ticket_engine, "reset",
        lambda *_a, **_k: pytest.fail("missing evidence reset canonical artifacts"),
    )
    monkeypatch.setattr(
        run_ticket, "_plan_with_agent", lambda *_a, **_k: pytest.fail("missing evidence started PLAN"),
    )

    assert run_ticket.main([ticket.id, "--harness", "grok"]) == 1

    assert launcher.requests == []
    assert ws.path.exists() and (ws.path / "implementation.py").is_file()
    assert "sem artefatos PLAN/BUILD" in capsys.readouterr().err
