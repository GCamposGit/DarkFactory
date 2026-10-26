"""Capture Claude Code statusline quota windows for the DarkFac usage adapter."""

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.paths import state_root  # noqa: E402


_WINDOWS = (
    ("five_hour", "claude:5h", "Janela Móvel (5h)", 300),
    ("seven_day", "claude:weekly", "Limite Semanal (1 semana)", 10080),
)


def _window(raw: Any, quota_id: str, label: str, duration: int) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    used_raw = raw.get("used_percentage")
    if isinstance(used_raw, bool):
        return None
    try:
        used = float(used_raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(used) or not 0 <= used <= 100:
        return None
    reset = raw.get("resets_at")
    if isinstance(reset, (int, float)) and not isinstance(reset, bool):
        try:
            reset = datetime.fromtimestamp(reset, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            reset = None
    elif isinstance(reset, str) and reset.strip():
        reset = reset.strip()
    else:
        reset = None
    return {
        "quota_id": quota_id,
        "label": label,
        "used_percent": round(used, 2),
        "remaining_percent": round(100.0 - used, 2),
        "window_duration_minutes": duration,
        "resets_at": reset,
        "metric": "subscription",
    }


def snapshot_from_payload(payload: Any, checked_at: str | None = None) -> dict[str, Any] | None:
    """Return a privacy-filtered snapshot, or None when no quota window is valid."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rate_limits"), dict):
        return None
    limits = payload["rate_limits"]
    windows = [
        window
        for key, quota_id, label, duration in _WINDOWS
        if (window := _window(limits.get(key), quota_id, label, duration)) is not None
    ]
    if not windows:
        return None
    return {
        "provider_id": "anthropic",
        "status": "connected",
        "adapter": "claude_statusline",
        "plan": "Claude Pro",
        "quota_supported": True,
        "windows": windows,
        "message": "Cota lida do statusline autenticado do Claude Code.",
        "checked_at": checked_at or datetime.now(timezone.utc).isoformat(),
    }


def capture_statusline(payload_text: str, usage_dir: Path | None = None) -> bool:
    """Atomically save a valid statusline payload without retaining session data."""
    try:
        payload = json.loads(payload_text)
    except (TypeError, json.JSONDecodeError):
        return False
    snapshot = snapshot_from_payload(payload)
    if snapshot is None:
        return False

    target_dir = Path(usage_dir) if usage_dir is not None else state_root() / "usage" / "providers"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "anthropic.json"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target_dir, prefix=".anthropic.", suffix=".tmp", delete=False
        ) as temp_file:
            temp_name = temp_file.name
            json.dump(snapshot, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, target)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)
    return True


def main() -> int:
    try:
        saved = capture_statusline(sys.stdin.read())
    except OSError:
        saved = False
    # Statusline stdout is rendered by Claude Code; keep it useful and free of payload data.
    sys.stdout.write("Claude quota synced" if saved else "Claude")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
