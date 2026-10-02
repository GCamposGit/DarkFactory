"""Ticket Quota Cost Tracking and Telemetry (USR-113).

Records before/after quota headroom delta per ticket execution to calibrate
consumption estimates and inform pump/factory reports.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORTS_DIR = PROJECT_ROOT / ".factory" / "reports"
COST_LEDGER_FILENAME = "ticket_quota_usage.jsonl"


def record_ticket_quota_cost(
    ticket_id: str,
    harness: str,
    before_headroom: Optional[float],
    after_headroom: Optional[float],
    duration_s: float = 0.0,
    *,
    reports_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """Record quota headroom before and after running a ticket."""
    target_dir = reports_dir or DEFAULT_REPORTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / COST_LEDGER_FILENAME

    delta: Optional[float] = None
    if before_headroom is not None and after_headroom is not None:
        delta = round(before_headroom - after_headroom, 2)

    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ticket_id": ticket_id,
        "harness": harness,
        "before_headroom": before_headroom,
        "after_headroom": after_headroom,
        "delta_headroom": delta,
        "duration_s": round(duration_s, 2),
    }

    with target_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return record


def read_ticket_quota_history(reports_dir: Optional[Path] = None) -> list[dict[str, Any]]:
    """Read full ticket quota cost history from jsonl ledger."""
    target_dir = reports_dir or DEFAULT_REPORTS_DIR
    target_file = target_dir / COST_LEDGER_FILENAME
    if not target_file.is_file():
        return []

    records: list[dict[str, Any]] = []
    with target_file.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(json.loads(stripped))
            except json.JSONDecodeError:
                continue
    return records


def format_ticket_quota_summary(record: dict[str, Any]) -> str:
    """Format human-readable quota consumption summary."""
    before = f"{record['before_headroom']:.1f}%" if record.get("before_headroom") is not None else "desconhecido"
    after = f"{record['after_headroom']:.1f}%" if record.get("after_headroom") is not None else "desconhecido"
    delta = record.get("delta_headroom")
    delta_str = f"{delta:+.1f}%" if delta is not None else "indisponível"

    return (
        f"[COTA] Consumo no ticket {record.get('ticket_id', 'n/d')} ({record.get('harness', 'n/d')}): "
        f"antes={before}, depois={after}, delta={delta_str} (duração: {record.get('duration_s', 0.0):.1f}s)"
    )
