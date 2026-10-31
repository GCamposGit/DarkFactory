"""Test suite for DarkFac Git Autonomy and Environment Synchronization (USR-57)."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from core.demands.models import DemandOrigin, LifecycleStage, PlanningHorizon, RoadmapItemType, UserTicket
from core.demands.store import DemandsStore
from core.git.autonomy import GitAutonomyManager, GitSyncResult, TicketCompletionReport
from core.orchestrator.guard import audit_paths, is_governance_evolution_authorized, main as guard_main
from core.roadmap.models import DeliveryStatus
from run_ticket import build_parser


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Execute git command in test repository."""
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    )


@pytest.fixture
def test_git_repo(tmp_path: Path) -> Path:
    """Fixture creating an initialized git repository with initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.name", "DarkFac Test")
    _git(repo, "config", "user.email", "test@darkfac.internal")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--quiet", "-m", "chore: initial commit")
    return repo


@pytest.fixture
def test_remote_pair(tmp_path: Path) -> tuple[Path, Path]:
    """Fixture creating an origin bare repo and a cloned local repo."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "--quiet", "-b", "main")

    local = tmp_path / "local"
    local.mkdir()
    _git(local, "init", "--quiet", "-b", "main")
    _git(local, "config", "user.name", "DarkFac Test")
    _git(local, "config", "user.email", "test@darkfac.internal")
    _git(local, "config", "commit.gpgsign", "false")
    (local / "README.md").write_text("# Project\n", encoding="utf-8")
    _git(local, "add", "README.md")
    _git(local, "commit", "--quiet", "-m", "chore: initial commit")
    _git(local, "remote", "add", "origin", str(bare))
    _git(local, "push", "--quiet", "-u", "origin", "main")
    return local, bare


def test_git_autonomy_get_sha_and_is_dirty(test_git_repo: Path):
    mgr = GitAutonomyManager(test_git_repo)
    sha = mgr.get_current_sha()
    assert len(sha) == 40
    assert not mgr.is_dirty()

    (test_git_repo / "dirty.txt").write_text("hello", encoding="utf-8")
    assert mgr.is_dirty()


def test_git_autonomy_commit_ticket_creates_atomic_commit(test_git_repo: Path):
    mgr = GitAutonomyManager(test_git_repo)
    (test_git_repo / "feature.py").write_text("def test(): pass\n", encoding="utf-8")

    initial_sha = mgr.get_current_sha()
    new_sha = mgr.commit_ticket(
        ticket_id="USR-57",
        title="Aumentar autonomia Git",
        scope="git",
    )

    assert new_sha != initial_sha
    assert not mgr.is_dirty()

    log_res = _git(test_git_repo, "log", "-n", "1", "--format=%B")
    commit_msg = log_res.stdout
    assert "feat(git): Aumentar autonomia Git [USR-57]" in commit_msg
    assert "DarkFac-Ticket: USR-57" in commit_msg
    assert "DarkFac-Autonomy: zero-human-touch" in commit_msg


def test_git_autonomy_commit_ticket_clean_tree_returns_head(test_git_repo: Path):
    mgr = GitAutonomyManager(test_git_repo)
    initial_sha = mgr.get_current_sha()
    result_sha = mgr.commit_ticket(
        ticket_id="USR-57",
        title="No changes",
    )
    assert result_sha == initial_sha


def test_commit_ticket_rejects_secret_in_pre_staged_index_blob(test_git_repo: Path) -> None:
    manager = GitAutonomyManager(test_git_repo)
    token = "123456789" + ":" + "Ab1_-" * 7
    target = test_git_repo / "notes.txt"
    target.write_text(f"token={token}\n", encoding="utf-8")
    _git(test_git_repo, "add", "notes.txt")
    target.write_text("clean worktree\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Staged content contains potential secrets") as exc:
        manager.commit_ticket("USR-100", "secret guard", paths=["README.md"])
    assert token not in str(exc.value)
    assert _git(test_git_repo, "log", "-1", "--format=%s").stdout.strip() == "chore: initial commit"


_SCRATCH = (
    ".tmp/harness-state-usr-423/run_timings.jsonl",
    ".tmp/harness-state-usr-423/suite-0.lock",
)
_TIMINGS = b'{"ticket":"USR-423","phase":"gate"}\n'
_LOCK = b"suite-0\n"


def _no_global_exclude(repo: Path) -> None:
    """The fixture must not ignore ``.tmp`` via a machine-wide excludes file."""
    blank = repo.parent / "blank-exclude"
    blank.write_text("", encoding="utf-8")
    _git(repo, "config", "core.excludesFile", str(blank))


def _plant_scratch(repo: Path) -> tuple[Path, Path]:
    root = repo / ".tmp" / "harness-state-usr-423"
    root.mkdir(parents=True)
    timings = root / "run_timings.jsonl"
    lock = root / "suite-0.lock"
    timings.write_bytes(_TIMINGS)
    lock.write_bytes(_LOCK)
    return timings, lock


def _assert_not_ignored(repo: Path, *paths: str) -> None:
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "--", *paths],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert ignored.returncode == 1, ignored.stdout


@pytest.mark.parametrize("staged", [False, True])
def test_commit_ticket_publishes_product_and_drops_harness_scratch(test_git_repo: Path, staged: bool) -> None:
    _no_global_exclude(test_git_repo)
    timings, lock = _plant_scratch(test_git_repo)
    (test_git_repo / "product.txt").write_text("product\n", encoding="utf-8")
    _assert_not_ignored(test_git_repo, *_SCRATCH)
    if staged:
        _git(test_git_repo, "add", "--", "product.txt", *_SCRATCH)
    head = GitAutonomyManager(test_git_repo).get_current_sha()

    sha = GitAutonomyManager(test_git_repo).commit_ticket("USR-429", "Produto")

    assert sha and sha != head
    names = _git(test_git_repo, "show", "--name-only", "--format=", "HEAD").stdout.strip().splitlines()
    assert names == ["product.txt"]
    assert timings.read_bytes() == _TIMINGS
    assert lock.read_bytes() == _LOCK
    assert _git(test_git_repo, "ls-files", "--", ".tmp").stdout.strip() == ""


@pytest.mark.parametrize("staged", [False, True])
def test_commit_ticket_scratch_only_returns_head(test_git_repo: Path, staged: bool) -> None:
    _no_global_exclude(test_git_repo)
    timings, lock = _plant_scratch(test_git_repo)
    _assert_not_ignored(test_git_repo, *_SCRATCH)
    if staged:
        _git(test_git_repo, "add", "--", *_SCRATCH)
    manager = GitAutonomyManager(test_git_repo)
    head = manager.get_current_sha()

    assert manager.commit_ticket("USR-429", "So scratch") == head

    assert _git(test_git_repo, "rev-parse", "HEAD").stdout.strip() == head
    assert _git(test_git_repo, "log", "-1", "--format=%s").stdout.strip() == "chore: initial commit"
    assert timings.read_bytes() == _TIMINGS
    assert lock.read_bytes() == _LOCK
    assert _git(test_git_repo, "ls-files", "--", ".tmp").stdout.strip() == ""


def test_commit_ticket_keeps_nested_tmp_outside_the_scratch_root(test_git_repo: Path) -> None:
    _no_global_exclude(test_git_repo)
    timings, lock = _plant_scratch(test_git_repo)
    nested = test_git_repo / "src" / ".tmp" / "keep.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("keep\n", encoding="utf-8")

    GitAutonomyManager(test_git_repo).commit_ticket("USR-429", "Aninhado")

    names = _git(test_git_repo, "show", "--name-only", "--format=", "HEAD").stdout.strip().splitlines()
    assert names == ["src/.tmp/keep.txt"]
    assert timings.read_bytes() == _TIMINGS
    assert lock.read_bytes() == _LOCK
    assert _git(test_git_repo, "ls-files", "--", ".tmp").stdout.strip() == ""
    assert _git(test_git_repo, "ls-files", "--", "src/.tmp/keep.txt").stdout.strip() == "src/.tmp/keep.txt"


def test_commit_ticket_rejects_force_added_ignored_path(test_git_repo: Path) -> None:
    manager = GitAutonomyManager(test_git_repo)
    (test_git_repo / ".gitignore").write_text("private/\n", encoding="utf-8")
    _git(test_git_repo, "add", ".gitignore")
    private = test_git_repo / "private"
    private.mkdir()
    (private / "settings.json").write_text("{}\n", encoding="utf-8")
    _git(test_git_repo, "add", "-f", "private/settings.json")

    with pytest.raises(RuntimeError, match="Refusing staged ignored path: private/settings.json"):
        manager.commit_ticket("USR-100", "ignored guard")


def test_commit_ticket_allows_justified_test_sentinel(test_git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from core.git import secret_scan

    token = "123456789" + ":" + "Ab1_-" * 7
    (test_git_repo / "sentinel.txt").write_text(token, encoding="utf-8")
    monkeypatch.setattr(secret_scan, "ALLOWLIST", (
        secret_scan.AllowlistEntry("sentinel.txt", ("telegram_bot_token",), "test sentinel"),
    ))
    assert GitAutonomyManager(test_git_repo).commit_ticket("USR-100", "sentinel")


def test_git_autonomy_sync_up_to_date(test_remote_pair: tuple[Path, Path]):
    local, _ = test_remote_pair
    mgr = GitAutonomyManager(local)

    res = mgr.sync_environment()
    assert res.ok
    assert res.action == "up_to_date"
    assert res.local_sha == res.remote_sha


def test_git_autonomy_sync_pushed_when_local_ahead(test_remote_pair: tuple[Path, Path]):
    local, _ = test_remote_pair
    mgr = GitAutonomyManager(local)

    (local / "new_feature.txt").write_text("new content\n", encoding="utf-8")
    new_sha = mgr.commit_ticket("USR-57", "Add new feature")

    # Local is now ahead of origin/main
    res = mgr.sync_environment(allow_push=True)
    assert res.ok
    assert res.action == "pushed"
    assert res.local_sha == new_sha

    # Verify origin is now up to date
    res_after = mgr.sync_environment()
    assert res_after.ok
    assert res_after.action == "up_to_date"


def test_git_autonomy_sync_fast_forward_when_remote_ahead(test_remote_pair: tuple[Path, Path], tmp_path: Path):
    local, bare = test_remote_pair

    # Clone another peer that pushes a change
    peer = tmp_path / "peer"
    peer.mkdir()
    _git(peer, "clone", "--quiet", str(bare), ".")
    _git(peer, "config", "user.name", "Peer Host")
    _git(peer, "config", "user.email", "peer@darkfac.internal")
    _git(peer, "config", "commit.gpgsign", "false")
    (peer / "peer.txt").write_text("peer commit\n", encoding="utf-8")
    _git(peer, "add", "peer.txt")
    _git(peer, "commit", "--quiet", "-m", "feat: peer contribution")
    _git(peer, "push", "--quiet", "origin", "main")

    # Now local is behind origin/main
    mgr = GitAutonomyManager(local)
    res = mgr.sync_environment()
    assert res.ok
    assert res.action == "merged_fast_forward"
    assert (local / "peer.txt").exists()


def test_git_autonomy_complete_ticket_lifecycle(test_remote_pair: tuple[Path, Path]):
    local, _ = test_remote_pair
    mgr = GitAutonomyManager(local)

    # Setup demands store inside local repo
    demands_dir = local / ".factory" / "demands"
    demands_dir.mkdir(parents=True)
    store = DemandsStore(demands_dir / "demands.json")

    ticket = UserTicket(
        id="USR-57",
        project_id="darkfac",
        title="Aumentar autonomia Git",
        origin=DemandOrigin.USER,
        status=DeliveryStatus.PLANNED,
        item_type=RoadmapItemType.FEATURE,
        lifecycle_stage=LifecycleStage.EXECUTION,
        horizon=PlanningHorizon.NOW,
        tags=["git", "autonomy"],
        problem_statement="Test problem",
        acceptance_criteria=["Passes tests"],
    )
    store.save_ticket(ticket)

    # Make code changes
    (local / "autonomy_engine.py").write_text("# Autonomous engine\n", encoding="utf-8")

    # Complete ticket
    report = mgr.complete_ticket("USR-57", cwd=local, auto_push=True, auto_commit=True)
    assert report.ok
    assert report.ticket_id == "USR-57"
    assert report.commit_sha is not None
    assert report.sync_result is not None
    assert report.sync_result.ok

    # Verify store updated to COMPLETED
    updated_ticket = store.get_ticket("USR-57")
    assert updated_ticket is not None
    assert updated_ticket.status == DeliveryStatus.COMPLETED


def test_run_ticket_parser_supports_git_autonomy_flags():
    parser = build_parser()
    args = parser.parse_args(["USR-57", "--no-commit", "--no-push"])
    assert args.ticket_id == "USR-57"
    assert args.no_commit is True
    assert args.no_push is True


def test_governance_evolution_flag_alone_never_authorizes(monkeypatch: pytest.MonkeyPatch, test_git_repo: Path):
    """USR-228: the env flag alone is never sufficient; an owner record on the trusted base is required."""
    monkeypatch.delenv("DARKFAC_ALLOW_GOVERNANCE_EVOLUTION", raising=False)
    assert not is_governance_evolution_authorized()

    monkeypatch.setenv("DARKFAC_ALLOW_GOVERNANCE_EVOLUTION", "1")
    assert not is_governance_evolution_authorized()

    (test_git_repo / "AGENTS.md").write_text("Modified agents\n", encoding="utf-8")
    _git(test_git_repo, "add", "AGENTS.md")

    monkeypatch.delenv("DARKFAC_ALLOW_GOVERNANCE_EVOLUTION", raising=False)
    assert guard_main(["HEAD", "--repo", str(test_git_repo)]) == 1

    monkeypatch.setenv("DARKFAC_ALLOW_GOVERNANCE_EVOLUTION", "true")
    assert guard_main(["HEAD", "--repo", str(test_git_repo)]) == 1


def test_sync_with_base_auto_merges_demands_json_concurrent_registrations(test_remote_pair: tuple[Path, Path]):
    """USR-112 Criterion 1 & 3: Real Git repo integration test with concurrent ticket registrations."""
    local, bare = test_remote_pair
    mgr = GitAutonomyManager(local)

    ledger_file = local / ".factory" / "demands" / "demands.json"
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    initial_demands = [
        {"id": "USR-01", "project_id": "darkfac", "title": "Base Ticket", "status": "planned"},
    ]
    ledger_file.write_text(json.dumps(initial_demands, indent=2) + "\n", encoding="utf-8")
    _git(local, "add", ".factory/demands/demands.json")
    _git(local, "commit", "-m", "chore: initialize demands ledger")
    _git(local, "push", "origin", "main")

    # Create ticket branch
    _git(local, "checkout", "-b", "ticket/usr-01")

    # Branch finishes USR-01
    branch_demands = [
        {
            "id": "USR-01",
            "project_id": "darkfac",
            "title": "Base Ticket",
            "status": "completed",
            "delivery_evidence": "abc1234",
        },
    ]
    ledger_file.write_text(json.dumps(branch_demands, indent=2) + "\n", encoding="utf-8")
    _git(local, "commit", "-am", "feat: complete USR-01")

    # Concurrently on main: clone or switch to main and add USR-02 and USR-03
    _git(local, "checkout", "main")
    main_demands = [
        {"id": "USR-01", "project_id": "darkfac", "title": "Base Ticket", "status": "planned"},
        {"id": "USR-02", "project_id": "darkfac", "title": "Concurrent Ticket 2", "status": "planned"},
        {"id": "USR-03", "project_id": "darkfac", "title": "Concurrent Ticket 3", "status": "planned"},
    ]
    ledger_file.write_text(json.dumps(main_demands, indent=2) + "\n", encoding="utf-8")
    _git(local, "commit", "-am", "chore: register USR-02 and USR-03 on main")
    _git(local, "push", "origin", "main")

    # Switch back to ticket branch
    _git(local, "checkout", "ticket/usr-01")

    # Run _sync_with_base with preserve_commits=True (implementation PR mode)
    ok, files, msg = mgr._sync_with_base("main", local, preserve_commits=True)
    assert ok is True
    assert files == []
    assert "merged" in msg

    # Verify both the branch update (USR-01 completed) and main additions (USR-02, USR-03) are preserved
    merged_content = json.loads(ledger_file.read_text(encoding="utf-8"))
    assert len(merged_content) == 3
    assert merged_content[0]["id"] == "USR-01"
    assert merged_content[0]["status"] == "completed"
    assert merged_content[0]["delivery_evidence"] == "abc1234"
    assert merged_content[1]["id"] == "USR-02"
    assert merged_content[2]["id"] == "USR-03"


def test_sync_with_base_fails_closed_on_conflicting_ticket_mutation(test_remote_pair: tuple[Path, Path]):
    """USR-112 Criterion 2 & 3: Fail-closed with ticket ID when same ticket modified incompatibly."""
    local, bare = test_remote_pair
    mgr = GitAutonomyManager(local)

    ledger_file = local / ".factory" / "demands" / "demands.json"
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    initial_demands = [
        {"id": "USR-01", "project_id": "darkfac", "title": "Base Ticket", "status": "planned"},
    ]
    ledger_file.write_text(json.dumps(initial_demands, indent=2) + "\n", encoding="utf-8")
    _git(local, "add", ".factory/demands/demands.json")
    _git(local, "commit", "-m", "chore: initialize demands ledger")
    _git(local, "push", "origin", "main")

    _git(local, "checkout", "-b", "ticket/usr-01-conflict")

    # Branch modifies USR-01
    branch_demands = [
        {"id": "USR-01", "project_id": "darkfac", "title": "Base Ticket", "status": "completed"},
    ]
    ledger_file.write_text(json.dumps(branch_demands, indent=2) + "\n", encoding="utf-8")
    _git(local, "commit", "-am", "feat: branch change USR-01")

    # Main modifies same ticket USR-01 with different status
    _git(local, "checkout", "main")
    main_demands = [
        {"id": "USR-01", "project_id": "darkfac", "title": "Base Ticket", "status": "cancelled"},
    ]
    ledger_file.write_text(json.dumps(main_demands, indent=2) + "\n", encoding="utf-8")
    _git(local, "commit", "-am", "chore: cancel USR-01 on main")
    _git(local, "push", "origin", "main")

    _git(local, "checkout", "ticket/usr-01-conflict")

    ok, files, msg = mgr._sync_with_base("main", local, preserve_commits=True)
    assert ok is False
    assert "USR-01" in msg
    assert "Conflict for ticket USR-01" in msg


def test_github_app_token_cached_and_injected_only_for_publishing(monkeypatch: pytest.MonkeyPatch) -> None:
    """USR-343 S2: App token goes to GH_TOKEN of push/pr-merge only; absent config keeps ambient env."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import httpx
    from core.git import app_token, autonomy

    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"token": "ghs_fake", "expires_at": "2030-01-01T00:00:00Z"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    env = {"GH_APP_ID": "1", "GH_APP_INSTALLATION_ID": "2", "GH_APP_PRIVATE_KEY": pem}
    app_token.clear_cache()
    assert app_token.get_app_token({}, client=client) is None
    assert app_token.get_app_token(env, client=client, now=1_000.0) == "ghs_fake"
    assert app_token.get_app_token(env, client=client, now=2_000.0) == "ghs_fake"
    assert len(calls) == 1 and calls[0].url.path == "/app/installations/2/access_tokens"
    assert b"pull_requests" in calls[0].content
    assert app_token.get_app_token(env, client=client, now=1_893_456_000.0 - 100) == "ghs_fake"
    assert len(calls) == 2  # inside the 5 min refresh margin
    app_token.clear_cache()

    monkeypatch.setattr(app_token, "get_app_token", lambda *a, **k: "ghs_fake")
    assert autonomy._app_env(["pr", "merge", "1"], autonomy._PUBLISHING_GH)["GH_TOKEN"] == "ghs_fake"
    assert autonomy._app_env(["pr", "view", "1"], autonomy._PUBLISHING_GH) is None
    monkeypatch.setattr(app_token, "get_app_token", lambda *a, **k: None)
    assert autonomy._app_env(["push", "origin"], frozenset({"push"})) is None
