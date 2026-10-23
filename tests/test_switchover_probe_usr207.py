from __future__ import annotations

import json
import threading
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.infra.switchover_probe import _http_get, probe_until_stopped


def test_probe_writes_one_second_http_and_sha_evidence(tmp_path: Path) -> None:
    stop = threading.Event()
    responses = iter([(200, {"git_sha": "old"}), (200, {"git_sha": "a" * 40})])
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    calls: list[str] = []

    def http_get(url: str) -> tuple[int, dict[str, Any]]:
        calls.append(url)
        response = next(responses)
        if len(calls) == 2:
            stop.set()
        return response

    evidence_path = tmp_path / "evidence.json"
    result = probe_until_stopped(
        "a" * 40,
        stop_event=stop,
        evidence_path=evidence_path,
        http_get=http_get,
        clock=lambda: next(ticks),
        wall_clock=lambda: datetime(2026, 10, 23, tzinfo=UTC),
        wait=lambda _event, _seconds: False,
    )

    assert result.passed
    assert len(result.samples) == 2
    assert all(sample.status_code == 200 for sample in result.samples)
    assert json.loads(evidence_path.read_text(encoding="utf-8"))["converged"] is True
    assert calls == ["https://darkhub.ggcampos.com/health"] * 2


def test_probe_fails_evidence_for_any_non_200_or_missing_sha(tmp_path: Path) -> None:
    stop = threading.Event()
    count = 0

    def http_get(_url: str) -> tuple[int, dict[str, Any]]:
        nonlocal count
        count += 1
        stop.set()
        return 503, {"git_sha": "old"}

    result = probe_until_stopped(
        "a" * 40,
        stop_event=stop,
        evidence_path=tmp_path / "evidence.json",
        http_get=http_get,
    )
    assert count == 1
    assert not result.passed
    assert not result.all_http_200
    assert not result.converged


def test_health_http_error_keeps_the_status_code(monkeypatch) -> None:
    error = urllib.error.HTTPError("https://darkhub.example/health", 503, "unavailable", {}, None)

    def fail(_request, timeout: float):
        raise error

    monkeypatch.setattr("urllib.request.urlopen", fail)

    status, payload = _http_get("https://darkhub.example/health")

    assert status == 503
    assert payload == {}
