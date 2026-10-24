"""One-second deployment availability evidence (USR-207 T2)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from core.infra.deploy_probe import DeployProbe, ProbeSample, evaluate_samples


def test_probe_samples_at_one_second_with_injected_clock_and_sleep(tmp_path: Path) -> None:
    times = iter([10.0, 11.0, 12.0])
    sleeps: list[float] = []
    responses = iter([(200, "a" * 40), (200, "b" * 40), (200, "b" * 40)])
    completed = threading.Event()
    holder: dict[str, DeployProbe] = {}

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            holder["probe"]._stop.set()
            completed.set()

    probe = DeployProbe(
        output_dir=tmp_path,
        target_sha="b" * 40,
        http_fn=lambda _url, _timeout: next(responses),
        clock_fn=lambda: next(times),
        sleep_fn=sleep,
    )
    holder["probe"] = probe
    probe.start()
    assert completed.wait(2)
    probe.stop()
    result = probe.finish()
    assert sleeps == [1.0, 1.0]
    assert [sample.timestamp for sample in result.samples] == [10.0, 11.0]
    assert result.sample_count == 2 and result.non200 == 0


def test_timeout_and_connection_errors_are_recorded_as_non_200() -> None:
    samples = [
        ProbeSample(1.0, None, None, "timeout"),
        ProbeSample(2.0, None, None, "connection_error"),
    ]
    verdict = evaluate_samples(samples, target_sha="b" * 40, deployments_for_sha=1)
    assert verdict.non200 == 2
    assert not verdict.passed


def test_verdict_requires_zero_non200_and_old_and_new_shas() -> None:
    good = [ProbeSample(1, 200, "a" * 40), ProbeSample(2, 200, "b" * 40)]
    assert evaluate_samples(good, target_sha="b" * 40, deployments_for_sha=1).passed
    assert not evaluate_samples(good + [ProbeSample(3, 503, None)], target_sha="b" * 40,
                                deployments_for_sha=1).passed
    assert not evaluate_samples([good[-1]], target_sha="b" * 40, deployments_for_sha=1).passed


def test_probe_writes_jsonl_and_summary_and_flags_duplicate_deployments(tmp_path: Path) -> None:
    probe = DeployProbe(
        output_dir=tmp_path,
        target_sha="b" * 40,
        http_fn=lambda _url, _timeout: (200, "a" * 40),
        clock_fn=lambda: 123.0,
        run_id="run-1",
    )
    probe.sample_once()
    probe.sample_once()
    result = probe.finish(deployments_for_sha=2)
    assert result.jsonl_path == tmp_path / "run-1.jsonl"
    assert result.summary_path == tmp_path / "run-1.json"
    rows = [json.loads(line) for line in result.jsonl_path.read_text(encoding="utf-8").splitlines()]
    assert rows[0] == {"timestamp": 123.0, "status": 200, "git_sha": "a" * 40, "error": None}
    summary = json.loads(result.summary_path.read_text(encoding="utf-8"))
    assert summary["deployments_for_sha"] == 2
    assert summary["duplicate_deployment"] is True
    assert summary["sample_count"] == 2


def test_default_location_is_under_state_root_reports_deploy(monkeypatch, tmp_path: Path) -> None:
    from core.infra import deploy_probe

    monkeypatch.setattr(deploy_probe, "state_root", lambda: tmp_path)
    assert deploy_probe.default_output_dir() == tmp_path / "reports" / "deploy"


def test_sample_never_raises_on_probe_or_persistence_errors(tmp_path: Path) -> None:
    def broken(_url: str, _timeout: float) -> tuple[int, str | None]:
        raise TimeoutError("late")

    probe = DeployProbe(output_dir=tmp_path, target_sha="b" * 40, http_fn=broken)
    probe.sample_once()
    result = probe.finish()
    assert result.non200 == 1
