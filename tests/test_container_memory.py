from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.infra.container_memory import read_container_memory
from hub.backend.main import app


def _write(root: Path, files: dict[str, str]) -> None:
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")


def test_reads_cgroup_v2_memory_and_oom_kills(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "memory.current": str(100 * 1024 * 1024),
            "memory.peak": str(200 * 1024 * 1024),
            "memory.max": str(512 * 1024 * 1024),
            "memory.events": "low 0\nhigh 0\nmax 2\noom 1\noom_kill 3\n",
        },
    )

    result = read_container_memory(tmp_path)

    assert result.used_mb == 100
    assert result.peak_mb == 200
    assert result.limit_mb == 512
    assert result.percent_of_limit == pytest.approx(100 * 100 / 512)
    assert result.oom_kills == 3


def test_reads_cgroup_v1_memory_and_fail_count(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "memory.usage_in_bytes": str(64 * 1024 * 1024),
            "memory.max_usage_in_bytes": str(128 * 1024 * 1024),
            "memory.limit_in_bytes": str(256 * 1024 * 1024),
            "memory.failcnt": "4",
        },
    )

    result = read_container_memory(tmp_path)

    assert result.used_mb == 64
    assert result.peak_mb == 128
    assert result.limit_mb == 256
    assert result.percent_of_limit == 25
    assert result.oom_kills == 4


def test_missing_corrupt_unreadable_and_unlimited_cgroups_are_unknown(
    tmp_path: Path, monkeypatch
) -> None:
    assert read_container_memory(tmp_path).used_mb is None

    _write(
        tmp_path,
        {
            "memory.current": "broken",
            "memory.peak": "2",
            "memory.max": "3",
            "memory.events": "oom_kill nope",
        },
    )
    assert read_container_memory(tmp_path).limit_mb is None

    _write(
        tmp_path,
        {
            "memory.current": "1",
            "memory.peak": "2",
            "memory.max": "max",
            "memory.events": "oom_kill 0",
        },
    )
    assert read_container_memory(tmp_path).used_mb is None

    v1 = tmp_path / "v1"
    _write(
        v1,
        {
            "memory.usage_in_bytes": "1",
            "memory.max_usage_in_bytes": "2",
            "memory.limit_in_bytes": str(2**63 - 4096),
            "memory.failcnt": "0",
        },
    )
    assert read_container_memory(v1).limit_mb is None

    def unreadable(_self: Path, *args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", unreadable)
    assert read_container_memory(tmp_path).used_mb is None


def test_health_memory_is_opt_in_and_read_failure_does_not_break_endpoint(monkeypatch) -> None:
    import core.infra.container_memory as memory

    client = TestClient(app)
    monkeypatch.setattr(
        memory,
        "read_container_memory",
        lambda: (_ for _ in ()).throw(RuntimeError("cgroup unavailable")),
    )

    plain = client.get("/health")
    assert plain.status_code == 200
    assert set(plain.json()) == {"status", "git_sha"}

    with_memory = client.get("/health?memory=1")
    assert with_memory.status_code == 200
    assert "memory" in with_memory.json()
    assert with_memory.json()["memory"]["used_mb"] is None


def test_sampler_alerts_at_threshold_once_per_window_and_again_after_window() -> None:
    from core.infra.container_memory import ContainerMemory, ContainerMemorySampler

    now = [100.0]
    sent: list[str] = []
    reading = [ContainerMemory(used_mb=410, peak_mb=410, limit_mb=512, percent_of_limit=80.1, oom_kills=0)]
    sampler = ContainerMemorySampler(
        reader=lambda: reading[0], notifier=sent.append, clock=lambda: now[0], dedupe_window_seconds=60
    )

    sampler.sample_once()
    sampler.sample_once()
    assert len(sent) == 1
    now[0] += 61
    sampler.sample_once()
    assert len(sent) == 2
    assert sampler.peak_percent == pytest.approx(410 * 100 / 512)


def test_sampler_alerts_on_each_oom_counter_increase() -> None:
    from core.infra.container_memory import ContainerMemory, ContainerMemorySampler

    counts = [2, 2, 3, 4]
    sent: list[str] = []
    sampler = ContainerMemorySampler(
        reader=lambda: ContainerMemory(used_mb=10, peak_mb=10, limit_mb=512, percent_of_limit=2, oom_kills=counts.pop(0)),
        notifier=sent.append,
        clock=lambda: 1.0,
        dedupe_window_seconds=3600,
    )

    for _ in range(4):
        sampler.sample_once()

    assert len(sent) == 2
    assert all("OOM" in message for message in sent)


def test_sampler_ignores_unknown_limit_and_swallows_notifier_failure(caplog) -> None:
    from core.infra.container_memory import ContainerMemory, ContainerMemorySampler, start_background_memory_sampler

    unknown = ContainerMemory(used_mb=10, peak_mb=10, oom_kills=0)
    sampler = ContainerMemorySampler(reader=lambda: unknown, notifier=lambda _message: pytest.fail("no alert"))
    assert sampler.sample_once() is False
    assert start_background_memory_sampler(reader=lambda: unknown) is None

    known = ContainerMemory(used_mb=450, peak_mb=450, limit_mb=512, percent_of_limit=88, oom_kills=0)
    failing = ContainerMemorySampler(reader=lambda: known, notifier=lambda _message: (_ for _ in ()).throw(RuntimeError("offline")))
    assert failing.sample_once() is True
    assert "notifier" in caplog.text.lower()


def test_load_report_is_pure_and_reports_p95_peak_and_errors() -> None:
    from scripts.hub_memory_load import build_report

    report = build_report(
        latencies_ms=[10, 20, 30, 40],
        errors=2,
        memory={"used_mb": 400, "peak_mb": 450, "limit_mb": 512, "percent_of_limit": 87.89, "oom_kills": 3},
        started_at="start",
        finished_at="finish",
    )

    assert report["latency_p95_ms"] == 40
    assert report["peak_percent"] == pytest.approx(87.89)
    assert report["oom_kills"] == 3
    assert report["errors"] == 2


def test_load_script_arguments_parse_without_network() -> None:
    from scripts.hub_memory_load import parse_args

    args = parse_args(["--base-url", "https://hub.example", "--concurrency", "20", "--seconds", "300", "--out", "report.json"])

    assert args.base_url == "https://hub.example"
    assert args.concurrency == 20
    assert args.seconds == 300
    assert str(args.out) == "report.json"
