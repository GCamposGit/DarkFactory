"""USR-207: external availability probe for the Hub /health endpoint.

Run one ``tick`` per minute from a node OUTSIDE the VPS (Notebook or Desktop scheduled task):

    python -m core.infra.availability_probe (cwd C:/dev/DarkFac)

It alerts the Telegram ops chat when /health keeps failing for more than ``ALERT_AFTER_SECONDS``
(default 120 s) and sends one recovery message when it comes back. State lives in a small JSON file
so each invocation is stateless. Clock, prober and sender are injectable for tests.
"""

from __future__ import annotations

import json
import logging
import argparse
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("availability_probe")

DEFAULT_URL = "https://darkhub.ggcampos.com/health?memory=true"
ALERT_AFTER_SECONDS = 120.0
REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILENAME = "hub_availability.json"


@dataclass
class ProbeState:
    first_failure_ts: Optional[float] = None
    alerted: bool = False
    last_tick_ts: Optional[float] = None
    oom_kill_count: Optional[int] = None
    high_usage_ticks: int = 0
    memory_alerted: bool = False

    def to_json(self) -> str:
        return json.dumps({
            "first_failure_ts": self.first_failure_ts, "alerted": self.alerted,
            "last_tick_ts": self.last_tick_ts, "oom_kill_count": self.oom_kill_count,
            "high_usage_ticks": self.high_usage_ticks, "memory_alerted": self.memory_alerted,
        })

    @classmethod
    def from_json(cls, raw: str) -> "ProbeState":
        try:
            data = json.loads(raw)
            ts = data.get("first_failure_ts")
            return cls(
                first_failure_ts=float(ts) if ts is not None else None,
                alerted=bool(data.get("alerted")),
                last_tick_ts=float(data["last_tick_ts"]) if data.get("last_tick_ts") is not None else None,
                oom_kill_count=int(data["oom_kill_count"]) if data.get("oom_kill_count") is not None else None,
                high_usage_ticks=max(0, int(data.get("high_usage_ticks", 0))),
                memory_alerted=bool(data.get("memory_alerted")),
            )
        except (ValueError, TypeError, AttributeError):
            return cls()


def http_probe(url: str = DEFAULT_URL, timeout: float = 5.0) -> bool:
    """True when /health answers 2xx within ``timeout``; never raises."""
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except Exception:
        return False


def http_health_probe(url: str = DEFAULT_URL, timeout: float = 5.0) -> dict | None:
    """Return the JSON health document, or None for any HTTP/parse failure."""
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if not 200 <= int(response.status) < 300:
                return None
            payload = json.loads(response.read().decode("utf-8"))
            return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def tick(
    state: ProbeState,
    *,
    probe: Callable[[], bool | dict | None],
    send: Callable[[str], bool],
    now: Callable[[], float],
    alert_after_seconds: float = ALERT_AFTER_SECONDS,
    url: str = DEFAULT_URL,
    memory_alert_percent: float = 85.0,
) -> ProbeState:
    """One observation. Mutates and returns ``state``; sender failures never raise."""
    t = now()
    observation = probe()
    healthy = bool(observation) if isinstance(observation, bool) else isinstance(observation, dict) and observation.get("status") == "ok"
    state.last_tick_ts = t
    if healthy:
        if state.alerted:
            _safe_send(send, f"[HUB] /health voltou a responder ({url}) apos indisponibilidade.")
        state.first_failure_ts = None
        state.alerted = False
    else:
        if state.first_failure_ts is None:
            state.first_failure_ts = t
        down_for = t - state.first_failure_ts
        if down_for >= alert_after_seconds and not state.alerted:
            if _safe_send(send, f"[HUB] /health indisponivel ha {int(down_for)}s ({url}). Verifique o deploy no Dokploy."):
                state.alerted = True

    if not isinstance(observation, dict):
        return state

    memory = observation.get("memory")
    if not isinstance(memory, dict) or not memory.get("available"):
        return state
    current = memory.get("current_bytes")
    limit = memory.get("limit_bytes")
    usage_high = isinstance(current, (int, float)) and isinstance(limit, (int, float)) and limit > 0 and current / limit * 100 >= memory_alert_percent
    state.high_usage_ticks = state.high_usage_ticks + 1 if usage_high else 0
    oom_count = memory.get("oom_kill_count")
    oom_count = int(oom_count) if isinstance(oom_count, (int, float)) else None
    oom_increased = oom_count is not None and state.oom_kill_count is not None and oom_count > state.oom_kill_count
    if oom_count is not None and state.oom_kill_count is None:
        state.oom_kill_count = oom_count
    elif oom_increased:
        count = state.oom_kill_count or 0
        for event_number in range(count + 1, oom_count + 1):
            if not _safe_send(send, f"[HUB] OOMKilled detectado: contador oom_kill={event_number} ({url}). Verifique memoria e reinicializacoes."):
                break  # keep this and later events pending for the next scheduled tick
            state.oom_kill_count = event_number
            state.memory_alerted = True
    elif oom_count is not None and state.oom_kill_count is not None and oom_count < state.oom_kill_count:
        # A reset indicates a replaced cgroup/container; begin counting its events from zero.
        state.oom_kill_count = oom_count

    if state.high_usage_ticks >= 2 and not state.memory_alerted:
        percent = current / limit * 100
        if _safe_send(send, f"[HUB] memoria alta: {percent:.1f}% do limite ({url}); acima de {memory_alert_percent:.0f}% por dois ticks."):
            state.memory_alerted = True
    if state.memory_alerted and not oom_increased and not usage_high:
        if _safe_send(send, f"[HUB] recuperacao: memoria do container voltou ao normal ({url})."):
            state.memory_alerted = False
    return state


def _safe_send(send: Callable[[str], bool], message: str) -> bool:
    try:
        return bool(send(message))
    except Exception as exc:  # noqa: BLE001
        logger.warning("ops alert failed: %s", exc.__class__.__name__)
        return False


def default_sender(message: str) -> bool:
    from core.line.auth_bootstrap import _notify_human

    return _notify_human(message)


def load_state(path: Path) -> ProbeState:
    try:
        return ProbeState.from_json(path.read_text(encoding="utf-8"))
    except OSError:
        return ProbeState()


def save_state(path: Path, state: ProbeState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.to_json(), encoding="utf-8")


def main(
    path: Optional[Path] = None,
    url: str = DEFAULT_URL,
    *,
    status: bool = False,
    now: Optional[float] = None,
    stale_after_seconds: float = 180.0,
) -> int:
    if path is None:
        from core.paths import state_root

        path = state_root() / STATE_FILENAME
    state = load_state(path)
    if status:
        age = float("inf") if state.last_tick_ts is None else max(0.0, (time.time() if now is None else now) - state.last_tick_ts)
        age_text = "desconhecida" if age == float("inf") else f"{age:.0f}s"
        print(f"Ultimo tick: idade {age_text}; limite de stale: {stale_after_seconds:.0f}s")
        return 1 if age > stale_after_seconds else 0
    state = tick(state, probe=lambda: http_health_probe(url), send=default_sender, now=time.time, url=url)
    save_state(path, state)
    return 0


if __name__ == "__main__":
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    parser = argparse.ArgumentParser(description="Sonda externa de disponibilidade e memoria do DarkHub")
    parser.add_argument("--status", action="store_true", help="mostra idade do ultimo tick e verifica se esta stale")
    parser.add_argument("--state-path", type=Path, help="caminho do arquivo de estado")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--stale-after", type=float, default=180.0, help="segundos ate considerar o agendamento parado")
    args = parser.parse_args()
    sys.exit(main(args.state_path, args.url, status=args.status, stale_after_seconds=args.stale_after))
