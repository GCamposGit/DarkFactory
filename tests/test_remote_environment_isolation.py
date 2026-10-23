"""Isolation guard for ambient remote worker settings (USR-267)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import AMBIENT_REMOTE_INFRA_ENV


def test_polluted_worker_environment_is_removed_before_child_tests_run(
    tmp_path: Path, monkeypatch
) -> None:
    """A fresh pytest process must scrub worker settings before importing its tests."""
    for index, name in enumerate(AMBIENT_REMOTE_INFRA_ENV):
        monkeypatch.setenv(name, f"http://unreachable-{index}.invalid:8080")

    child_test = tmp_path / "test_environment.py"
    names = repr(AMBIENT_REMOTE_INFRA_ENV)
    child_test.write_text(
        "import os\n"
        f"REMOTE_ENV_NAMES = {names}\n"
        "LEAKED_REMOTE_ENV = [name for name in REMOTE_ENV_NAMES if name in os.environ]\n"
        "def test_worker_environment_is_clean():\n"
        "    assert not LEAKED_REMOTE_ENV, f'remote worker environment leaked: {LEAKED_REMOTE_ENV}'\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--confcutdir",
            str(tmp_path),
            "-p",
            "tests.conftest",
            str(child_test),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=120,  # the child pytest starts slowly on a loaded worker (Desktop xdist run)
        check=False,
    )

    assert result.returncode == 0, f"child pytest failed:\n{result.stdout}\n{result.stderr}"
    assert "1 passed" in result.stdout
