"""scripts/line_validate.py: official runner on a clean tree or on a snapshot commit of a dirty one."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def line_validate():
    spec = importlib.util.spec_from_file_location("line_validate_under_test", REPO_ROOT / "scripts" / "line_validate.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repo(tmp_path: Path, steps: list[dict], extra_files: dict[str, str] | None = None) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "harness.config.json").write_text(json.dumps({"steps": steps}), encoding="utf-8")
    for rel, content in (extra_files or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.test")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_is_clean_tree_detects_untracked_and_modified_files(line_validate, tmp_path: Path) -> None:
    root = _repo(tmp_path, [{"name": "s", "cmd": "python -c pass", "quick": True}])
    assert line_validate.is_clean_tree(root) is True
    (root / "new.txt").write_text("x", encoding="utf-8")
    assert line_validate.is_clean_tree(root) is False


def test_is_clean_tree_fails_closed_outside_a_repository(line_validate, tmp_path: Path) -> None:
    assert line_validate.is_clean_tree(tmp_path / "nowhere") is False


def test_dirty_tree_without_a_snapshot_runs_quick_steps_directly_and_stops_at_first_failure(
    line_validate, tmp_path: Path, capsys
) -> None:
    steps = [
        {"name": "ok", "cmd": "python -c \"open('ran_ok', 'w').close()\"", "quick": True},
        {"name": "skipped_not_quick", "cmd": "python -c \"open('ran_slow', 'w').close()\"", "quick": False},
        {"name": "boom", "cmd": "python -c \"import sys; sys.exit(3)\"", "quick": True},
        {"name": "never", "cmd": "python -c \"open('ran_never', 'w').close()\"", "quick": True},
    ]
    root = _repo(tmp_path, steps)
    (root / "dirty.txt").write_text("x", encoding="utf-8")

    assert line_validate.main(root) == 3
    assert (root / "ran_ok").exists()
    assert not (root / "ran_slow").exists()
    assert not (root / "ran_never").exists()
    out = capsys.readouterr().out
    assert "boom" in out
    # the tiny repo has no core/harness/runner.py, so the snapshot path is unavailable: loud fallback
    assert "WARNING: snapshot unavailable" in out


def test_dirty_tree_passes_when_every_quick_step_passes(line_validate, tmp_path: Path) -> None:
    root = _repo(tmp_path, [{"name": "ok", "cmd": "python -c pass", "quick": True}])
    (root / "dirty.txt").write_text("x", encoding="utf-8")
    assert line_validate.main(root) == 0


def test_clean_tree_delegates_to_the_official_runner(line_validate, tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path, [{"name": "ok", "cmd": "python -c pass", "quick": True}])
    monkeypatch.setenv("DOKPLOY_API_KEY", "prod-key")
    calls: list[tuple[list[str], dict]] = []
    monkeypatch.setattr(line_validate.subprocess, "call", lambda argv, **kw: calls.append((list(argv), kw)) or 0)

    assert line_validate.main(root) == 0
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[0] == sys.executable
    assert argv[1].endswith("runner.py") and argv[-1] == "--quick"
    assert "DOKPLOY_API_KEY" not in kwargs["env"]


def test_sanitized_env_drops_production_credentials_but_keeps_test_worker_dispatch(line_validate) -> None:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/darkfac",
        "DARKFAC_HF02_DATABASE_URL": "postgresql://u:p@db:5432/x",
        "DARKHUB_CONTROL_DATABASE_URL": "postgresql://u:p@db:5432/x",
        "DOKPLOY_API_URL": "https://dokploy.example",
        "DOKPLOY_API_KEY": "k",
        "TELEGRAM_OWNER_BOT_TOKEN": "t",
        "GITHUB_TOKEN": "g",
        "GITHUB_PAT": "g",
        "CLAUDE_CODE_OAUTH_TOKEN": "c",
        "OPENAI_API_KEY": "o",
        "R2_ACCESS_KEY_ID": "r",
        "DARKFAC_BACKUP_ENCRYPTION_KEY": "e",
        "DARKFAC_CANARY_BASE_URL": "https://canary.example",
        "DARKFAC_WORKER_TOKEN": "w",
        "DARKFAC_TEST_WORKERS": "http://100.78.181.90:8080",
        "DARKFAC_REMOTE_BUSY_WAIT_SEC": "300",
        # cloud worker container config that tests read from the ambient environment
        "DARKFAC_CODEX_SANDBOX_MODE": "bypass",
        "DARKFAC_MAX_CONCURRENT_SLOTS": "1",
        "DARKFAC_WORKSPACES": "/workspaces",
        "DARKFAC_OPENROUTER_CHEAP_MODEL": "x/y",
        "DATA_DIR": "/app/data",
        "FACTORY_DIR": "/app/.factory",
        "OLLAMA_BASE_URL": "http://ollama:11434",
        "REMOTE_HARNESS_URL": "http://100.78.181.90:8080",
        "REMOTE_HARNESS_URLS": "http://a:8080,http://b:8080",
    }
    env = line_validate.sanitized_env(base)
    assert set(env) == {
        "PATH",
        "HOME",
        "DARKFAC_WORKER_TOKEN",
        "DARKFAC_TEST_WORKERS",
        "DARKFAC_REMOTE_BUSY_WAIT_SEC",
    }


def test_dirty_tree_steps_do_not_see_production_credentials(line_validate, tmp_path: Path, monkeypatch) -> None:
    cmd = "python -c \"import os; open('seen', 'w').write(os.environ.get('DARKFAC_HF02_DATABASE_URL', 'absent'))\""
    root = _repo(tmp_path, [{"name": "probe", "cmd": cmd, "quick": True}])
    (root / "dirty.txt").write_text("x", encoding="utf-8")
    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://u:p@db:5432/x")

    assert line_validate.main(root) == 0
    assert (root / "seen").read_text(encoding="utf-8") == "absent"


# --- dirty tree: snapshot commit + official runner in a temporary checkout ------------------------

_STUB_RUNNER = """
import os, subprocess, sys
from pathlib import Path

status = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all"], capture_output=True, text=True).stdout
lines = []
def print(text):  # stdout may not exist (pythonw.exe on the Windows test worker): report through a file
    lines.append(text)
print("STUB_CWD=" + os.getcwd())
print("STUB_ARGV=" + " ".join(sys.argv[1:]))
print("STUB_CLEAN=" + str(not status.strip()))
print("STUB_TRACKED=" + Path("tracked.txt").read_text())
print("STUB_NEW=" + (Path("new.txt").read_text() if Path("new.txt").exists() else "absent"))
print("STUB_IGNORED=" + ("present" if Path("ignored.log").exists() else "absent"))
print("STUB_SANDBOX=" + os.environ.get("DARKFAC_CODEX_SANDBOX_MODE", "absent"))
print("STUB_DB=" + os.environ.get("DARKFAC_HF02_DATABASE_URL", "absent"))
Path(os.environ["STUB_OUT"]).write_text("\\n".join(lines), encoding="utf-8")
sys.exit(int(os.environ.get("STUB_EXIT", "0")))
"""


def _git_out(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _snapshot_repo(tmp_path: Path) -> Path:
    root = _repo(
        tmp_path,
        [{"name": "never_direct", "cmd": "python -c \"raise SystemExit(99)\"", "quick": True}],
        extra_files={
            "core/harness/runner.py": _STUB_RUNNER,
            "tracked.txt": "committed",
            ".gitignore": "ignored.log\n",
        },
    )
    (root / "tracked.txt").write_text("edited", encoding="utf-8")
    (root / "new.txt").write_text("brand new", encoding="utf-8")
    (root / "ignored.log").write_text("noise", encoding="utf-8")
    return root


def _stub_fields(out_file: Path) -> dict[str, str]:
    text = out_file.read_text(encoding="utf-8")
    return dict(line.split("=", 1) for line in text.splitlines() if line.startswith("STUB_"))


def test_dirty_tree_runs_the_official_runner_on_a_clean_snapshot_commit(
    line_validate, tmp_path: Path, monkeypatch
) -> None:
    root = _snapshot_repo(tmp_path)
    out_file = tmp_path / "stub_out.txt"
    monkeypatch.setenv("STUB_OUT", str(out_file))
    monkeypatch.setenv("DARKFAC_CODEX_SANDBOX_MODE", "bypass")
    monkeypatch.setenv("DARKFAC_HF02_DATABASE_URL", "postgresql://u:p@db:5432/x")
    head_before = _git_out(root, "rev-parse", "HEAD")
    branch_before = _git_out(root, "rev-parse", "--abbrev-ref", "HEAD")
    status_before = _git_out(root, "status", "--porcelain=v1", "--untracked-files=all")

    assert line_validate.main(root) == 0

    fields = _stub_fields(out_file)
    checkout = Path(fields["STUB_CWD"])
    assert checkout.resolve() != root.resolve()
    assert fields["STUB_ARGV"] == "--quick"
    assert fields["STUB_CLEAN"] == "True"  # the runner's own clean-tree check would pass
    assert fields["STUB_TRACKED"] == "edited"  # tracked edit is in the snapshot
    assert fields["STUB_NEW"] == "brand new"  # untracked file is in the snapshot
    assert fields["STUB_IGNORED"] == "absent"  # gitignored noise is not
    assert fields["STUB_SANDBOX"] == "absent"  # runner env is sanitized
    assert fields["STUB_DB"] == "absent"
    # the real repository is exactly as before: HEAD, branch, index, working tree
    assert _git_out(root, "rev-parse", "HEAD") == head_before
    assert _git_out(root, "rev-parse", "--abbrev-ref", "HEAD") == branch_before
    assert _git_out(root, "status", "--porcelain=v1", "--untracked-files=all") == status_before
    assert _git_out(root, "diff", "--cached", "--name-only") == ""
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "edited"
    # temporary checkout and its scratch directory are gone
    assert not checkout.exists()
    assert not checkout.parent.exists()
    assert _git_out(root, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_snapshot_run_propagates_the_runner_exit_code_and_still_cleans_up(
    line_validate, tmp_path: Path, monkeypatch
) -> None:
    root = _snapshot_repo(tmp_path)
    out_file = tmp_path / "stub_out.txt"
    monkeypatch.setenv("STUB_OUT", str(out_file))
    monkeypatch.setenv("STUB_EXIT", "7")

    assert line_validate.main(root) == 7

    checkout = Path(_stub_fields(out_file)["STUB_CWD"])
    assert not checkout.exists()
    assert _git_out(root, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_snapshot_commit_is_parented_on_head_and_leaves_the_real_index_alone(line_validate, tmp_path: Path) -> None:
    root = _snapshot_repo(tmp_path)
    head = _git_out(root, "rev-parse", "HEAD")

    sha = line_validate.build_snapshot_commit(root)

    assert _git_out(root, "rev-parse", f"{sha}^") == head
    assert _git_out(root, "show", f"{sha}:tracked.txt") == "edited"
    assert _git_out(root, "show", f"{sha}:new.txt") == "brand new"
    names = _git_out(root, "ls-tree", "-r", "--name-only", sha).splitlines()
    assert "ignored.log" not in names
    assert _git_out(root, "rev-parse", "HEAD") == head
    assert _git_out(root, "diff", "--cached", "--name-only") == ""
    assert "?? new.txt" in _git_out(root, "status", "--porcelain=v1", "--untracked-files=all")


def test_snapshot_failure_falls_back_to_direct_steps_with_a_warning(
    line_validate, tmp_path: Path, monkeypatch, capsys
) -> None:
    root = _snapshot_repo(tmp_path)
    (root / "harness.config.json").write_text(
        json.dumps({"steps": [{"name": "ok", "cmd": "python -c \"open('ran_direct', 'w').close()\"", "quick": True}]}),
        encoding="utf-8",
    )

    def _boom(_root: Path) -> str:
        raise line_validate.SnapshotError("git is broken")

    monkeypatch.setattr(line_validate, "build_snapshot_commit", _boom)

    assert line_validate.main(root) == 0
    assert (root / "ran_direct").exists()
    assert "WARNING: snapshot unavailable (git is broken)" in capsys.readouterr().out


def test_worktree_add_failure_cleans_scratch_and_falls_back(line_validate, tmp_path: Path, monkeypatch, capsys) -> None:
    root = _snapshot_repo(tmp_path)
    (root / "harness.config.json").write_text(
        json.dumps({"steps": [{"name": "ok", "cmd": "python -c pass", "quick": True}]}), encoding="utf-8"
    )
    real_git = line_validate._git
    scratch_dirs: list[Path] = []

    def _git_failing_worktree_add(root_arg: Path, *args: str, env=None) -> str:
        if args[:2] == ("worktree", "add"):
            scratch_dirs.append(Path(args[3]).parent)
            raise line_validate.SnapshotError("worktree add refused")
        return real_git(root_arg, *args, env=env)

    monkeypatch.setattr(line_validate, "_git", _git_failing_worktree_add)

    assert line_validate.main(root) == 0
    assert "WARNING: snapshot unavailable (worktree add refused)" in capsys.readouterr().out
    assert scratch_dirs and not scratch_dirs[0].exists()


def test_clean_tree_does_not_build_a_snapshot(line_validate, tmp_path: Path, monkeypatch) -> None:
    root = _repo(tmp_path, [{"name": "ok", "cmd": "python -c pass", "quick": True}])
    monkeypatch.setattr(line_validate.subprocess, "call", lambda argv, **kw: 0)

    def _unexpected(_root: Path) -> str:
        raise AssertionError("snapshot built for a clean tree")

    monkeypatch.setattr(line_validate, "build_snapshot_commit", _unexpected)
    assert line_validate.main(root) == 0


def test_sanitized_env_drops_routing_operating_harness_and_onprem_backup_config(line_validate) -> None:
    base = {
        "PATH": "/usr/bin",
        "DARKFAC_ROUTING_UNKNOWN_QUOTA": "last_resort",
        "DARKFAC_ROUTING_ANYTHING_ELSE": "1",
        "DARKFAC_OPERATING_HARNESS": "claude",
        "DARKFAC_ONPREM_BACKUP_DIR": "E:\\DarkFac\\Backups",
    }
    assert line_validate.sanitized_env(base) == {"PATH": "/usr/bin"}


def test_runner_env_caps_xdist_workers_off_windows_unless_already_set(line_validate) -> None:
    base = {"PATH": "/usr/bin", "DARKFAC_TEST_WORKERS": "http://w:8080"}
    capped = line_validate.runner_env(base, platform="linux")
    assert capped["PYTEST_XDIST_AUTO_NUM_WORKERS"] == line_validate.LOCAL_XDIST_WORKERS
    assert capped["DARKFAC_TEST_WORKERS"] == "http://w:8080"
    assert "PYTEST_XDIST_AUTO_NUM_WORKERS" not in line_validate.runner_env(base, platform="win32")
    explicit = line_validate.runner_env({**base, "PYTEST_XDIST_AUTO_NUM_WORKERS": "4"}, platform="linux")
    assert explicit["PYTEST_XDIST_AUTO_NUM_WORKERS"] == "4"
