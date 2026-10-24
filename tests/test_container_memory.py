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
