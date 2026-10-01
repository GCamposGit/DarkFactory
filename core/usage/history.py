"""Sanitized, local evidence trail for subscription quota probes."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.usage.models import AccountConnectionStatus, ProviderAccountUsage

logger = logging.getLogger(__name__)

_ALLOWED_RAW = {
    "xai": {"usagePercent", "currentPeriodStart", "nextResetTimestampUtc", "hasAvailableUsage", "hasNonZeroIncludedLimit", "includedUsageSuperGrokPlan", "grokPlanLabel", "cursorPlanName", "billingBrand"},
    "openai": {"primaryUsedPercent", "primaryWindowDurationMins", "primaryResetsAt",
               "secondaryUsedPercent", "secondaryWindowDurationMins", "secondaryResetsAt",
               "usedPercent", "windowDurationMins", "resetsAt"},
    "anthropic": {"anthropic-ratelimit-unified-7d-utilization", "anthropic-ratelimit-unified-7d-reset", "anthropic-ratelimit-unified-5h-utilization", "anthropic-ratelimit-unified-5h-reset"},
    "google": {"remainingFraction", "resetTime", "window", "bucketId"},
}


def sanitize_raw(provider: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only non-identifying quota fields; never persist unknown response keys."""
    if not isinstance(raw, dict):
        return {}
    allowed = _ALLOWED_RAW.get(provider, set())
    clean: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in allowed or type(value) not in (str, int, float, bool, type(None)):
            continue
        if isinstance(value, str) and (len(value) > 128 or "@" in value
                                      or re.search(r"(?i)(bearer|token|secret|eyJ[A-Za-z0-9_-]{20})", value)):
            clean[key] = "[redacted]"
        else:
            clean[key] = value
    return clean


def history_path(snapshot_dir: Path, provider: str) -> Path:
    return Path(snapshot_dir).parent / "history" / f"{provider}.jsonl"


def confirmation_path(snapshot_dir: Path, provider: str) -> Path:
    return history_path(snapshot_dir, provider).with_suffix(".confirmation.json")


def _parse(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def read_history(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        return [row for line in path.read_text(encoding="utf-8").splitlines()
                if (row := json.loads(line)) and isinstance(row, dict)]
    except (OSError, ValueError) as exc:
        logger.warning("Quota history unreadable at %s: %s", path, exc)
        return []


def record_probe(usage: ProviderAccountUsage, snapshot_dir: Path) -> ProviderAccountUsage:
    """Check a real reading against earlier readings, then append sanitized evidence."""
    if usage.provider_id not in _ALLOWED_RAW:
        return usage
    path = history_path(snapshot_dir, usage.provider_id)
    rows = read_history(path)
    flags: list[str] = []
    try:
        if path.is_file() and path.stat().st_size > 0 and not rows:
            flags.append("quota history unreadable")
    except OSError:
        flags.append("quota history unavailable")
    now = _parse(usage.checked_at) or datetime.now(timezone.utc)
    for window in usage.windows:
        if window.used_percent is None:
            continue
        previous = next((entry for entry in reversed(rows)
                         if entry.get("quota_id") == window.quota_id
                         and entry.get("used_percent") is not None
                         and not entry.get("flags")), None)
        period_key = usage.period_start or window.resets_at
        if previous and period_key and previous.get("period_start") == period_key:
            prior = float(previous["used_percent"])
            if window.used_percent < prior - 0.1:
                flags.append(f"{window.quota_id}: used_percent decreased in same period")
            if window.used_percent - prior > 50:
                flags.append(f"{window.quota_id}: used_percent jumped over 50 points")
        start = _parse(usage.period_start)
        if start is None and window.resets_at and window.window_duration_minutes:
            reset = _parse(window.resets_at)
            start = reset - timedelta(minutes=window.window_duration_minutes) if reset else None
        if (start and window.window_duration_minutes == 10080
                and timedelta(0) <= now - start <= timedelta(hours=1)
                and window.remaining_percent is not None and window.remaining_percent < 20):
            flags.append(f"{window.quota_id}: low remaining immediately after reset")
    measured_windows = usage.windows
    confirmation_file = confirmation_path(snapshot_dir, usage.provider_id)
    if flags and confirmation_file.is_file():
        try:
            confirmation = json.loads(confirmation_file.read_text(encoding="utf-8"))
            confirmed_window = next((w for w in measured_windows if w.quota_id == confirmation.get("quota_id")), None)
            if (confirmation.get("period_start") == usage.period_start
                    and confirmed_window is not None
                    and confirmed_window.used_percent is not None
                    and abs(confirmed_window.used_percent - float(confirmation["used_percent"])) <= 0.1):
                flags = []
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Invalid quota confirmation for %s", usage.provider_id)
    if flags:
        logger.warning("Suspicious quota reading for %s: %s", usage.provider_id, "; ".join(flags))
        usage = usage.model_copy(update={"status": AccountConnectionStatus.DEGRADED,
                                         "plausibility_flags": flags,
                                         "message": "SUSPEITA: " + "; ".join(flags),
                                         "windows": [w.model_copy(update={"used_percent": None, "remaining_percent": None}) for w in usage.windows]})
    path.parent.mkdir(parents=True, exist_ok=True)
    # Rotate whole files: previous lines are never rewritten. Archive once the oldest
    # entry passes the retention horizon, then start a new append-only current file.
    if rows and (oldest := _parse(rows[0].get("checked_at"))) and now - oldest > timedelta(days=30):
        archive = path.with_name(f"{path.stem}.{now.strftime('%Y%m%dT%H%M%S%f')}.jsonl")
        path.replace(archive)
    with path.open("a", encoding="utf-8") as stream:
        for window in measured_windows or [None]:
            stream.write(json.dumps({
                "checked_at": usage.checked_at, "adapter": usage.adapter,
                "raw_fields": sanitize_raw(usage.provider_id, usage.raw_fields),
                "period_start": usage.period_start or (window.resets_at if window else None),
                "quota_id": window.quota_id if window else None,
                "used_percent": window.used_percent if window else None,
                "remaining_percent": window.remaining_percent if window else None,
                "resets_at": window.resets_at if window else None,
                "flags": flags,
            }, ensure_ascii=False) + "\n")
    return usage
