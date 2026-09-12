#!/usr/bin/env python3
"""
Dark Factory Model Benchmark & Efficiency Frontier CLI.
Usage:
    python -m core.benchmarks.cli run [--force]
    python -m core.benchmarks.cli status
    python -m core.benchmarks.cli frontier
    python -m core.benchmarks.cli table
    python -m core.benchmarks.cli recommend --task-type coding --complexity high [--offline]
"""

import sys
import json
import argparse
import time
from typing import List

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from core.benchmarks.models import ModelBenchmarkEntry, DailyBenchmarkLedger, ModelTier
from core.benchmarks.fetcher import (
    DailyBenchmarkService,
    get_daily_benchmark_service,
    ensure_daily_benchmark,
)
from core.benchmarks.frontier import (
    DOMAIN_METADATA,
    compute_pareto_frontier,
    compute_domain_pareto_frontiers,
    compute_frontier_proximity_indices,
    get_top_candidates_for_tier,
    get_domain_top3_candidates,
    build_frontier_summary,
    select_best_model_for_task,
)


def format_currency(val: float) -> str:
    if val == 0.0:
        return "$0.00"
    if val < 0.01:
        return f"${val:.4f}"
    return f"${val:.2f}"


def print_frontier_table(models: List[ModelBenchmarkEntry], title: str = "PARETO EFFICIENCY FRONTIER"):
    print("=" * 125)
    print(f" {title}")
    print("=" * 125)
    header = (
        f"{'Model ID':<30} | {'Provider':<10} | {'API In/Out':<13} | {'API/Task':<8} | "
        f"{'Plano ($0 marg.)':<18} | {'Code':<5} | {'TPS':<5} | {'Valor Cota':<10}"
    )
    print(header)
    print("-" * 125)

    for m in models:
        plan_str = m.subscription_name if m.has_subscription_plan and m.subscription_name else "-"
        quota_save = f"+{format_currency(m.cost_per_task)}" if m.has_subscription_plan else "$0.00"
        cost_in_out = f"{format_currency(m.input_cost_per_m)}/{format_currency(m.output_cost_per_m)}"
        coding_str = f"{m.coding_score:<5.1f}" if m.coding_score is not None else "  n/a"
        tps_str = f"{m.output_speed_tps:<5.0f}" if m.output_speed_tps is not None else "  n/a"

        row = (
            f"{m.model_id[:30]:<30} | "
            f"{m.provider[:10]:<10} | "
            f"{cost_in_out:<13} | "
            f"{format_currency(m.cost_per_task):<8} | "
            f"{plan_str[:18]:<18} | "
            f"{coding_str:<5} | "
            f"{tps_str:<5} | "
            f"{quota_save:<10}"
        )
        print(row)
    print("=" * 125)


def cmd_run(args):
    service = get_daily_benchmark_service()
    today = service.get_today_str()
    should_run = service.should_run_today()

    if not should_run and not args.force:
        print(f"[STATUS] Benchmark already up to date for today ({today}). Skipping run.")
        print("         Use --force to update again if needed.")
        ledger = service.load_latest_ledger()
    else:
        print(f"[BENCHMARK] Executing daily model benchmark update for {today}...")
        ledger = service.build_daily_ledger(force=args.force)
        print(f"[OK] Daily benchmark completed! Monitored {ledger.total_models_scanned} models.")
        print(f"     Coding Pareto Frontier models: {len(ledger.pareto_coding_models)}")

    if ledger:
        print(f"\nRecommended Models by Tier:")
        for tier, mid in ledger.recommendations_by_tier.items():
            print(f"  - {tier}: {mid}")


def cmd_status(args):
    service = get_daily_benchmark_service()
    today = service.get_today_str()
    ledger = service.load_latest_ledger()

    if not ledger:
        print(f"[STATUS] No benchmark ledger found. Run 'python -m core.benchmarks.cli run' to initialize.")
        return

    is_today = ledger.date == today
    status_str = "CURRENT (Today)" if is_today else f"OUTDATED (Last run: {ledger.date})"

    print("=" * 60)
    print(" DARK FACTORY - DAILY MODEL BENCHMARK STATUS")
    print("=" * 60)
    print(f" Status:               {status_str}")
    print(f" Ledger Date:          {ledger.date}")
    print(f" Updated At:           {ledger.updated_at}")
    print(f" Total Models Scanned: {ledger.total_models_scanned}")
    print(f" Coding Frontier:      {len(ledger.pareto_coding_models)} models")
    print(f" General Frontier:     {len(ledger.pareto_general_models)} models")
    print("-" * 60)
    print(" Recommendations:")
    for tier, mid in ledger.recommendations_by_tier.items():
        print(f"   {tier:<20}: {mid}")
    print("=" * 60)


def cmd_frontier(args):
    service = get_daily_benchmark_service()
    ledger = service.load_latest_ledger()
    if not ledger:
        ledger = ensure_daily_benchmark()

    all_models = list(ledger.models.values())
    summary = build_frontier_summary(ledger.date, all_models)

    print(f"\nBenchmark Date: {ledger.date} (Artificial Analysis & OpenRouter)")
    print_frontier_table(summary.coding_frontier, title="CODING AGENT PARETO FRONTIER")


def cmd_table(args):
    service = get_daily_benchmark_service()
    ledger = service.load_latest_ledger()
    if not ledger:
        ledger = ensure_daily_benchmark()

    all_models = sorted(ledger.models.values(), key=lambda m: (not m.is_pareto_coding, -m.coding_score))
    print_frontier_table(all_models, title=f"ALL MONITORED FRONTIER MODELS ({ledger.date})")


def cmd_recommend(args):
    service = get_daily_benchmark_service()
    ledger = service.load_latest_ledger()
    if not ledger:
        ledger = ensure_daily_benchmark()

    all_models = list(ledger.models.values())
    best_model, reason = select_best_model_for_task(
        models=all_models,
        task_type=args.task_type,
        complexity=args.complexity,
        offline=args.offline,
    )

    result = {
        "model": best_model.model_id,
        "name": best_model.name,
        "provider": best_model.provider,
        "tier": best_model.tier.value,
        "coding_score": best_model.coding_score,
        "cost_per_task": best_model.cost_per_task,
        "output_speed_tps": best_model.output_speed_tps,
        "efficiency_score": best_model.efficiency_score_coding,
        "is_pareto_optimal": best_model.is_pareto_coding,
        "reason": reason,
    }
    print(json.dumps(result, indent=2))


def cmd_proximity(args):
    service = get_daily_benchmark_service()
    ledger = service.load_latest_ledger()
    if not ledger:
        ledger = ensure_daily_benchmark()

    all_models = list(ledger.models.values())
    compute_frontier_proximity_indices(all_models, metric="coding_score")

    # Sort primarily by proximity descending, then code score descending
    sorted_models = sorted(
        all_models,
        key=lambda m: (-m.frontier_proximity_index, -m.coding_score)
    )

    print("=" * 135)
    print(f" FRONTIER PROXIMITY INDEX (FPI) & EPSILON-DOMINANCE ANALYSIS ({ledger.date})")
    print("=" * 135)
    header = (
        f"{'Model ID':<28} | {'Provider':<10} | {'API/Task':<8} | {'Code':<5} | "
        f"{'FPI (%)':<7} | {'ε-Cap (pts)':<11} | {'ε-Cost ($)':<11} | {'Opp. Score':<10} | {'Status':<22}"
    )
    print(header)
    print("-" * 135)

    for m in sorted_models:
        if m.is_pareto_coding:
            status = "[*] PARETO OPTIMAL"
        elif m.is_near_pareto:
            status = "[+] NEAR-CHALLENGER"
        elif m.tier == ModelTier.LOCAL_ZERO_COST:
            status = "[-] LOCAL $0 TIER"
        else:
            status = "    DOMINATED"

        eps_c = format_currency(m.epsilon_gap_cost) if m.epsilon_gap_cost > 0 else "-"
        eps_q = f"+{m.epsilon_gap_capability:.1f} pts" if m.epsilon_gap_capability > 0 else "-"

        row = (
            f"{m.model_id[:28]:<28} | "
            f"{m.provider[:10]:<10} | "
            f"{format_currency(m.cost_per_task):<8} | "
            f"{m.coding_score:<5.1f} | "
            f"{m.frontier_proximity_index * 100.0:>5.1f}% | "
            f"{eps_q:<11} | "
            f"{eps_c:<11} | "
            f"{m.opportunity_score:>10.4f} | "
            f"{status:<22}"
        )
        print(row)
    print("=" * 135)
    print(" Legend:")
    print("   [*] PARETO OPTIMAL: Defines the current frontier curve (0 distance).")
    print("   [+] NEAR-CHALLENGER: FPI >= 95% (near-optimal trade-off; high opportunity in speed/context).")
    print("   [-] LOCAL $0 TIER: Zero-cost Ollama worker for private/offline execution.")


def cmd_top3(args):
    service = get_daily_benchmark_service()
    ledger = service.load_latest_ledger()
    if not ledger:
        ledger = ensure_daily_benchmark()

    all_models = list(ledger.models.values())
    candidates = get_top_candidates_for_tier(all_models, tier=args.complexity, k=3)

    print("=" * 105)
    print(f" TOP 3 SPECULATIVE CANDIDATES FOR TIER: {args.complexity.upper()}")
    print("=" * 105)
    header = f"{'Model ID':<28} | {'Role':<20} | {'Cost/Task':<10} | {'TPS':<5} | {'Code':<5} | {'FPI (%)':<7} | {'Pareto?'}"
    print(header)
    print("-" * 105)

    for c in candidates:
        is_p = "YES" if c.is_pareto else "NO"
        print(
            f"{c.model_id[:28]:<28} | "
            f"{c.role:<20} | "
            f"{format_currency(c.cost_per_task):<10} | "
            f"{c.output_speed_tps:<5.0f} | "
            f"{c.coding_score:<5.1f} | "
            f"{c.frontier_proximity * 100.0:>5.1f}% | "
            f"{is_p}"
        )
    print("=" * 105)


def cmd_race(args):
    from core.benchmarks.racing import SpeculativeRacingEngine
    engine = SpeculativeRacingEngine()

    print(f"\n[SPECULATIVE RACE] Initiating race for tier '{args.complexity}' (Offline: {args.offline})...")
    result = engine.execute_speculative_race(
        task_id=f"cli_task_{int(time.time())}",
        task_prompt=args.prompt,
        complexity=args.complexity,
        offline=args.offline,
    )

    print("=" * 70)
    print(" SPECULATIVE RACE RESULT")
    print("=" * 70)
    print(f" Race ID:             {result.race_id}")
    print(f" Task Complexity:     {result.complexity}")
    print(f" Candidates:          {', '.join(result.candidates)}")
    print(f" Winner Model:        {result.winner_model_id} ({result.winner_role})")
    print(f" Escalation:          {'YES (Drafter failed)' if result.escalation_occurred else 'NO (One-shot verified)'}")
    print(f" Execution Time:      {result.duration_ms:.2f} ms")
    print(f" Cost Incurred:       ${result.total_cost_usd:.6f}")
    print(f" Cost Saved:          +${result.cost_saved_usd:.6f}")
    print(f" Verification Status: {'PASSED' if result.verification_passed else 'FAILED'}")
    print(f" Execution Mode:      {result.verification_details.get('execution_mode', 'unknown')}")
    print(f" Model Inference:     {'YES' if result.verification_details.get('model_inference_executed') else 'NO'}")
    print("=" * 70)


def cmd_tournament(args):
    from core.benchmarks.racing import SpeculativeRacingEngine
    engine = SpeculativeRacingEngine()

    print(f"\n[TOURNAMENT] Running empirical tournament across top models for tier '{args.complexity}'...")
    tournament_data = engine.run_empirical_tournament(complexity=args.complexity, offline=args.offline)

    print("=" * 95)
    print(" EMPIRICAL LEADERBOARD (Dark Factory Windows Environment)")
    print("=" * 95)
    header = f"{'Model ID':<30} | {'Elo Rating':<11} | {'Pass@1':<8} | {'Runs':<6} | {'Cost Spent':<11}"
    print(header)
    print("-" * 95)

    for mid, stats in tournament_data["leaderboard"].items():
        print(
            f"{mid[:30]:<30} | "
            f"{stats['elo_rating']:<11.1f} | "
            f"{stats['pass_rate_at_1'] * 100.0:>6.1f}% | "
            f"{stats['total_tasks_run']:<6} | "
            f"${stats['total_cost_spent']:<11.6f}"
        )
    print("=" * 95)
    print(f"[OK] Tournament recorded. Cumulative ledger saved to .factory/benchmarks/empirical_ledger.json")


def print_domain_frontier_table(models: List[ModelBenchmarkEntry], domain: str = "coding"):
    print("=" * 115)
    print(f" PARETO FRONTIER FOR DOMAIN: {domain.upper()}")
    print("=" * 115)
    header = f"{'Model ID':<30} | {'Provider':<10} | {'Cost/Task':<10} | {'Domain Score':<12} | {'TPS':<5} | {'Plan'}"
    print(header)
    print("-" * 115)
    for m in models:
        plan_str = m.subscription_name if m.has_subscription_plan and m.subscription_name else "-"
        score = m.get_domain_score(domain)
        print(f"{m.model_id[:30]:<30} | {m.provider[:10]:<10} | {format_currency(m.cost_per_task):<10} | {score:<12.1f} | {m.output_speed_tps:<5.0f} | {plan_str[:15]}")
    print("=" * 115)


def cmd_domains(args):
    service = get_daily_benchmark_service()
    ledger = service.load_latest_ledger() or ensure_daily_benchmark()
    all_models = list(ledger.models.values())
    frontiers = compute_domain_pareto_frontiers(all_models)

    print("=" * 125)
    print(" DARK FACTORY - TASK-ADAPTIVE BENCHMARK DOMAINS & CANONICAL STANDARDS")
    print("=" * 125)
    header = f"{'Domain':<22} | {'Canonical External Benchmark':<35} | {'Frontier Leader':<25} | {'Authority'}"
    print(header)
    print("-" * 125)

    for domain_key, meta in DOMAIN_METADATA.items():
        domain_f = frontiers.get(domain_key, [])
        leader_id = domain_f[-1].model_id if domain_f else "None"
        print(f"{domain_key:<22} | {meta.canonical_benchmark[:35]:<35} | {leader_id[:25]:<25} | {meta.source_authority}")
    print("=" * 125)


def cmd_domain_frontier(args):
    service = get_daily_benchmark_service()
    ledger = service.load_latest_ledger() or ensure_daily_benchmark()
    all_models = list(ledger.models.values())
    domain_key = args.domain.lower()

    if domain_key not in DOMAIN_METADATA:
        print(f"[ERROR] Unknown domain '{domain_key}'. Choose from: {list(DOMAIN_METADATA.keys())}")
        return

    meta = DOMAIN_METADATA[domain_key]
    domain_frontier = compute_pareto_frontier(all_models, metric=domain_key)

    print(f"\nDomain: {meta.display_name} ({domain_key})")
    print(f"Benchmark Standard: {meta.canonical_benchmark} | Authority: {meta.source_authority}")
    print(f"Evaluates: {meta.evaluates}")
    print_domain_frontier_table(domain_frontier, domain=domain_key)


def cmd_route_task(args):
    from core.benchmarks.router import get_benchmark_router
    router = get_benchmark_router()
    decision = router.route_task(
        requirement=args.prompt,
        complexity=args.complexity,
        offline=args.offline,
        domain_override=args.domain,
    )

    print("=" * 85)
    print(" DARK FACTORY - TASK-ADAPTIVE BENCHMARK ROUTER DECISION")
    print("=" * 85)
    print(f" Requirement:          {decision.requirement}")
    print(f" Detected Domain:      {decision.detected_domain.upper()} (Confidence: {decision.confidence*100:.0f}%)")
    print(f" Intent Rationale:     {decision.intent_explanation}")
    print(f" Canonical Benchmark:  {decision.canonical_benchmark}")
    print(f" Optimal Frontier:     {decision.optimal_model_id} ({decision.optimal_model_name})")
    print(f" Domain Score:         {decision.domain_score:.1f} / 100")
    print(f" Estimated Task Cost:  ${decision.cost_per_task:.6f}")
    print("-" * 85)
    print(" Speculative Top-3 Candidates for this Domain:")
    for c in decision.speculative_candidates:
        pareto_mark = "[*] Pareto" if c.get("is_pareto") else "    Near"
        print(f"   - {c['role']:<20}: {c['model_id']:<30} (Score: {c['coding_score']:.1f}, ${c['cost_per_task']:.4f}/task, {c['output_speed_tps']:.0f} tps) {pareto_mark}")
    print("=" * 85)


def main():
    parser = argparse.ArgumentParser(description="Dark Factory Model Benchmark & Frontier CLI")
    subparsers = parser.add_subparsers(dest="command")

    run_p = subparsers.add_parser("run", help="Run or verify daily benchmark")
    run_p.add_argument("--force", action="store_true", help="Force benchmark update even if already run today")

    subparsers.add_parser("status", help="Show current benchmark status and last execution date")
    subparsers.add_parser("frontier", help="Show current Coding Pareto Efficiency Frontier")
    subparsers.add_parser("table", help="List all monitored models with costs and scores")
    subparsers.add_parser("proximity", help="Show Frontier Proximity Index (FPI), epsilon-gap and challenger status")

    subparsers.add_parser("domains", help="List all task-adaptive benchmark domains and canonical standards")

    dom_f_p = subparsers.add_parser("domain-frontier", help="Show Pareto frontier for a specific domain")
    dom_f_p.add_argument("--domain", required=True, choices=list(DOMAIN_METADATA.keys()))

    route_p = subparsers.add_parser("route-task", help="Analyze prompt/requirement and route to optimal benchmark & model")
    route_p.add_argument("--prompt", required=True, help="Development requirement or task prompt")
    route_p.add_argument("--complexity", default="high", choices=["low", "medium", "high", "critical"])
    route_p.add_argument("--domain", choices=list(DOMAIN_METADATA.keys()), help="Optional manual domain override")
    route_p.add_argument("--offline", action="store_true", help="Route exclusively to local Ollama cluster ($0 cost)")

    top3_p = subparsers.add_parser("top3", help="Show top-3 speculative candidate models for tier")
    top3_p.add_argument("--complexity", default="high", choices=["low", "medium", "high", "critical", "local_fast"])

    race_p = subparsers.add_parser("race", help="Execute speculative cascade race across top models")
    race_p.add_argument("--prompt", default="Implement verified sorting function with type hints", help="Task prompt")
    race_p.add_argument("--complexity", default="high", choices=["low", "medium", "high", "critical"])
    race_p.add_argument("--offline", action="store_true", help="Execute in local Ollama cluster ($0 cost)")

    tourn_p = subparsers.add_parser("tournament", help="Run empirical tournament and update Elo ratings")
    tourn_p.add_argument("--complexity", default="high", choices=["low", "medium", "high", "critical"])
    tourn_p.add_argument("--offline", action="store_true", help="Execute in local Ollama cluster")

    rec_p = subparsers.add_parser("recommend", help="Recommend optimal model for task")
    rec_p.add_argument("--task-type", default="coding", choices=["coding", "architecture", "research", "review", "scout"])
    rec_p.add_argument("--complexity", default="medium", choices=["low", "medium", "high", "critical"])
    rec_p.add_argument("--offline", action="store_true", help="Select only local Ollama models ($0 cost)")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "frontier":
        cmd_frontier(args)
    elif args.command == "table":
        cmd_table(args)
    elif args.command == "proximity":
        cmd_proximity(args)
    elif args.command == "domains":
        cmd_domains(args)
    elif args.command == "domain-frontier":
        cmd_domain_frontier(args)
    elif args.command == "route-task":
        cmd_route_task(args)
    elif args.command == "top3":
        cmd_top3(args)
    elif args.command == "race":
        cmd_race(args)
    elif args.command == "tournament":
        cmd_tournament(args)
    elif args.command == "recommend":
        cmd_recommend(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
