"""HF-27-10 -- Daily E2E canary for the DarkFac production line.

Once a day the factory submits a small, deterministic demand to its own
line through the same public intake every other demand uses
(`core.demands.autonomous_intake.AutonomousIntakeService`), then *observes
without intervening*: per-stage timings, harness/role, cost, retries and a
`GET /version` smoke check against the deployed canary app. The result is a
Pydantic report written to `.factory/reports/canary/<date>.json`.

This replaces the HF-15-02 manual protocol. The V2 exit criterion is
`green_streak(reports) >= 7` (see `is_v2_criterion_met`); once that holds,
`core.line.dogfood` starts feeding the factory's own backlog back through
the line.

Design notes
------------
- Domain (scenario selection, streak math, report shape) is plain Python /
  Pydantic with no I/O. The three side-effecting boundaries -- the clock,
  the `/version` smoke probe, and "what happened on the line for this
  run_id" -- are each a small `Protocol` with a real adapter and an
  injectable fake, per AGENTS.md's "domain e I/O separados" rule.
- Idempotency reuses `ControlStore.accept`'s own contract (HF-08-01):
  replaying the *same* `IntakeCommand` (deterministic payload for a given
  calendar day) returns the same `IntakeReceipt` instead of raising or
  duplicating. `run_daily` therefore does not need its own duplicate
  bookkeeping -- a second same-day run just resumes observing the same
  `run_id`.
- Failure notification and the weekly summary reuse
  `core.notifications.service.NotificationService`, the same sender
  `core.line.human` uses for owner notifications, instead of inventing a
  parallel Telegram path.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Protocol

from pydantic import BaseModel, Field

from core.demands.autonomous_intake import AutonomousIntakeService
from core.demands.store import DemandsStore
from core.workflow.control_contracts import IdempotencyConflict, IntakeCommand
from core.workflow.control_store import ControlStore, SQLiteControlStore

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CANARY_PROJECT_ID = "darkfac-canary"
CANARY_CHANNEL = "canary"
REPORTS_DIR = PROJECT_ROOT / ".factory" / "reports" / "canary"
DEFAULT_CONTROL_DB = PROJECT_ROOT / ".factory" / "control.db"

CanaryScenario = Literal["normal", "initial_test_fail", "smoke_rollback"]


# --------------------------------------------------------------------------
# Domain: deterministic demand + weekly scenario selection
# --------------------------------------------------------------------------


def select_scenario(day: date) -> CanaryScenario:
    """Deterministic weekly controlled-failure rotation (handoff section
    "Semanalmente, variar o canário com um cenário de falha controlada").

    Pure function of the calendar date so the choice is reproducible and
    reportable without any extra state: Monday exercises the validate loop
    (a demand whose initial test fails), Thursday exercises a smoke check
    that requires rollback, every other day runs the plain green path.
    """
    weekday = day.weekday()  # Monday=0 ... Sunday=6
    if weekday == 0:
        return "initial_test_fail"
    if weekday == 3:
        return "smoke_rollback"
    return "normal"


def _demand_text(day: date) -> str:
    return (
        f"Adicione ao /version o campo `canary_day` com a data de hoje "
        f"`{day.isoformat()}` e um teste."
    )


def build_intake_command(day: date, scenario: CanaryScenario) -> IntakeCommand:
    """The exact demand text from the handoff, submitted for `darkfac-canary`.

    `external_id=canary:<date>` is the idempotency key `ControlStore.accept`
    keys off; `scenario` rides in the payload (and therefore the payload
    digest) so a same-day replay only ever matches its own scenario.
    """
    criteria = [f"GET /version contém canary_day={day.isoformat()}"]
    if scenario == "initial_test_fail":
        criteria.append("Teste inicial deve falhar de propósito (exercita o validate loop)")
    elif scenario == "smoke_rollback":
        criteria.append("Smoke deve exigir rollback de propósito")

    return IntakeCommand(
        project_id=CANARY_PROJECT_ID,
        channel=CANARY_CHANNEL,
        external_id=f"canary:{day.isoformat()}",
        mode="autonomous",
        policy_ref="darkfac://line/canary/v1",
        payload={
            "title": f"Canário diário {day.isoformat()}",
            "problem": _demand_text(day),
            "journey": "GET /version reflete canary_day do dia",
            "non_goals": [],
            "criteria": criteria,
            "scenario": scenario,
        },
    )


# --------------------------------------------------------------------------
# Report shape
# --------------------------------------------------------------------------


class CanaryStageObservation(BaseModel):
    """One line stage's telemetry, as observed (never mutated) by the canary."""

    stage: str
    status: str
    iteration: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    actual_cost: float = 0.0
    cause_code: str | None = None
    harness: str | None = None
    model_used: str | None = None


class SmokeResult(BaseModel):
    ok: bool
    url: str
    expected_date: str
    observed_body: str | None = None
    error: str | None = None
    rollback_required: bool = False


class CanaryReport(BaseModel):
    date: str
    scenario: CanaryScenario
    external_id: str
    run_id: str | None = None
    demand_id: str | None = None
    stages: list[CanaryStageObservation] = Field(default_factory=list)
    smoke: SmokeResult | None = None
    passed: bool = False
    failing_stage: str | None = None
    cause_code: str | None = None
    dry_run: bool = False
    notes: str = ""


# --------------------------------------------------------------------------
# Injectable I/O boundaries
# --------------------------------------------------------------------------


class Clock(Protocol):
    def today(self) -> date: ...

    def now(self) -> datetime: ...


class SystemClock:
    def today(self) -> date:
        return datetime.now(UTC).date()

    def now(self) -> datetime:
        return datetime.now(UTC)


class SmokeClient(Protocol):
    def check(self, base_url: str, expected_date: date, scenario: CanaryScenario) -> SmokeResult: ...


class HttpSmokeClient:
    """Real adapter: `GET {base_url}/version`, checks it contains the date."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def check(self, base_url: str, expected_date: date, scenario: CanaryScenario) -> SmokeResult:
        import urllib.error
        import urllib.request

        url = f"{base_url.rstrip('/')}/version"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:  # noqa: S310 - fixed canary host
                body = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return SmokeResult(
                ok=False,
                url=url,
                expected_date=expected_date.isoformat(),
                error=str(exc),
                rollback_required=(scenario == "smoke_rollback"),
            )
        ok = expected_date.isoformat() in body
        return SmokeResult(
            ok=ok,
            url=url,
            expected_date=expected_date.isoformat(),
            observed_body=body[:500],
            rollback_required=(scenario == "smoke_rollback" and not ok),
        )


class LineObserver(Protocol):
    def observe(self, run_id: str) -> list[CanaryStageObservation]: ...


class ControlStoreLineObserver:
    """Reads stage telemetry from `ControlStore.get_run_status` (HF-27-10
    addition to `SQLiteControlStore`, mirroring the pre-existing
    `PostgresControlStore.get_run_status`). Never claims or mutates jobs."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def observe(self, run_id: str) -> list[CanaryStageObservation]:
        getter = getattr(self.store, "get_run_status", None)
        if getter is None:
            logger.warning("Store %r has no get_run_status; canary cannot observe stages", type(self.store))
            return []
        status = getter(run_id)
        if not status:
            return []
        observations: list[CanaryStageObservation] = []
        for job in status.get("jobs", []):
            observations.append(
                CanaryStageObservation(
                    stage=job.get("stage", ""),
                    status=job.get("status", "unknown"),
                    iteration=int(job.get("iteration") or 0),
                    started_at=job.get("started_at"),
                    finished_at=job.get("finished_at"),
                    actual_cost=float(job.get("actual_cost") or 0.0),
                    cause_code=job.get("cause_code"),
                    harness=job.get("role"),
                )
            )
        return observations


NotifySender = Callable[[str], bool]


def _default_failure_sender() -> Optional[NotifySender]:
    try:
        from core.notifications.models import AlertCategory, AlertSeverity
        from core.notifications.service import NotificationService
    except Exception:  # pragma: no cover - defensive
        return None

    service = NotificationService()

    def _send(text: str) -> bool:
        event = service.notify(
            category=AlertCategory.SYSTEM_HEALTH,
            severity=AlertSeverity.CRITICAL,
            title="Canário E2E falhou",
            message=text,
            force=True,
        )
        return event is not None

    return _send


def _default_summary_sender() -> Optional[NotifySender]:
    try:
        from core.notifications.models import AlertCategory, AlertSeverity
        from core.notifications.service import NotificationService
    except Exception:  # pragma: no cover - defensive
        return None

    service = NotificationService()

    def _send(text: str) -> bool:
        event = service.notify(
            category=AlertCategory.SYSTEM_HEALTH,
            severity=AlertSeverity.INFO,
            title="Resumo semanal do canário",
            message=text,
            force=True,
        )
        return event is not None

    return _send


# --------------------------------------------------------------------------
# Report persistence + green streak
# --------------------------------------------------------------------------


def _report_path(reports_dir: Path, day: date) -> Path:
    return reports_dir / f"{day.isoformat()}.json"


def _write_report(reports_dir: Path, day: date, report: CanaryReport) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = _report_path(reports_dir, day)
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_reports(reports_dir: Path = REPORTS_DIR, limit: int | None = None) -> list[CanaryReport]:
    """All readable reports, oldest first. `limit` keeps only the most recent N."""
    if not reports_dir.exists():
        return []
    reports: list[CanaryReport] = []
    for path in sorted(reports_dir.glob("*.json")):
        try:
            reports.append(CanaryReport.model_validate_json(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            logger.warning("Skipping unreadable canary report %s: %s", path, exc)
    reports.sort(key=lambda r: r.date)
    if limit is not None and limit > 0:
        reports = reports[-limit:]
    return reports


def green_streak(reports: list[CanaryReport]) -> int:
    """Consecutive green days counted backward from the most recent report.

    A missing calendar day or a failed (`passed=False`) report stops the
    count. Order-independent: `reports` is re-sorted by `date` first.
    """
    if not reports:
        return 0
    ordered = sorted(reports, key=lambda r: r.date)
    streak = 0
    expected: date | None = None
    for report in reversed(ordered):
        report_date = date.fromisoformat(report.date)
        if expected is not None and report_date != expected:
            break
        if not report.passed:
            break
        streak += 1
        expected = report_date - timedelta(days=1)
    return streak


def is_v2_criterion_met(reports: list[CanaryReport], *, threshold: int = 7) -> bool:
    """The plan's V2 exit criterion: `threshold` consecutive green canary days."""
    return green_streak(reports) >= threshold


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def _notify_failure(report: CanaryReport, sender: Optional[NotifySender]) -> bool:
    text = (
        f"[CANARIO FALHOU] {report.date} etapa={report.failing_stage} "
        f"cause_code={report.cause_code} cenario={report.scenario} run_id={report.run_id}"
    )
    send = sender if sender is not None else _default_failure_sender()
    if send is None:
        logger.warning("No notifier available for canary failure (%s)", report.date)
        return False
    try:
        return bool(send(text))
    except Exception as exc:  # pragma: no cover - notification must never break the canary
        logger.warning("Failed to send canary failure notification: %s", exc)
        return False


def send_weekly_summary(
    *,
    reports_dir: Path = REPORTS_DIR,
    today: date | None = None,
    sender: Optional[NotifySender] = None,
) -> bool:
    """Last 7 reports + green streak, sent through the Telegram notifier."""
    today = today or SystemClock().today()
    reports = load_reports(reports_dir, limit=7)
    streak = green_streak(reports)
    lines = [f"[CANARIO] Resumo semanal ({today.isoformat()})", f"Green streak: {streak} dia(s)"]
    for report in reports:
        mark = "OK" if report.passed else "FALHA"
        line = f"  {report.date} [{report.scenario}] {mark}"
        if not report.passed:
            line += f" etapa={report.failing_stage} cause={report.cause_code}"
        lines.append(line)
    if streak >= 7:
        lines.append("V2 atingido: 7 dias verdes seguidos.")
    text = "\n".join(lines)

    send = sender if sender is not None else _default_summary_sender()
    if send is None:
        logger.warning("No notifier available for canary weekly summary")
        return False
    try:
        return bool(send(text))
    except Exception as exc:  # pragma: no cover - notification must never break the canary
        logger.warning("Failed to send canary weekly summary: %s", exc)
        return False


def run_daily(
    *,
    day: date | None = None,
    store: ControlStore | None = None,
    demands_store: DemandsStore | None = None,
    observer: LineObserver | None = None,
    smoke_client: SmokeClient | None = None,
    clock: Clock | None = None,
    failure_sender: Optional[NotifySender] = None,
    summary_sender: Optional[NotifySender] = None,
    base_url: str | None = None,
    reports_dir: Path = REPORTS_DIR,
    dry_run: bool = False,
    send_weekly: bool = True,
) -> CanaryReport:
    """Submit (or resume observing) today's canary demand and write a report.

    Idempotent: a second call for the same `day` submits the identical
    `IntakeCommand`, which `ControlStore.accept` resolves to the exact same
    `IntakeReceipt` (HF-08-01 contract) instead of creating a new run --
    this function then simply re-observes that same `run_id`.
    """
    clock = clock or SystemClock()
    today = day or clock.today()
    scenario = select_scenario(today)
    command = build_intake_command(today, scenario)

    if dry_run:
        report = CanaryReport(
            date=today.isoformat(),
            scenario=scenario,
            external_id=command.external_id,
            passed=True,
            dry_run=True,
            notes="dry-run: intake not submitted",
        )
        _write_report(reports_dir, today, report)
        return report

    store = store or SQLiteControlStore(db_path=DEFAULT_CONTROL_DB)
    service = AutonomousIntakeService(store=store, demands_store=demands_store)

    try:
        receipt = service.accept(command, clock.now())
    except IdempotencyConflict as exc:
        # A pure function of `today` should never collide with a different
        # payload, but stay fail-closed instead of crashing the daily job.
        logger.error("Canary intake conflict for %s: %s", command.external_id, exc)
        report = CanaryReport(
            date=today.isoformat(),
            scenario=scenario,
            external_id=command.external_id,
            passed=False,
            failing_stage="intake",
            cause_code="idempotency_conflict",
            notes=str(exc),
        )
        _write_report(reports_dir, today, report)
        _notify_failure(report, failure_sender)
        return report

    run_id = receipt.run_id
    observer = observer or ControlStoreLineObserver(store)
    stages = observer.observe(run_id) if run_id else []

    smoke: SmokeResult | None = None
    if base_url:
        active_smoke_client = smoke_client or HttpSmokeClient()
        smoke = active_smoke_client.check(base_url, today, scenario)

    failing_stage: str | None = None
    cause_code: str | None = None
    for observation in stages:
        if observation.status == "failed":
            failing_stage = observation.stage
            cause_code = observation.cause_code
            break

    passed = failing_stage is None and (smoke is None or smoke.ok)
    if smoke is not None and not smoke.ok and failing_stage is None:
        failing_stage = "smoke"
        cause_code = cause_code or ("rollback_required" if smoke.rollback_required else "smoke_check_failed")
        passed = False

    report = CanaryReport(
        date=today.isoformat(),
        scenario=scenario,
        external_id=command.external_id,
        run_id=run_id,
        demand_id=receipt.demand_id,
        stages=stages,
        smoke=smoke,
        passed=passed,
        failing_stage=failing_stage,
        cause_code=cause_code,
    )
    _write_report(reports_dir, today, report)

    if not passed:
        _notify_failure(report, failure_sender)

    if send_weekly and today.weekday() == 0:  # Monday
        send_weekly_summary(reports_dir=reports_dir, today=today, sender=summary_sender)

    return report


# --------------------------------------------------------------------------
# CLI (python -m core.line.canary run|summary|status)
# --------------------------------------------------------------------------


def _cli_run(args: argparse.Namespace) -> int:
    day = date.fromisoformat(args.date) if args.date else None
    report = run_daily(day=day, base_url=args.base_url, dry_run=args.dry_run)
    print(report.model_dump_json(indent=2))
    return 0 if (report.passed or report.dry_run) else 1


def _cli_summary(args: argparse.Namespace) -> int:
    today = date.fromisoformat(args.date) if args.date else SystemClock().today()
    reports = load_reports(REPORTS_DIR, limit=7)
    streak = green_streak(reports)
    print(
        json.dumps(
            {
                "green_streak": streak,
                "v2_met": streak >= 7,
                "reports": [r.date for r in reports],
            },
            indent=2,
        )
    )
    if not args.no_send:
        send_weekly_summary(today=today)
    return 0


def _cli_status(_args: argparse.Namespace) -> int:
    reports = load_reports(REPORTS_DIR, limit=7)
    streak = green_streak(reports)
    last = reports[-1] if reports else None
    print(
        json.dumps(
            {
                "green_streak": streak,
                "v2_met": streak >= 7,
                "last_report": last.model_dump(mode="json") if last else None,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="DarkFac daily E2E canary (HF-27-10)")
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Submit/observe today's canary demand and write a report")
    run_p.add_argument("--date", default=None, help="YYYY-MM-DD (defaults to today, UTC)")
    run_p.add_argument("--base-url", default=None, help="Canary app base URL for the /version smoke check")
    run_p.add_argument("--dry-run", action="store_true", help="Do not submit an intake; write a dry-run report")

    summary_p = sub.add_parser("summary", help="Print and send the weekly summary (last 7 reports)")
    summary_p.add_argument("--date", default=None)
    summary_p.add_argument("--no-send", action="store_true", help="Print only; skip the notifier")

    sub.add_parser("status", help="Print the current green streak and last report")

    args = parser.parse_args(argv)
    if args.command == "run":
        return _cli_run(args)
    if args.command == "summary":
        return _cli_summary(args)
    if args.command == "status":
        return _cli_status(args)
    return 1  # pragma: no cover - argparse enforces `required=True`


if __name__ == "__main__":
    sys.exit(main())
