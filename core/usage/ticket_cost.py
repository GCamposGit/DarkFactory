"""Ticket Quota Cost Tracking and Telemetry (USR-113).

Records before/after quota headroom delta per ticket execution to calibrate
consumption estimates and inform pump/factory reports.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from core.paths import state_root

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPORTS_DIR = state_root() / "reports"
COST_LEDGER_FILENAME = "ticket_quota_usage.jsonl"


def record_ticket_quota_cost(
    ticket_id: str,
    harness: str,
    before_headroom: Optional[float],
    after_headroom: Optional[float],
    duration_s: float = 0.0,
    *,
    reports_dir: Optional[Path] = None,
    attempt_id: Optional[str] = None,
    model: Optional[str] = None,
    stage: Optional[str] = None,
    success: Optional[bool] = None,
    weekly_delta: Optional[float] = None,
    five_hour_delta: Optional[float] = None,
    api_cost_usd: Optional[float] = None,
) -> dict[str, Any]:
    """Record quota headroom before and after running a ticket (one row per attempt, failed ones included).

    The optional keyword fields (USR-378) are backward compatible: old rows without them still read.
    """
    if not ticket_id or not str(ticket_id).strip():
        raise ValueError("ticket_id e obrigatorio para registrar custo de uso")
    target_dir = reports_dir or (state_root() / "reports")
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
        "attempt_id": attempt_id or uuid4().hex,
        "model": model,
        "stage": stage,
        "success": success,
        "weekly_delta": weekly_delta,
        "five_hour_delta": five_hour_delta,
        "api_cost_usd": api_cost_usd,
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


WEEKS_PER_MONTH = 4.345
PRICES_PATH = PROJECT_ROOT / ".factory" / "config" / "subscription_prices.json"


class TicketCostRecord(BaseModel):
    """One usage row: a ticket attempt (quota delta) or an API call attributed to a ticket."""

    model_config = ConfigDict(extra="ignore")

    ticket_id: Optional[str] = None
    kind: str = "attempt"  # "attempt" (quota ledger) | "api_call" (model usage ledger)
    attempt_id: Optional[str] = None
    harness: Optional[str] = None
    model: Optional[str] = None
    stage: Optional[str] = None
    success: Optional[bool] = None
    delta_headroom: Optional[float] = None
    weekly_delta: Optional[float] = None
    five_hour_delta: Optional[float] = None
    api_cost_usd: Optional[float] = None
    timestamp: Optional[str] = None


class TicketCost(BaseModel):
    ticket_id: str
    attempts: int = 0
    failed_attempts: int = 0
    api_cost_usd: float = 0.0
    implicit_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    harnesses: list[str] = Field(default_factory=list)


def load_subscription_prices(path: Optional[Path] = None) -> dict[str, float]:
    """Monthly price in USD per harness (values may still be pending owner confirmation)."""
    try:
        data = json.loads((path or PRICES_PATH).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    plans = data.get("plans", {})
    return {
        str(k): float(v["monthly_usd"])
        for k, v in plans.items()
        if isinstance(v, dict) and isinstance(v.get("monthly_usd"), (int, float))
    }


def implicit_cost_usd(weekly_delta_pct: Optional[float], monthly_usd: float) -> float:
    """Weekly-window delta% / 100 x (monthly price / 4.345 weeks)."""
    if not weekly_delta_pct or weekly_delta_pct < 0 or monthly_usd <= 0:
        return 0.0
    return weekly_delta_pct / 100.0 * (monthly_usd / WEEKS_PER_MONTH)


def cost_by_ticket(
    records: Iterable[dict[str, Any] | TicketCostRecord], prices: dict[str, float]
) -> list[TicketCost]:
    """Aggregate per ticket: API cost + implicit subscription cost; failed attempts count.

    Rows without ticket_id are ignored (unattributable). The weekly delta falls back to the
    legacy ``delta_headroom`` column for rows that predate the weekly field.
    """
    rows: dict[str, TicketCost] = {}
    for raw in records:
        rec = raw if isinstance(raw, TicketCostRecord) else TicketCostRecord.model_validate(raw)
        tid = (rec.ticket_id or "").strip()
        if not tid:
            continue
        row = rows.setdefault(tid, TicketCost(ticket_id=tid))
        row.api_cost_usd += rec.api_cost_usd or 0.0
        if rec.harness and rec.harness not in row.harnesses:
            row.harnesses.append(rec.harness)
        if rec.kind != "attempt":
            continue
        row.attempts += 1
        if rec.success is False:
            row.failed_attempts += 1
        weekly = rec.weekly_delta if rec.weekly_delta is not None else rec.delta_headroom
        row.implicit_cost_usd += implicit_cost_usd(weekly, prices.get(rec.harness or "", 0.0))
    for row in rows.values():
        row.api_cost_usd = round(row.api_cost_usd, 6)
        row.implicit_cost_usd = round(row.implicit_cost_usd, 6)
        row.total_cost_usd = round(row.api_cost_usd + row.implicit_cost_usd, 6)
    return sorted(rows.values(), key=lambda r: r.total_cost_usd, reverse=True)


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
