"""Tests for memory-safe worker capacity budgeting and WMI startup mitigation (USR-95)."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from tests import _worker_capacity

pytestmark = [pytest.mark.offline]

BYTES_PER_GIB = 1024 * 1024 * 1024


@pytest.fixture(autouse=True)
def _clean_worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DARKFAC_MAX_WORKERS", raising=False)
    monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS", raising=False)


def test_calculate_safe_worker_cap_notebook_incident() -> None:
    """The 2026-09-30 incident: Notebook with 6.3 GiB available RAM and 20 CPUs.

    Without memory-aware budgeting, xdist spawned 4+ workers, exhausting commit
    and triggering 0x8007000e. With budgeting, it must be capped at 2 workers.
    """
    available = int(6.3 * BYTES_PER_GIB)
    cpus = 20
    cap = _worker_capacity.calculate_safe_worker_cap(
        available_bytes=available,
        cpu_count=cpus,
    )
    assert cap == 2, f"Expected 2 workers for 6.3 GiB available, got {cap}"


def test_calculate_safe_worker_cap_moderate_memory() -> None:
    """With 9.2 GiB available RAM, system should budget 3 workers."""
    available = int(9.2 * BYTES_PER_GIB)
    cpus = 20
    cap = _worker_capacity.calculate_safe_worker_cap(
        available_bytes=available,
        cpu_count=cpus,
    )
    assert cap == 3, f"Expected 3 workers for 9.2 GiB available, got {cap}"


def test_calculate_safe_worker_cap_high_memory_capped_by_hard_limit() -> None:
    """With 64 GiB available RAM and 32 CPUs, default hard cap (8) takes effect."""
    available = int(64.0 * BYTES_PER_GIB)
    cpus = 32
    cap = _worker_capacity.calculate_safe_worker_cap(
        available_bytes=available,
        cpu_count=cpus,
    )
    assert cap == 8, f"Expected hard cap 8, got {cap}"


def test_calculate_safe_worker_cap_bounded_by_cpu() -> None:
    """With 32 GiB RAM but only 4 CPUs, bounded by CPU count (4)."""
    available = int(32.0 * BYTES_PER_GIB)
    cpus = 4
    cap = _worker_capacity.calculate_safe_worker_cap(
        available_bytes=available,
        cpu_count=cpus,
    )
    assert cap == 4


def test_calculate_safe_worker_cap_low_memory_returns_minimum_one() -> None:
    """With less than 2 GiB available, must return at least 1 worker."""
    available = int(1.0 * BYTES_PER_GIB)
    cpus = 8
    cap = _worker_capacity.calculate_safe_worker_cap(
        available_bytes=available,
        cpu_count=cpus,
    )
    assert cap == 1


def test_env_var_override_darkfac_max_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit DARKFAC_MAX_WORKERS overrides dynamic calculation."""
    monkeypatch.setenv("DARKFAC_MAX_WORKERS", "5")
    cap = _worker_capacity.calculate_safe_worker_cap(
        available_bytes=int(6.3 * BYTES_PER_GIB),
        cpu_count=20,
    )
    assert cap == 5


def test_env_var_override_pytest_xdist_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """PYTEST_XDIST_AUTO_NUM_WORKERS overrides dynamic calculation."""
    monkeypatch.delenv("DARKFAC_MAX_WORKERS", raising=False)
    monkeypatch.setenv("PYTEST_XDIST_AUTO_NUM_WORKERS", "4")
    cap = _worker_capacity.calculate_safe_worker_cap(
        available_bytes=int(6.3 * BYTES_PER_GIB),
        cpu_count=20,
    )
    assert cap == 4


def test_mitigate_windows_wmi_startup() -> None:
    """WMI mitigation ensures platform queries succeed without invoking unhandled WMI."""
    _worker_capacity.mitigate_windows_wmi_startup()
    import platform

    system = platform.system()
    assert system in {"Windows", "Linux", "Darwin"}
    uname = platform.uname()
    assert uname.system is not None
    if sys.platform == "win32":
        release, version, csd, ptype = platform.win32_ver()
        assert release != "" or version != ""


def test_structural_worker_cap_diagnostic() -> None:
    """Structural diagnostic gate: validates that the effective local worker cap
    does not exceed safe available memory limits on the host running the suite.
    """
    avail = _worker_capacity.get_available_memory_bytes()
    cpus = _worker_capacity.get_physical_cpu_count()
    safe_cap = _worker_capacity.calculate_safe_worker_cap(avail, cpus)

    # Verification: (safe_cap * 2.0 GiB) must not exceed (avail + 1.0 GiB margin)
    # when safe_cap > 1
    if safe_cap > 1:
        required_mem = (safe_cap * _worker_capacity.DEFAULT_MIN_WORKER_MEMORY_BYTES)
        assert required_mem <= avail + _worker_capacity.DEFAULT_SYSTEM_RESERVE_BYTES, (
            f"Configured cap ({safe_cap}) requires ~{required_mem / BYTES_PER_GIB:.1f} GiB "
            f"which exceeds safe memory headroom (available: {avail / BYTES_PER_GIB:.1f} GiB)"
        )


def test_explain_worker_budget() -> None:
    """Verify human-readable explanation of allowed workers and memory budgeting (USR-143)."""
    # 1. With memory limiting
    avail = int(6.3 * BYTES_PER_GIB)
    explanation = _worker_capacity.explain_worker_budget(available_bytes=avail, cpu_count=20)
    assert "2 xdist workers" in explanation
    assert "available RAM: 6.3 GiB" in explanation

    # 2. With CPU limiting
    avail = int(32.0 * BYTES_PER_GIB)
    explanation = _worker_capacity.explain_worker_budget(available_bytes=avail, cpu_count=4)
    assert "4 xdist workers" in explanation
    assert "bounded by 4 CPUs" in explanation

    # 3. With override DARKFAC_MAX_WORKERS
    with patch.dict(os.environ, {"DARKFAC_MAX_WORKERS": "5"}):
        explanation = _worker_capacity.explain_worker_budget(available_bytes=avail, cpu_count=20)
        assert "5 workers" in explanation
        assert "override DARKFAC_MAX_WORKERS=5" in explanation

