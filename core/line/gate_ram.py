"""Espera de RAM antes do portao oficial LOCAL (USR-262).

O harness escolhe os workers xdist pela RAM livre no inicio da execucao. Com 1 worker a suite passa
de 3000 s (4 portoes estouraram em 14/10 com 0 testes contabilizados); com 2 passa em ~1814 s. Este
modulo mede quantos workers caberiam (reutilizando ``tests/_worker_capacity``, sem edita-lo) e espera
ate caberem 2+, ou desiste com causa explicita sem rodar o portao.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

ENV_RAM_WAIT_S = "DARKFAC_GATE_RAM_WAIT_S"
DEFAULT_RAM_WAIT_S = 1800.0
MIN_GATE_WORKERS = 2
POLL_INTERVAL_S = 60.0
CAUSE_INSUFFICIENT_RAM = "gate_insufficient_ram"


@dataclass(frozen=True)
class RamReading:
    available_gib: float
    workers: int


@dataclass(frozen=True)
class RamWaitResult:
    ok: bool
    reading: RamReading
    waited_s: float


def ram_wait_limit_s() -> float:
    raw = os.environ.get(ENV_RAM_WAIT_S, "").strip()
    if not raw:
        return DEFAULT_RAM_WAIT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_RAM_WAIT_S
    return value if value >= 0 else DEFAULT_RAM_WAIT_S


def measure_workers() -> RamReading:
    """Workers que o portao teria agora (mesma conta de ``tests/_worker_capacity``, sem o piso de 1)."""
    from tests import _worker_capacity as wc

    available = wc.get_available_memory_bytes()
    gib = available / wc.BYTES_PER_GIB
    for name in ("DARKFAC_MAX_WORKERS", "PYTEST_XDIST_AUTO_NUM_WORKERS"):
        raw = os.environ.get(name)
        if raw:
            try:
                if int(raw) > 0:
                    return RamReading(gib, int(raw))
            except ValueError:
                pass
    by_memory = max(0, available - wc.DEFAULT_SYSTEM_RESERVE_BYTES) // wc.DEFAULT_MIN_WORKER_MEMORY_BYTES
    return RamReading(gib, int(min(by_memory, wc.get_physical_cpu_count(), wc.DEFAULT_HARD_CAP_WORKERS)))


def remote_worker_available() -> bool:
    """True when the official gate would run on a reachable, idle remote worker instead of locally."""
    try:
        from core.harness import remote_dispatch as rd

        if rd._effective_mode(remote_required=False) == "off" or rd._unconditional_disable_reason():
            return False
        for url in rd.worker_urls():
            health = rd.probe_health(url)
            if health is None or rd.is_self_dispatch(url, health) or health.get("busy"):
                continue
            return True
    except Exception:
        return False
    return False


def wait_for_gate_ram(
    *,
    measure: Callable[[], RamReading] = measure_workers,
    sleep: Callable[[float], Any] = time.sleep,
    limit_s: float | None = None,
    poll_s: float = POLL_INTERVAL_S,
    min_workers: int = MIN_GATE_WORKERS,
    announce: Callable[[str], Any] | None = None,
) -> RamWaitResult:
    """Block until ``min_workers`` fit in RAM or ``limit_s`` elapses (time counted by the sleeps)."""
    limit = ram_wait_limit_s() if limit_s is None else limit_s
    say = announce or (lambda msg: print(msg, file=sys.stderr))
    waited = 0.0
    reading = measure()
    while reading.workers < min_workers:
        if waited >= limit:
            return RamWaitResult(False, reading, waited)
        say(
            f"[RAM] {reading.available_gib:.1f} GiB livres, cabem {reading.workers} workers; "
            f"aguardando {min_workers}+ (limite {limit:.0f} s)"
        )
        step = min(poll_s, limit - waited)
        sleep(step)
        waited += step
        reading = measure()
    return RamWaitResult(True, reading, waited)
