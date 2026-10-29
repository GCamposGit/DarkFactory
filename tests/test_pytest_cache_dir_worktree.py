"""USR-243: pytest runs must not leave a protected .pytest_cache in a worktree."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests import _cache_dir

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, timeout=90, check=False
    )


def test_external_cache_dir_is_stable_and_per_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "temp"))
    a = _cache_dir.external_cache_dir(tmp_path / "a")
    assert a == _cache_dir.external_cache_dir(tmp_path / "a")
    assert a != _cache_dir.external_cache_dir(tmp_path / "b")
    assert a == tmp_path / "temp" / "DarkFac" / "pytest-cache" / a.name


def test_worktree_remove_after_pytest_run_and_lf_persists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    assert _run([*git, "init", "-q", "-b", "main"], repo).returncode == 0
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    assert _run([*git, "add", "-A"], repo).returncode == 0
    assert _run([*git, "commit", "-q", "-m", "init"], repo).returncode == 0
    wt = tmp_path / "wt"
    assert _run([*git, "worktree", "add", "-q", "-b", "ticket/x", str(wt)], repo).returncode == 0

    (wt / "conftest.py").write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import pytest\n"
        "from tests import _cache_dir\n"
        "@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_configure(config):\n"
        "    _cache_dir.redirect_cache_dir(config)\n",
        encoding="utf-8",
    )
    (wt / "test_x.py").write_text(
        "def test_ok():\n    assert True\n\ndef test_bad():\n    assert False\n",
        encoding="utf-8",
    )
    pytest_ini = wt / "pytest.ini"
    pytest_ini.write_text("[pytest]\ntestpaths = .\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    base = [sys.executable, "-m", "pytest", "-q", "-c", str(pytest_ini), "--rootdir", str(wt)]
    first = _run(base, wt, env)
    assert first.returncode == 1, first.stdout + first.stderr
    assert not (wt / ".pytest_cache").exists()
    cache_root = Path(tempfile.gettempdir()) / "DarkFac" / "pytest-cache"
    assert any(cache_root.rglob("lastfailed"))

    second = _run([*base, "--lf"], wt, env)
    assert "test_bad" in second.stdout and "1 failed" in second.stdout, second.stdout
    assert "1 passed" not in second.stdout

    removed = _run([*git, "worktree", "remove", "--force", str(wt)], repo)
    assert removed.returncode == 0, removed.stderr
    assert not wt.exists()
