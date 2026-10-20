"""USR-315: the grokbox supervisor is the versioned bash pair in scripts/grokbox (not a second supervisor)."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUP = ROOT / "scripts" / "grokbox" / "grokbox-supervisor.sh"
COMMON = ROOT / "scripts" / "grokbox" / "grokbox-common.sh"


def _main_loop(text: str) -> str:
    return text[text.index("# ---------------- main loop ----------------"):]


def test_reload_rule_runs_every_tick_not_only_in_deep_checks() -> None:
    text = SUP.read_text(encoding="utf-8")
    loop = _main_loop(text)
    tick_line = next(line for line in loop.splitlines() if "check_worker" in line)
    assert "reload_if_repo_moved" in tick_line, "the HEAD-moved reload must run on every 30 s tick"
    deep = text[text.index("deep_checks(){"): text.index("# ---------------- maintenance")]
    assert "reloading idle worker" not in deep


def test_reload_uses_sha_seen_at_instance_start_and_waits_for_idle() -> None:
    text = SUP.read_text(encoding="utf-8")
    body = text[text.index("reload_if_repo_moved(){"): text.index("repo_info(){")]
    assert "W_BOOT_SHA" in body and "worker_idle" in body
    assert re.search(r'RELOAD_MIN_UPTIME_SEC:-60', text)
    # the boot sha is captured only when a NEW instance answers
    assert 'if [ "$inst" != "$W_INST" ]; then W_INST=$inst; W_BOOT_SHA=$sha' in text


def test_no_python_supervisor_competes_with_the_bash_one() -> None:
    assert not (ROOT / "scripts" / "grokbox_supervisor.py").exists()


@pytest.mark.skipif(
    shutil.which("bash") is None or sys.platform == "win32",
    reason="the scripts run only on the Linux grokbox VM; Windows 'bash' may be the WSL launcher without a distro",
)
def test_scripts_parse_with_bash() -> None:
    for script in (SUP, COMMON):
        res = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert res.returncode == 0, res.stderr
