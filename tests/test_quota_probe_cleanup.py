"""Claude headless quota probe stays fail-closed when sandbox cleanup fails (USR-457).

Production evidence: preflight reported codex quota at 42%, but at process shutdown
``tempfile.TemporaryDirectory._cleanup`` raised ``PermissionError [WinError 5]``
removing the ``darkfac-quota-probe`` sandbox. ``_probe_claude_headless`` now opens its
scratch cwd via ``core.common.tempdirs.probe_sandbox``, whose cleanup failure is logged
and never raised (see ``tests/test_tempdirs.py``). This test checks the call site: a
cleanup failure must not change the probe's fail-closed answer (``None`` when no
window is readable) and must not escape as an exception.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

import pytest

from core.common import tempdirs
from core.usage import adapters
from core.usage.adapters import ClaudeCodeAccountAdapter, ProviderFamily, ProviderSpec


def _deny_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    def _denied(path):  # noqa: ANN001 - matches tempdirs.robust_rmtree's signature
        exc = PermissionError("[WinError 5] Access is denied")
        exc.winerror = 5  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(tempdirs, "robust_rmtree", _denied)


class _FakeProcess:
    pid = 12345
    returncode = 0

    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout

    def communicate(self, timeout=None):  # noqa: ANN001
        return self.stdout, ""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):  # noqa: ANN001
        return self.returncode


def test_probe_claude_headless_stays_fail_closed_when_sandbox_cleanup_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _deny_cleanup(monkeypatch)
    monkeypatch.setattr("core.usage.adapters.subprocess.Popen", lambda *a, **k: _FakeProcess())
    monkeypatch.setattr("core.usage.adapters._terminate_probe_process_tree", lambda process: None)

    spec = ProviderSpec("anthropic", "Anthropic / Claude", ProviderFamily.FRONTIER, "https://claude.ai/settings/usage")
    adapter = ClaudeCodeAccountAdapter(spec, tmp_path)

    with caplog.at_level(logging.WARNING, logger="darkfac.common.tempdirs"):
        result = adapter._probe_claude_headless("claude")

    assert result is None  # fail-closed: no quota window was readable
    assert "winerror=5" in caplog.text  # the cleanup failure is a structured log, not a swallowed exception


def test_probe_claude_headless_still_returns_a_window_when_cleanup_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A readable window must win over an unrelated sandbox cleanup failure."""
    _deny_cleanup(monkeypatch)

    class _WithWindowResult:
        returncode = 0
        stdout = (
            '{"type": "rate_limit_event", "rate_limit_info": '
            '{"unifiedWindows": {"five_hour": {"utilization": 0.42}}}}\n'
        )
        stderr = ""

    monkeypatch.setattr(
        "core.usage.adapters.subprocess.Popen",
        lambda *a, **k: _FakeProcess(_WithWindowResult.stdout),
    )
    monkeypatch.setattr("core.usage.adapters._terminate_probe_process_tree", lambda process: None)

    spec = ProviderSpec("anthropic", "Anthropic / Claude", ProviderFamily.FRONTIER, "https://claude.ai/settings/usage")
    adapter = ClaudeCodeAccountAdapter(spec, tmp_path)

    result = adapter._probe_claude_headless("claude")

    assert result is not None
    assert result.quota_supported is True
    assert any(w.used_percent == 42.0 for w in result.windows)


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL and process tree integration")
def test_real_probe_subprocess_with_restricted_acl_is_cleaned_without_finalizer_error() -> None:
    """Kill a real process tree before cleaning a sandbox containing a restricted ACL file."""
    username = subprocess.check_output(["whoami"], text=True).strip()
    with tempdirs.probe_sandbox(prefix="darkfac-quota-probe-integration-") as directory:
        ready = directory / "ready.flag"
        worker = (
            "from pathlib import Path; import subprocess, sys, time; "
            "p=Path('restricted.txt'); p.write_text('probe', encoding='utf-8'); "
            "subprocess.run(['icacls', str(p), '/inheritance:r', '/grant:r', 'SYSTEM:(F)', "
            "'/deny', sys.argv[1] + ':(D)'], check=True, capture_output=True); "
            "Path('ready.flag').write_text('ready', encoding='utf-8'); time.sleep(30)"
        )
        parent = (
            "import subprocess, sys, time; "
            "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]], "
            "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
            "time.sleep(30)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", parent, worker, username], cwd=directory,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.05)
        assert ready.exists(), "ACL restricted subprocess did not become ready"
        adapters._terminate_probe_process_tree(process)
        assert process.poll() is not None
        assert (directory / "restricted.txt").is_file()
