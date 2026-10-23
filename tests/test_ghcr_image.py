"""Shared GHCR manifest polling behavior."""

from __future__ import annotations

import pytest

from core.orchestrator.ghcr_image import GhcrTagUnavailableError, wait_for_ghcr_tag


SHA = "a" * 40


def test_wait_continues_after_probe_exception_then_fails_without_build_fallback() -> None:
    now = [0.0]
    probes = [0]

    def probe(repository: str, tag: str) -> int:
        probes[0] += 1
        raise OSError("network unavailable")

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(GhcrTagUnavailableError) as caught:
        wait_for_ghcr_tag(
            SHA, probe=probe, timeout=2, interval=1, sleep_fn=sleep, clock_fn=lambda: now[0]
        )
    assert probes[0] == 3
    assert f"ghcr.io/gcamposgit/darkhub:{SHA}" in str(caught.value)
    assert "no fallback to build" in str(caught.value)
