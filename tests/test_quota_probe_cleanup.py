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

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

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
    assert adapter.last_probe_cleanup is not None
    assert adapter.last_probe_cleanup.ok is False
    assert adapter.last_probe_cleanup.error_kind == "PermissionError"
    assert adapter.last_probe_cleanup.winerror == 5
    assert "winerror=5" in caplog.text  # the cleanup failure is a structured log, not a swallowed exception
    monkeypatch.undo()
    if adapter.last_probe_cleanup.path.exists():
        tempdirs.robust_rmtree(adapter.last_probe_cleanup.path)


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
    assert adapter.last_probe_cleanup is not None
    assert adapter.last_probe_cleanup.ok is False
    assert adapter.last_probe_cleanup.winerror == 5
    monkeypatch.undo()
    if adapter.last_probe_cleanup.path.exists():
        tempdirs.robust_rmtree(adapter.last_probe_cleanup.path)


@pytest.mark.skipif(os.name != "nt", reason="Windows file sharing and process tree integration")
def test_real_quota_probe_subprocess_reaps_locked_child_before_cleanup(tmp_path: Path) -> None:
    """Exercise adapter shutdown in a fresh process with a child-held Windows file lock.

    The child opens a file without FILE_SHARE_DELETE, so Windows refuses removal while
    it is alive. The real probe cleanup must terminate the process tree first, retain a
    structured result, and remove only its own ``darkfac-quota-probe`` directory. The
    outer subprocess also makes interpreter shutdown observable on captured stderr.
    """
    sentinel = tmp_path / "external-sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    harness = r'''
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from core.usage import adapters
from core.usage.adapters import ClaudeCodeAccountAdapter, ProviderFamily, ProviderSpec

case_root = Path(sys.argv[1]).resolve()
tempfile.tempdir = str(case_root)
probe_cli = r"""
import ctypes
import json
import sys
import time
from ctypes import wintypes
from pathlib import Path

path = Path("restricted-open.txt")
path.write_text("probe", encoding="utf-8")
kernel = ctypes.WinDLL("kernel32", use_last_error=True)
kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel.CreateFileW.restype = wintypes.HANDLE
handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
assert handle != ctypes.c_void_p(-1).value, ctypes.get_last_error()
Path(sys.argv[1]).write_text("ready", encoding="utf-8")
print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {"five_hour": {"utilization": 0.42}}}}), flush=True)
while True:
    time.sleep(1)
"""

real_popen = subprocess.Popen
probe_processes = []

class ProbeProcess:
    """Real subprocess with a short communicate deadline to exercise timeout cleanup."""
    def __init__(self, command, **kwargs):
        self._cwd = Path(kwargs["cwd"])
        self.delete_blocked = False
        self._process = real_popen(
            [sys.executable, "-c", probe_cli, str(case_root / "darkfac-quota-probe-child-ready")],
            **kwargs,
        )
        probe_processes.append(self)
    @property
    def pid(self):
        return self._process.pid
    @property
    def returncode(self):
        return self._process.returncode
    def communicate(self, timeout=None):
        try:
            return self._process.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                (self._cwd / "restricted-open.txt").unlink()
            except PermissionError:
                self.delete_blocked = True
            else:
                raise AssertionError("Windows allowed deleting a file held without FILE_SHARE_DELETE")
            raise
    def poll(self):
        return self._process.poll()
    def wait(self, timeout=None):
        return self._process.wait(timeout=timeout)
    def kill(self):
        return self._process.kill()

adapters.subprocess = SimpleNamespace(
    Popen=ProbeProcess, run=subprocess.run, PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
    CREATE_NEW_PROCESS_GROUP=subprocess.CREATE_NEW_PROCESS_GROUP,
    TimeoutExpired=subprocess.TimeoutExpired, CompletedProcess=subprocess.CompletedProcess,
)
spec = ProviderSpec("anthropic", "Anthropic / Claude", ProviderFamily.FRONTIER, "https://claude.ai/settings/usage")
adapter = ClaudeCodeAccountAdapter(spec, case_root / "snapshots")
usage = adapter._probe_claude_headless("probe-cli")
cleanup = adapter.last_probe_cleanup
print(json.dumps({
    "quota_is_fail_closed": usage is None,
    "cleanup": None if cleanup is None else {
        "ok": cleanup.ok, "error_kind": cleanup.error_kind, "winerror": cleanup.winerror,
        "detail": cleanup.detail, "path": str(cleanup.path), "exists": cleanup.path.exists(),
    },
    "probe_ready": (case_root / "darkfac-quota-probe-child-ready").exists(),
    "delete_was_blocked": bool(probe_processes and probe_processes[0].delete_blocked),
}))
'''
    completed = subprocess.run(
        [sys.executable, "-c", harness, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    stderr = completed.stderr.lower()
    assert "exception ignored" not in stderr
    assert "weakref" not in stderr
    assert "temporarydirectory" not in stderr
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result["quota_is_fail_closed"] is True
    assert result["probe_ready"] is True
    assert result["delete_was_blocked"] is True
    assert result["cleanup"] is not None
    assert result["cleanup"]["ok"] is True, result["cleanup"]
    sandbox_path = Path(result["cleanup"]["path"])
    assert sandbox_path.name.startswith("darkfac-quota-probe-")
    assert sandbox_path.parent == tmp_path.resolve()
    assert result["cleanup"]["exists"] is False
    assert sentinel.read_text(encoding="utf-8") == "keep"
