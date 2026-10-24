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
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("availability_probe")

DEFAULT_URL = "https://darkhub.ggcampos.com/health"
ALERT_AFTER_SECONDS = 120.0
REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILENAME = "hub_availability.json"


@dataclass
class ProbeState:
    first_failure_ts: Optional[float] = None
    alerted: bool = False

    def to_json(self) -> str:
        return json.dumps({"first_failure_ts": self.first_failure_ts, "alerted": self.alerted})

    @classmethod
    def from_json(cls, raw: str) -> "ProbeState":
        try:
            data = json.loads(raw)
            ts = data.get("first_failure_ts")
            return cls(first_failure_ts=float(ts) if ts is not None else None, alerted=bool(data.get("alerted")))
        except (ValueError, TypeError, AttributeError):
            return cls()


def http_probe_response(url: str = DEFAULT_URL, timeout: float = 5.0) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Return ``(status, git_sha, error)`` for /health; never raises."""
    try:
        request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            try:
                payload = json.loads(response.read().decode("utf-8"))
                sha = payload.get("git_sha") if isinstance(payload, dict) else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                sha = None
            return status, str(sha) if sha else None, None
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
            sha = payload.get("git_sha") if isinstance(payload, dict) else None
        except Exception:
            sha = None
        return int(exc.code), str(sha) if sha else None, None
    except Exception as exc:
        detail = getattr(exc, "reason", None)
        timed_out = isinstance(exc, TimeoutError) or isinstance(detail, TimeoutError)
        return None, None, "timeout" if timed_out else "connection_error"


def http_probe(url: str = DEFAULT_URL, timeout: float = 5.0) -> bool:
    """True when /health answers 2xx within ``timeout``; never raises."""
    status, _sha, _error = http_probe_response(url, timeout)
    return status is not None and 200 <= status < 300


def tick(
    state: ProbeState,
    *,
    probe: Callable[[], bool],
    send: Callable[[str], bool],
    now: Callable[[], float],
    alert_after_seconds: float = ALERT_AFTER_SECONDS,
    url: str = DEFAULT_URL,
) -> ProbeState:
    """One observation. Mutates and returns ``state``; sender failures never raise."""
    t = now()
    if probe():
        if state.alerted:
            _safe_send(send, f"[HUB] /health voltou a responder ({url}) apos indisponibilidade.")
        state.first_failure_ts = None
        state.alerted = False
        return state
    if state.first_failure_ts is None:
        state.first_failure_ts = t
    down_for = t - state.first_failure_ts
    if down_for >= alert_after_seconds and not state.alerted:
        if _safe_send(send, f"[HUB] /health indisponivel ha {int(down_for)}s ({url}). Verifique o deploy no Dokploy."):
            state.alerted = True
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


def main(path: Optional[Path] = None, url: str = DEFAULT_URL) -> int:
    if path is None:
        from core.paths import state_root

        path = state_root() / STATE_FILENAME
    state = load_state(path)
    state = tick(state, probe=lambda: http_probe(url), send=default_sender, now=time.time, url=url)
    save_state(path, state)
    return 0


if __name__ == "__main__":
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    sys.exit(main())
