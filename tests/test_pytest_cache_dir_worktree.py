"""USR-243 / USR-421: pytest cache stays outside the worktree and the focus exits."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests import _cache_dir

REPO_ROOT = Path(__file__).resolve().parent.parent
# Product deadline is pytest.ini ``timeout = 120``. This bound is stricter so a
# session stuck in Cache.set fails here instead of waiting for that deadline.
_PYTEST_DEADLINE_SEC = 60
_SUMMARY_LINE = re.compile(
    r"^(?:=+\s+)?\d+ [A-Za-z]\w*(?:, \d+ [A-Za-z]\w*)* in \d+(?:\.\d+)?s(?:\s+=+)?$"
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_FOCUS_FILE = "tests/test_fleet_dispatcher.py"


def _run(
    args: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    *,
    timeout: int = _PYTEST_DEADLINE_SEC,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _pytest_summary_line(output: str) -> str:
    for line in output.splitlines():
        stripped = line.strip()
        if _SUMMARY_LINE.fullmatch(stripped):
            return stripped
    raise AssertionError(f"pytest summary line missing\n{output}")


def test_external_cache_dir_is_stable_and_per_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "temp"))
    a = _cache_dir.external_cache_dir(tmp_path / "a")
    assert a == _cache_dir.external_cache_dir(tmp_path / "a")
    assert a != _cache_dir.external_cache_dir(tmp_path / "b")
    assert a == tmp_path / "temp" / "DarkFac" / "pytest-cache" / a.name


def test_worktree_remove_after_pytest_run_and_lf_persists(tmp_path: Path) -> None:
    assert _PYTEST_DEADLINE_SEC < 120
    user_temp = Path(tempfile.gettempdir())
    probe = user_temp / f".darkfac-usr421-temp-probe-{os.getpid()}"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()

    unusable_local = tmp_path / "unusable-localappdata"
    unusable_local.write_text("not a directory\n", encoding="utf-8")

    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    assert _run([*git, "init", "-q", "-b", "main"], repo).returncode == 0
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    assert _run([*git, "add", "-A"], repo).returncode == 0
    assert _run([*git, "commit", "-q", "-m", "init"], repo).returncode == 0
    wt = tmp_path / "wt"
    assert _run([*git, "worktree", "add", "-q", "-b", "ticket/x", str(wt)], repo).returncode == 0
    cache = _cache_dir.external_cache_dir(wt)
    assert cache.is_relative_to(user_temp)

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
    env["LOCALAPPDATA"] = str(unusable_local)
    env["TEMP"] = str(user_temp)
    env["TMP"] = str(user_temp)
    env["PY_COLORS"] = "0"
    env["NO_COLOR"] = "1"
    env.pop("FORCE_COLOR", None)
    base = [sys.executable, "-m", "pytest", "-q", "-c", str(pytest_ini), "--rootdir", str(wt)]
    try:
        first = _run(base, wt, env)
        assert first.returncode == 1, first.stdout + first.stderr
        _pytest_summary_line(first.stdout)
        assert not (wt / ".pytest_cache").exists()
        lastfailed = cache / "v" / "cache" / "lastfailed"
        assert lastfailed.is_file()
        assert "test_bad" in lastfailed.read_text(encoding="utf-8")

        second = _run([*base, "--lf"], wt, env)
        summary = _pytest_summary_line(second.stdout)
        assert "test_bad" in second.stdout and "1 failed" in summary, second.stdout
        assert "1 passed" not in summary
        assert not (wt / ".pytest_cache").exists()

        removed = _run([*git, "worktree", "remove", "--force", str(wt)], repo)
        assert removed.returncode == 0, removed.stderr
        assert not wt.exists()
    finally:
        shutil.rmtree(cache, ignore_errors=True)


def test_fleet_dispatcher_focus_exits_with_passed_summary(tmp_path: Path) -> None:
    """The real focus, with this repo's pytest.ini, must exit and print a summary.

    ``-p no:cacheprovider`` and an empty ``addopts`` would hide the sessionfinish
    path that stayed open at 100%. The child deadline stays under ``timeout = 120``.
    """

    ini_path = REPO_ROOT / "pytest.ini"
    ini_text = ini_path.read_text(encoding="utf-8")
    timeout_match = re.search(r"(?m)^timeout\s*=\s*(\d+)\s*$", ini_text)
    assert timeout_match is not None
    configured_timeout = int(timeout_match.group(1))
    assert configured_timeout == 120
    assert re.search(r"(?m)^timeout_method\s*=\s*thread\s*$", ini_text)
    assert _PYTEST_DEADLINE_SEC < configured_timeout

    unusable_local = tmp_path / "unusable-localappdata"
    unusable_local.write_text("not a directory\n", encoding="utf-8")
    user_temp = Path(tempfile.gettempdir())
    probe = user_temp / f".darkfac-usr421-focus-probe-{os.getpid()}"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()

    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    env["LOCALAPPDATA"] = str(unusable_local)
    env["TEMP"] = str(user_temp)
    env["TMP"] = str(user_temp)
    env["PY_COLORS"] = "0"
    env["NO_COLOR"] = "1"
    env.pop("FORCE_COLOR", None)

    command = [
        sys.executable,
        "-m",
        "pytest",
        _FOCUS_FILE,
        "-q",
        "--color=no",
        "-c",
        str(ini_path),
        "--rootdir",
        str(REPO_ROOT),
    ]
    assert command.count(_FOCUS_FILE) == 1
    assert "-p" not in command
    assert "-o" not in command
    assert "--override-ini" not in command
    assert "no:cacheprovider" not in command
    assert not any("addopts" in part for part in command)

    completed = _run(command, REPO_ROOT, env, timeout=_PYTEST_DEADLINE_SEC)
    combined = _ANSI.sub("", f"{completed.stdout}\n{completed.stderr}")
    assert completed.returncode == 0, combined
    summary = _pytest_summary_line(combined)
    assert re.search(r"\b\d+ passed\b", summary), summary
    assert not re.search(r"\bfailed\b", summary), summary
