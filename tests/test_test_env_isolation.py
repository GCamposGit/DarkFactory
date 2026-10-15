"""Guard: tests never inherit pointers to real remote workers from the launching environment."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.conftest import AMBIENT_REMOTE_INFRA_ENV

ROOT = Path(__file__).resolve().parent.parent


def test_list_covers_the_worker_pointers() -> None:
    assert "DARKFAC_TEST_WORKERS" in AMBIENT_REMOTE_INFRA_ENV
    assert "DARKFAC_WORKER_HEALTH_TIMEOUT_SEC" in AMBIENT_REMOTE_INFRA_ENV
    assert "DARKFAC_REMOTE_HARNESS" in AMBIENT_REMOTE_INFRA_ENV


def test_child_pytest_with_polluted_env_sees_clean_environment(tmp_path: Path) -> None:
    probe = tmp_path / "test_probe_env.py"
    probe.write_text(
        "import os\n"
        "NAMES = %r\n"
        "def test_clean():\n"
        "    assert [n for n in NAMES if n in os.environ] == []\n"
        "def test_test_can_still_set(monkeypatch):\n"
        "    monkeypatch.setenv('DARKFAC_TEST_WORKERS', 'http://x')\n"
        "    assert os.environ['DARKFAC_TEST_WORKERS'] == 'http://x'\n" % (list(AMBIENT_REMOTE_INFRA_ENV),),
        encoding="utf-8",
    )
    env = {**os.environ, **{name: "http://100.64.0.1:8080" for name in AMBIENT_REMOTE_INFRA_ENV}}
    env["DARKFAC_SUITE_LOCK"] = "0"
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-q", "-n", "0", "-p", "no:cacheprovider",
            "-p", "tests.conftest", "--rootdir", str(tmp_path), str(probe),
        ],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
