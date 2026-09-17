"""CLI Interface for Portfolio Efficiency, Capacity, Budgets, and Routing (HF-23).

Governed by Universal Engineering Standards (AGENTS.md).
Provides deterministic, headless inspection and management of project queues,
slot capacity limits, financial ceilings, and model routing decisions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from .budget_manager import PortfolioBudgetManager
from .models import JobSlotKind, PortfolioEfficiencyReport, utc_now_iso
from .router_optimizer import PortfolioModelRouter
from .scheduler import PortfolioScheduler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="darkfac-portfolio",
        description="Dark Factory Portfolio Efficiency, Capacity & Routing Engine (HF-23)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: status
    subparsers.add_parser("status", help="Display active slots, queue metrics, and project budget statuses.")

    # Subcommand: set-budget
    p_bset = subparsers.add_parser("set-budget", help="Configure project monthly budget ceiling in USD.")
    p_bset.add_argument("--project", required=True, help="Project identifier (e.g. atrium, jarvis, darkfac).")
    p_bset.add_argument("--limit", required=True, type=float, help="Monthly budget limit in USD.")

    # Subcommand: record-spend
    p_bspend = subparsers.add_parser("record-spend", help="Record API spend against a project budget.")
    p_bspend.add_argument("--project", required=True, help="Project identifier.")
    p_bspend.add_argument("--amount", required=True, type=float, help="Amount spent in USD.")

    # Subcommand: route-task
    p_route = subparsers.add_parser("route-task", help="Resolve the optimal model for a specific task and project.")
    p_route.add_argument("--task-type", required=True, help="Task type (research, web_research, architecture, coding_high, review).")
    p_route.add_argument("--project", default="atrium", help="Project identifier.")

    # Subcommand: enqueue-job
    p_enq = subparsers.add_parser("enqueue-job", help="Enqueue a job into the weighted fair scheduler.")
    p_enq.add_argument("--job-id", required=True, help="Job identifier.")
    p_enq.add_argument("--project", required=True, help="Project identifier.")
    p_enq.add_argument("--kind", choices=["heavy", "light"], default="light", help="Execution intensity.")
    p_enq.add_argument("--priority", type=int, default=0, help="Priority index.")

    # Subcommand: dequeue-job
    subparsers.add_parser("dequeue-job", help="Dequeue the next highest priority job according to Weighted Fairness.")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    budget_mgr = PortfolioBudgetManager()
    scheduler = PortfolioScheduler()
    router = PortfolioModelRouter(budget_manager=budget_mgr)

    if args.command == "status":
        heavy, light = scheduler.get_active_counts()
        report = PortfolioEfficiencyReport(
            active_heavy_slots=heavy,
            max_heavy_slots=scheduler.capacity.max_heavy_slots,
            active_light_slots=light,
            max_light_slots=scheduler.capacity.max_light_slots,
            queued_jobs_by_project=scheduler.get_queue_status(),
            starvation_ticks_by_project=scheduler.get_starvation_status(),
            budgets=budget_mgr.list_budgets(),
            generated_at=utc_now_iso(),
        )
        print(report.model_dump_json(indent=2))
        return 0

    elif args.command == "set-budget":
        updated = budget_mgr.set_budget(args.project, args.limit)
        print(updated.model_dump_json(indent=2))
        return 0

    elif args.command == "record-spend":
        updated = budget_mgr.record_spend(args.project, args.amount)
        print(updated.model_dump_json(indent=2))
        return 0

    elif args.command == "route-task":
        res = router.route_task(task_type=args.task_type, project_id=args.project)
        print(res.model_dump_json(indent=2))
        return 0

    elif args.command == "enqueue-job":
        kind = JobSlotKind.HEAVY if args.kind == "heavy" else JobSlotKind.LIGHT
        scheduler.enqueue_job(job_id=args.job_id, project_id=args.project, kind=kind, priority=args.priority)
        print(json.dumps({"success": True, "job_id": args.job_id, "project_id": args.project, "kind": kind.value}, indent=2))
        return 0

    elif args.command == "dequeue-job":
        job = scheduler.dequeue_next()
        if job:
            print(json.dumps({"success": True, "job": job}, indent=2))
            return 0
        else:
            print(json.dumps({"success": False, "message": "No dispatchable jobs in queue."}, indent=2))
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
