"""
Command Line Interface for DarkFac Continuous Learning & Self-Improvement Engine.
Allows agents and developers to audit, trigger, and inspect learning checkpoints.
"""

import sys
import argparse
import json
from pathlib import Path

# Ensure UTF-8 output on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Ensure project root in sys.path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.learning.tracker import ContinuousLearningTracker
from core.learning.models import PreferenceCategory, MistakeCategory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DarkFac Continuous Self-Improvement CLI",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status command
    subparsers.add_parser("status", help="Display current learning ledger metrics and active insights")

    # record-turn command
    p_turn = subparsers.add_parser("record-turn", help="Record an interaction turn and check for 2nd-prompt trigger")
    p_turn.add_argument("--prompt", required=True, help="Summary of user prompt")
    p_turn.add_argument("--intent", required=True, help="Perceived user intent")
    p_turn.add_argument("--followup", action="store_true", help="Flag if this prompt was a clarification or follow-up")
    p_turn.add_argument("--skill", default=None, help="Associated skill name")
    p_turn.add_argument("--gap", default=None, help="Identified context gap or reason for iteration")

    # record-pref command
    p_pref = subparsers.add_parser("record-pref", help="Register or reinforce a user preference/convention")
    p_pref.add_argument("--category", required=True, choices=[c.value for c in PreferenceCategory], help="Category")
    p_pref.add_argument("--rule", required=True, help="Formulated preference rule")
    p_pref.add_argument("--example", required=True, help="Context or concrete example")
    p_pref.add_argument("--confidence", type=float, default=1.0, help="Confidence level (0.0 - 1.0)")

    # rca command
    p_rca = subparsers.add_parser("rca", help="Record a 5-Whys Root Cause Analysis and preventative patch")
    p_rca.add_argument("--category", required=True, choices=[m.value for m in MistakeCategory], help="Mistake category")
    p_rca.add_argument("--symptom", required=True, help="Observable error symptom")
    p_rca.add_argument("--mechanism", required=True, help="Technical mechanism of failure")
    p_rca.add_argument("--root-cause", required=True, help="Underlying systemic root cause")
    p_rca.add_argument("--patch", required=True, help="Description of applied patch")
    p_rca.add_argument("--rule", required=True, help="Preventative inviolable rule")
    p_rca.add_argument("--test-file", default=None, help="Associated regression test file")

    # extrapolate command
    p_ext = subparsers.add_parser("extrapolate", help="Transfer lesson to analogous domains")
    p_ext.add_argument("--source", required=True, help="Source domain where lesson was learned")
    p_ext.add_argument("--targets", required=True, nargs="+", help="Target analogous domains")
    p_ext.add_argument("--lesson", required=True, help="Specific localized lesson")
    p_ext.add_argument("--principle", required=True, help="Generalized engineering principle")
    p_ext.add_argument("--actions", nargs="*", default=[], help="Specific applied actions")

    # prime command (System 1 fast context primer)
    p_prime = subparsers.add_parser("prime", help="Synthesize System 1 prompt priming context")
    p_prime.add_argument("--domain", default=None, help="Target domain filter")

    # prune command (System 2 anti-entropy policy debt pruning)
    subparsers.add_parser("prune", help="Prune policy debt and consolidate redundant rules")

    # benchmark command
    subparsers.add_parser("benchmark", help="Run empirical effectiveness benchmark suite")

    return parser


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = build_parser()
    args = parser.parse_args()
    tracker = ContinuousLearningTracker()

    if args.command == "status":
        summary = tracker.get_summary_report()
        print("==================================================")
        print("[LEARNING] DarkFac Continuous Self-Improvement Status")
        print("==================================================")
        print(f"Session ID:          {summary['session_id']}")
        print(f"Prompt Count:        {summary['prompt_count']}")
        print(f"Total Turns:         {summary['total_turns']}")
        print(f"Follow-up Turns:     {summary['followup_turns']}")
        print(f"One-Shot Rate:       {summary['one_shot_rate_pct']}%")
        print(f"Preferences Tracked: {summary['preferences_tracked']}")
        print(f"RCAs Resolved:       {summary['rcas_resolved']}")
        print(f"Analogous Transfers: {summary['analogous_transfers']}")
        print(f"Last Updated:        {summary['last_updated']}")

        if tracker.ledger.preferences:
            print("\n--- Active User Preferences ---")
            for p in tracker.ledger.preferences[-5:]:
                print(f"[{p.category.value.upper()}] {p.rule} (Reinforced: {p.times_reinforced}x)")

        if tracker.ledger.mistakes:
            print("\n--- Recent Mistake RCAs & Patches ---")
            for m in tracker.ledger.mistakes[-3:]:
                print(f"[{m.category.value.upper()}] {m.symptom} -> Root Cause: {m.root_cause} (Rule: {m.preventative_rule})")

        if tracker.ledger.transfers:
            print("\n--- Recent Analogous Transfers ---")
            for t in tracker.ledger.transfers[-3:]:
                print(f"[{t.source_domain} -> {', '.join(t.target_domains)}] {t.generalized_principle}")

    elif args.command == "record-turn":
        turn = tracker.record_turn(
            user_prompt_summary=args.prompt,
            perceived_intent=args.intent,
            was_followup=args.followup,
            one_shot_success=(not args.followup),
            missing_context_or_gap=args.gap,
            target_skill=args.skill,
        )
        is_trigger = tracker.is_checkpoint_turn(turn.turn_id, turn.was_followup)
        print(f"[TURN RECORDED] ID: {turn.turn_id} | Followup: {turn.was_followup}")
        if is_trigger:
            print("[CHECKPOINT TRIGGERED] Mandatory stop & self-improvement assessment required!")
        else:
            print("[CHECKPOINT IDLE] Next checkpoint scheduled for Turn", turn.turn_id + (1 if turn.turn_id % 2 != 0 else 2))

    elif args.command == "record-pref":
        pref = tracker.register_preference(
            category=PreferenceCategory(args.category),
            rule=args.rule,
            context_or_example=args.example,
            confidence=args.confidence,
        )
        print(f"[PREFERENCE RECORDED] ID: {pref.preference_id} | Rule: {pref.rule}")

    elif args.command == "rca":
        rca = tracker.record_mistake_rca(
            category=MistakeCategory(args.category),
            symptom=args.symptom,
            mechanism=args.mechanism,
            root_cause=args.root_cause,
            patch_description=args.patch,
            preventative_rule=args.rule,
            regression_test_file=args.test_file,
        )
        print(f"[RCA RESOLVED] ID: {rca.rca_id} | Root Cause: {rca.root_cause}")

    elif args.command == "extrapolate":
        transfer = tracker.extrapolate_analogy(
            source_domain=args.source,
            target_domains=args.targets,
            specific_lesson=args.lesson,
            generalized_principle=args.principle,
            applied_actions=args.actions,
        )
        print(f"[ANALOGY EXTRAPOLATED] ID: {transfer.transfer_id} | {args.source} -> {args.targets}")

    elif args.command == "prime":
        context = tracker.get_active_system1_context(domain=args.domain)
        print(context)

    elif args.command == "prune":
        report = tracker.prune_policy_debt()
        print("==================================================")
        print("[POLICY DEBT PRUNED] System 2 Anti-Entropy")
        print("==================================================")
        print(f"Redundant Rules Deactivated: {report.redundant_count}")
        print(f"Consolidated Rules:          {report.consolidated_count}")
        print(f"Active Clean Rules:          {report.active_clean_count}")
        if report.pruned_ids:
            print(f"Pruned IDs:                  {', '.join(report.pruned_ids)}")

    elif args.command == "benchmark":
        from core.learning.benchmark import SelfLearningBenchmark
        benchmark = SelfLearningBenchmark(verbose=True)
        summary = benchmark.run_all()
        sys.exit(0 if summary["all_passed"] else 1)


if __name__ == "__main__":
    main()

