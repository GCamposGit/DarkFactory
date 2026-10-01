#!/usr/bin/env python3
"""Inspect sanitized quota evidence and compare it with owner dashboard readings."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.router.token_budget import _quota_headroom
from core.usage.adapters import build_default_adapters
from core.usage.history import confirmation_path, history_path, read_history, sanitize_raw


def audit(provider: str | None = None, expects: dict[str, float] | None = None,
          snapshot_dir: Path | None = None, confirm: bool = False) -> int:
    directory = snapshot_dir or ROOT / ".factory" / "usage" / "providers"
    expectations = expects or {}
    failures = 0
    for adapter in build_default_adapters(directory):
        if provider and adapter.spec.provider_id != provider:
            continue
        if adapter.spec.provider_id not in {"xai", "anthropic", "openai", "google"}:
            continue
        usage = adapter.inspect(force=True)
        if confirm and usage.provider_id in expectations and usage.plausibility_flags:
            weekly = next((w for w in usage.windows if w.window_duration_minutes == 10080), None)
            raw_used = usage.raw_fields.get("usagePercent") if usage.provider_id == "xai" else None
            if (weekly and type(raw_used) in (int, float)
                    and abs((100 - raw_used) - expectations[usage.provider_id]) <= 5):
                target = confirmation_path(directory, usage.provider_id)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps({"period_start": usage.period_start,
                    "quota_id": weekly.quota_id, "used_percent": round(raw_used, 2)},
                    ensure_ascii=False), encoding="utf-8")
                usage = adapter.inspect(force=True)
        headroom = _quota_headroom(usage)
        try:
            age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(usage.checked_at.replace("Z", "+00:00"))).total_seconds())
            age_text = f"{age / 60:.1f} min"
        except ValueError:
            age_text = "unknown"
        raw = sanitize_raw(usage.provider_id, usage.raw_fields)
        print(f"{usage.provider_id}: source={usage.adapter} age={age_text} status={usage.status.value} "
              f"raw={json.dumps(raw, ensure_ascii=False, sort_keys=True)}")
        for window in usage.windows:
            print(f"  {window.quota_id}: used={window.used_percent} remaining={window.remaining_percent} "
                  f"reset={window.resets_at}")
        rows = read_history(history_path(directory, usage.provider_id))
        flags = usage.plausibility_flags or (rows[-1].get("flags", []) if rows else [])
        print(f"  plausibility={flags or 'ok'}")
        if usage.provider_id in expectations:
            expected = expectations[usage.provider_id]
            if headroom is None or abs(headroom - expected) > 5:
                print(f"  MISMATCH: dashboard={expected:.1f}% meter={headroom}")
                failures += 1
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("anthropic", "openai", "google", "xai"))
    parser.add_argument("--expect", action="append", default=[], metavar="PROVIDER=PERCENT")
    parser.add_argument("--confirm", action="store_true", help="Confirm a suspicious Grok reading against --expect dashboard evidence")
    args = parser.parse_args()
    expected: dict[str, float] = {}
    for item in args.expect:
        try:
            name, percent = item.split("=", 1)
            if name not in {"anthropic", "openai", "google", "xai"} or not 0 <= float(percent) <= 100:
                raise ValueError(item)
            expected[name] = float(percent)
        except ValueError:
            parser.error(f"invalid --expect {item!r}; use provider=0..100")
    if args.confirm and not expected:
        parser.error("--confirm requires --expect")
    return audit(args.provider, expected, confirm=args.confirm)


if __name__ == "__main__":
    raise SystemExit(main())
