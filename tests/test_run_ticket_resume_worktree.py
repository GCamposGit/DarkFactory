"""run_ticket reuses the worktree an interrupted attempt preserved (USR-225).

Incident 11/10/2026: `run_ticket USR-205 --harness grok` hit the 1800 s timeout with the implementation
already in its worktree; the retry policy preserved it, but the NEXT `run_ticket USR-205` created a brand
new worktree and the agent started from scratch. The USR-113 continuation notice only looked at the
worktree of the current run, so a second invocation was not covered.

Every repository here is a temporary one under ``tmp_path``; nothing touches the real ``.worktrees``.
"""

from __future__ import annotations

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


def test_second_invocation_reuses_the_worktree_and_carries_the_continuation_notice(
    repo: SimpleNamespace, launcher: SimpleNamespace, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # USR-256 gave a timeout with real changes in the worktree its own path (advance straight to the
    # focal pre-test/gate/self-correction loop instead of failing outright); that path is covered on its
    # own in tests/test_run_ticket_routing.py. This test is about an older, separate concern -- CLI
    # worktree reuse and the continuation notice across two *separate* invocations -- so it forces the
    # plain timeout-failure branch here regardless of the real on-disk changes the fake agent leaves.
    monkeypatch.setattr(run_ticket.agent_retry, "default_worktree_has_changes", lambda cwd: False)

    assert run_ticket.main(["USR-205", "--harness", "grok"]) == 1  # attempt 1: timeout, work preserved
    first_cwd = Path(launcher.requests[-1].cwd)
    assert _worktree_names(repo) == [first_cwd.name]
    assert "AVISO DE CONTINUA" not in launcher.requests[0].prompt  # nothing existed yet
    first_calls = len(launcher.requests)
    capsys.readouterr()

    assert run_ticket.main(["USR-205", "--harness", "grok"]) == 1  # attempt 2: times out again

    second_requests = launcher.requests[first_calls:]
    assert second_requests, "the agent was not run on the second invocation"
    assert {Path(r.cwd) for r in second_requests} == {first_cwd}  # the SAME worktree
    assert _worktree_names(repo) == [first_cwd.name]  # no new worktree was created
    prompt = second_requests[0].prompt
    assert "AVISO DE CONTINUA" in prompt and "impl.py" in prompt and "NÃO recomece a implementação do zero" in prompt
    assert "Reaproveitando a worktree preservada" in capsys.readouterr().err
    assert (first_cwd / "impl.py").read_text(encoding="utf-8") == "partial = True\n"  # the work survived


def test_continuation_notice_also_covers_work_that_was_already_committed(
    repo: SimpleNamespace, launcher: SimpleNamespace
) -> None:
    ws = create_workspace("USR-205", cwd=repo.local)
    (ws.path / "committed_impl.py").write_text("x = 1\n", encoding="utf-8")
    _git(ws.path, "add", "committed_impl.py")
    _git(ws.path, "commit", "--quiet", "-m", "wip")

    run_ticket.main(["USR-205", "--harness", "grok"])

    assert Path(launcher.requests[0].cwd) == ws.path
    assert "AVISO DE CONTINUA" in launcher.requests[0].prompt
    assert "committed_impl.py" in launcher.requests[0].prompt


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
    assert ticket_workspace.list_worktrees(repo.local)  # sanity: still a healthy repo
