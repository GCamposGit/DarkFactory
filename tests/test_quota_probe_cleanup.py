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

import pytest

from core.common import tempdirs
from core.usage.adapters import ClaudeCodeAccountAdapter, ProviderFamily, ProviderSpec


def _deny_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    def _denied(path):  # noqa: ANN001 - matches tempdirs.robust_rmtree's signature
        exc = PermissionError("[WinError 5] Access is denied")
        exc.winerror = 5  # type: ignore[attr-defined]
        raise exc

    monkeypatch.setattr(tempdirs, "robust_rmtree", _denied)


class _NoWindowResult:
    returncode = 0
    stdout = ""  # no rate_limit_event line -> no readable utilization
    stderr = ""


def test_probe_claude_headless_stays_fail_closed_when_sandbox_cleanup_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _deny_cleanup(monkeypatch)
    monkeypatch.setattr("core.usage.adapters.subprocess.run", lambda *a, **k: _NoWindowResult())

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

    monkeypatch.setattr("core.usage.adapters.subprocess.run", lambda *a, **k: _WithWindowResult())

    spec = ProviderSpec("anthropic", "Anthropic / Claude", ProviderFamily.FRONTIER, "https://claude.ai/settings/usage")
    adapter = ClaudeCodeAccountAdapter(spec, tmp_path)

    result = adapter._probe_claude_headless("claude")

    assert result is not None
    assert result.quota_supported is True
    assert any(w.used_percent == 42.0 for w in result.windows)
