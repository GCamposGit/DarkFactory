"""
Dark Factory Speculative Model Racing & Empirical Tournament Engine.
Executes A/B speculative cascading across Top-3 models per tier,
enforces deterministic test gates, and maintains empirical real-world stats.
"""

import os
import json
import time
import uuid
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Callable, Tuple

from core.benchmarks.models import (
    LiveModelExecution,
    ModelBenchmarkEntry,
    RaceExecutionMode,
    SpeculativeCandidate,
    SpeculativeAttempt,
    SpeculativeRaceResult,
    EmpiricalModelStats,
    ModelTier,
    TaskComplexity,
)
from core.benchmarks.frontier import get_top_candidates_for_tier
from core.benchmarks.fetcher import ensure_daily_benchmark

logger = logging.getLogger("darkfac.benchmarks.racing")

DEFAULT_EMPIRICAL_LEDGER_PATH = Path(".factory/benchmarks/empirical_ledger.json")
K_FACTOR_ELO = 32.0
LiveExecutor = Callable[[str, str], LiveModelExecution]


class EmpiricalBenchmarkLedger:
    """Perpetual storage of real-world Dark Factory empirical performance metrics."""

    def __init__(self, file_path: Path = DEFAULT_EMPIRICAL_LEDGER_PATH):
        self.file_path = file_path
        self.stats: Dict[str, EmpiricalModelStats] = {}
        self.legacy_stats: Dict[str, EmpiricalModelStats] = {}
        self.history: List[SpeculativeRaceResult] = []
        self.live_history: List[SpeculativeRaceResult] = []
        self.simulated_history: List[SpeculativeRaceResult] = []
        self.validator_history: List[SpeculativeRaceResult] = []
        self._load()

    def _append_history(self, race: SpeculativeRaceResult) -> None:
        self.history.append(race)
        if race.execution_mode is RaceExecutionMode.LIVE:
            self.live_history.append(race)
        elif race.execution_mode is RaceExecutionMode.VALIDATOR_ONLY:
            self.validator_history.append(race)
        else:
            self.simulated_history.append(race)

    def _load(self) -> None:
        if not self.file_path.exists():
            return
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            for mid, sdata in data.get("legacy_models", {}).items():
                self.legacy_stats[mid] = EmpiricalModelStats.from_dict(sdata)

            stored_stats = {
                mid: EmpiricalModelStats.from_dict(sdata)
                for mid, sdata in data.get("models", {}).items()
            }
            if data.get("metrics_scope") == "live_model_inference_only":
                self.stats.update(stored_stats)
            else:
                # Pre-DF-05 ledgers mixed simulations with empirical metrics.
                self.legacy_stats.update(stored_stats)

            for rdata in data.get("recent_races", []):
                self._append_history(SpeculativeRaceResult.from_dict(rdata))
        except Exception as e:
            logger.warning(f"Failed to load empirical ledger from {self.file_path}: {e}")

    def save(self) -> None:
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "total_models_tracked": len(self.stats),
                "total_races_recorded": len(self.history),
                "metrics_scope": "live_model_inference_only",
                "models": {mid: s.to_dict() for mid, s in self.stats.items()},
                "legacy_models": {
                    mid: stat.to_dict() for mid, stat in self.legacy_stats.items()
                },
                "recent_races": [r.to_dict() for r in self.history[-50:]],
                "recent_races_by_mode": {
                    RaceExecutionMode.LIVE.value: [
                        race.to_dict() for race in self.live_history[-50:]
                    ],
                    RaceExecutionMode.VALIDATOR_ONLY.value: [
                        race.to_dict() for race in self.validator_history[-50:]
                    ],
                    RaceExecutionMode.SYNTHETIC_SIMULATION.value: [
                        race.to_dict() for race in self.simulated_history[-50:]
                    ],
                },
            }
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save empirical ledger to {self.file_path}: {e}")

    def get_or_create_stats(self, model_id: str) -> EmpiricalModelStats:
        if model_id not in self.stats:
            self.stats[model_id] = EmpiricalModelStats(
                model_id=model_id,
                elo_rating=1200.0,
                last_updated=datetime.now(timezone.utc).isoformat(),
            )
        return self.stats[model_id]

    def update_elo(self, winner_id: str, loser_ids: List[str]) -> None:
        """Updates Bradley-Terry / Elo ratings following a head-to-head or speculative match."""
        winner_stat = self.get_or_create_stats(winner_id)
        for loser_id in loser_ids:
            if loser_id == winner_id:
                continue
            loser_stat = self.get_or_create_stats(loser_id)

            # Expected score
            ea = 1.0 / (1.0 + 10.0 ** ((loser_stat.elo_rating - winner_stat.elo_rating) / 400.0))
            eb = 1.0 - ea

            # Score update (winner gets 1.0, loser gets 0.0)
            winner_stat.elo_rating += round(K_FACTOR_ELO * (1.0 - ea), 2)
            loser_stat.elo_rating += round(K_FACTOR_ELO * (0.0 - eb), 2)

    def record_race(self, race: SpeculativeRaceResult) -> None:
        """Records every race, while restricting empirical metrics to live attempts."""
        self._append_history(race)

        if race.execution_mode is not RaceExecutionMode.LIVE:
            self.save()
            return

        live_attempts = [
            attempt for attempt in race.attempts if attempt.model_inference_executed
        ]
        for attempt in live_attempts:
            stat = self.get_or_create_stats(attempt.model_id)
            stat.total_tasks_run += 1
            if attempt.verification_passed:
                stat.successful_tasks += 1
            stat.pass_rate_at_1 = round(
                stat.successful_tasks / stat.total_tasks_run,
                4,
            )
            if attempt.cost_usd is None:
                stat.unknown_cost_tasks += 1
            else:
                stat.total_cost_spent = round(
                    stat.total_cost_spent + attempt.cost_usd,
                    6,
                )
                stat.cost_observations += 1
            if attempt.duration_ms is not None:
                total_latency = (
                    stat.avg_latency_ms * stat.latency_observations
                    + attempt.duration_ms
                )
                stat.latency_observations += 1
                stat.avg_latency_ms = round(
                    total_latency / stat.latency_observations,
                    2,
                )
            if attempt.tokens_generated is not None:
                total_tokens = (
                    stat.avg_tokens_generated * stat.token_observations
                    + attempt.tokens_generated
                )
                stat.token_observations += 1
                stat.avg_tokens_generated = round(
                    total_tokens / stat.token_observations,
                    2,
                )
            stat.last_updated = datetime.now(timezone.utc).isoformat()

        executed_losers = [
            attempt.model_id
            for attempt in live_attempts
            if not attempt.verification_passed
            and attempt.model_id != race.winner_model_id
        ]
        if race.verification_passed and executed_losers:
            self.update_elo(race.winner_model_id, executed_losers)

        self.save()


class SpeculativeRacingEngine:
    """Executes speculative races and empirical tournaments."""

    def __init__(self, ledger: Optional[EmpiricalBenchmarkLedger] = None):
        self.ledger = ledger or EmpiricalBenchmarkLedger()

    def get_top_candidates(
        self,
        complexity: str = "high",
        offline: bool = False,
        k: int = 3,
    ) -> List[SpeculativeCandidate]:
        """Retrieves the top-3 models for a given capability tier."""
        daily_ledger = ensure_daily_benchmark()
        all_models = list(daily_ledger.models.values())

        tier_key = "local_fast" if offline else complexity.lower()
        return get_top_candidates_for_tier(all_models, tier=tier_key, k=k)

    def execute_speculative_race(
        self,
        task_id: str,
        task_prompt: str,
        complexity: str = "high",
        validator_func: Optional[Callable[[str], Tuple[bool, Dict[str, Any]]]] = None,
        simulate_drafter_success_rate: float = 0.75,
        offline: bool = False,
        live_executor: Optional[LiveExecutor] = None,
    ) -> SpeculativeRaceResult:
        """
        Executes an A/B speculative cascade race across the top-3 candidate models.
        
        Mechanism (FrugalGPT / Speculative Cascades):
        1. Fast drafter executes first.
        2. Output is evaluated against deterministic validator_func (syntax check / test runner).
        3. If drafter passes: Accepted immediately. Huge cost and latency savings!
        4. If drafter fails: Escalates to the balanced challenger or frontier arbiter.
        5. Verification and economics recorded to empirical ledger.
        """
        if validator_func is not None and live_executor is not None:
            raise ValueError("validator_func and live_executor are mutually exclusive")
        if not 0.0 <= simulate_drafter_success_rate <= 1.0:
            raise ValueError("simulate_drafter_success_rate must be between 0.0 and 1.0")

        candidates = self.get_top_candidates(complexity=complexity, offline=offline, k=3)
        if not candidates:
            raise ValueError(f"No candidate models found for complexity '{complexity}'")

        candidate_ids = [c.model_id for c in candidates]
        drafter = next((c for c in candidates if c.role == "fast_drafter"), candidates[0])
        arbiter = next((c for c in candidates if c.role == "frontier_arbiter"), candidates[-1])

        start_time = time.perf_counter()
        escalation_occurred = False
        winner = drafter
        verification_passed = True
        attempts: List[SpeculativeAttempt] = []
        if live_executor is not None:
            execution_mode = RaceExecutionMode.LIVE
        elif validator_func is not None:
            execution_mode = RaceExecutionMode.VALIDATOR_ONLY
        else:
            execution_mode = RaceExecutionMode.SYNTHETIC_SIMULATION
        verification_details: Dict[str, Any] = {
            "phase": "drafter_check",
            "execution_mode": execution_mode.value,
            "model_inference_executed": execution_mode is RaceExecutionMode.LIVE,
        }

        if live_executor is not None:
            def run_live(candidate: SpeculativeCandidate) -> SpeculativeAttempt:
                attempt_started = time.perf_counter()
                try:
                    execution = live_executor(candidate.model_id, task_prompt)
                    return SpeculativeAttempt(
                        model_id=candidate.model_id,
                        role=candidate.role,
                        execution_mode=RaceExecutionMode.LIVE,
                        model_inference_executed=True,
                        verification_passed=execution.verification_passed,
                        response=execution.response,
                        cost_usd=execution.cost_usd,
                        duration_ms=execution.duration_ms,
                        tokens_generated=execution.tokens_generated,
                        verification_details=execution.verification_details,
                    )
                except Exception as exc:
                    logger.warning(
                        "Live executor failed for model %s: %s",
                        candidate.model_id,
                        type(exc).__name__,
                    )
                    return SpeculativeAttempt(
                        model_id=candidate.model_id,
                        role=candidate.role,
                        execution_mode=RaceExecutionMode.LIVE,
                        model_inference_executed=True,
                        verification_passed=False,
                        duration_ms=round(
                            (time.perf_counter() - attempt_started) * 1000.0,
                            2,
                        ),
                        verification_details={
                            "execution_error": type(exc).__name__,
                        },
                    )

            drafter_attempt = run_live(drafter)
            attempts.append(drafter_attempt)
            verification_passed = drafter_attempt.verification_passed
            verification_details.update(drafter_attempt.verification_details)
            if not drafter_attempt.verification_passed:
                escalation_occurred = True
                winner = arbiter
                arbiter_attempt = run_live(arbiter)
                attempts.append(arbiter_attempt)
                verification_passed = arbiter_attempt.verification_passed
                verification_details["escalated_to"] = arbiter.model_id
                verification_details["arbiter_result"] = (
                    arbiter_attempt.verification_details
                )
        elif validator_func:
            # Execute actual deterministic validator on provided prompt/logic
            passed, details = validator_func(drafter.model_id)
            attempts.append(
                SpeculativeAttempt(
                    model_id=drafter.model_id,
                    role=drafter.role,
                    execution_mode=RaceExecutionMode.VALIDATOR_ONLY,
                    model_inference_executed=False,
                    verification_passed=passed,
                    verification_details=details,
                )
            )
            verification_details.update(details)
            if passed:
                winner = drafter
                verification_passed = True
            else:
                # Escalate to arbiter
                escalation_occurred = True
                winner = arbiter
                passed_arb, details_arb = validator_func(arbiter.model_id)
                attempts.append(
                    SpeculativeAttempt(
                        model_id=arbiter.model_id,
                        role=arbiter.role,
                        execution_mode=RaceExecutionMode.VALIDATOR_ONLY,
                        model_inference_executed=False,
                        verification_passed=passed_arb,
                        verification_details=details_arb,
                    )
                )
                verification_passed = passed_arb
                verification_details["escalated_to"] = arbiter.model_id
                verification_details["arbiter_result"] = details_arb
        else:
            # Deterministic simulation based on model capabilities
            # Drafter succeeds if simulated check passes
            if simulate_drafter_success_rate >= 0.50:
                winner = drafter
                verification_passed = True
                verification_details["status"] = "drafter_verified_one_shot"
                attempts.append(
                    SpeculativeAttempt(
                        model_id=drafter.model_id,
                        role=drafter.role,
                        execution_mode=RaceExecutionMode.SYNTHETIC_SIMULATION,
                        model_inference_executed=False,
                        verification_passed=True,
                        verification_details={
                            "simulated_success_rate": simulate_drafter_success_rate,
                        },
                    )
                )
            else:
                escalation_occurred = True
                winner = arbiter
                verification_passed = True
                verification_details["status"] = "escalated_to_arbiter_verified"
                attempts.extend(
                    [
                        SpeculativeAttempt(
                            model_id=drafter.model_id,
                            role=drafter.role,
                            execution_mode=RaceExecutionMode.SYNTHETIC_SIMULATION,
                            model_inference_executed=False,
                            verification_passed=False,
                            verification_details={
                                "simulated_success_rate": simulate_drafter_success_rate,
                            },
                        ),
                        SpeculativeAttempt(
                            model_id=arbiter.model_id,
                            role=arbiter.role,
                            execution_mode=RaceExecutionMode.SYNTHETIC_SIMULATION,
                            model_inference_executed=False,
                            verification_passed=True,
                        ),
                    ]
                )

        elapsed_ms = round((time.perf_counter() - start_time) * 1000.0, 2)

        if execution_mode is RaceExecutionMode.LIVE:
            total_cost = sum(
                attempt.cost_usd
                for attempt in attempts
                if attempt.cost_usd is not None
            )
            verification_details["cost_basis"] = "reported_live"
            verification_details["cost_complete"] = all(
                attempt.cost_usd is not None for attempt in attempts
            )
        else:
            total_cost = drafter.cost_per_task
            if escalation_occurred:
                total_cost += arbiter.cost_per_task
            verification_details["cost_basis"] = "benchmark_estimate"

        if execution_mode is RaceExecutionMode.LIVE:
            # A comparable live arbiter baseline is not available in a cascade.
            cost_saved = 0.0
            verification_details["cost_saved_basis"] = "live_baseline_unavailable"
        else:
            cost_if_always_arbiter = arbiter.cost_per_task
            cost_saved = round(max(0.0, cost_if_always_arbiter - total_cost), 6)
            verification_details["cost_saved_basis"] = "benchmark_estimate"

        race_result = SpeculativeRaceResult(
            race_id=f"race_{uuid.uuid4().hex[:8]}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            task_id=task_id,
            complexity=complexity,
            candidates=candidate_ids,
            winner_model_id=winner.model_id,
            winner_role=winner.role,
            escalation_occurred=escalation_occurred,
            duration_ms=elapsed_ms,
            total_cost_usd=round(total_cost, 6),
            cost_saved_usd=cost_saved,
            verification_passed=verification_passed,
            verification_details=verification_details,
            execution_mode=execution_mode,
            attempts=attempts,
        )

        self.ledger.record_race(race_result)
        return race_result

    def run_empirical_tournament(
        self,
        sample_tasks: Optional[List[Dict[str, Any]]] = None,
        complexity: str = "high",
        offline: bool = False,
    ) -> Dict[str, Any]:
        """
        Runs deterministic validator-only tournament fixtures.

        Results remain auditable, but do not update live Elo or Pass@1 metrics.
        """
        if not sample_tasks:
            # Standard Dark Factory micro-task fixtures for deterministic verification
            sample_tasks = [
                {
                    "task_id": "syntax_ast_transpile",
                    "prompt": "Verify strict typing and compile AST without syntax errors",
                    "validator": lambda mid: (True, {"error_count": 0, "syntax_valid": True}),
                },
                {
                    "task_id": "unit_test_harness_gate",
                    "prompt": "Run unit test suite with deterministic exit code",
                    "validator": lambda mid: (True, {"passed_tests": 5, "failed_tests": 0}),
                },
                {
                    "task_id": "boundary_edge_case_probe",
                    "prompt": "Evaluate edge cases for UTF-8 Windows stdout handling",
                    "validator": lambda mid: (True, {"encoding_safe": True, "clean_exit": True}),
                },
            ]

        results = []
        for task in sample_tasks:
            res = self.execute_speculative_race(
                task_id=task["task_id"],
                task_prompt=task["prompt"],
                complexity=complexity,
                validator_func=task.get("validator"),
                offline=offline,
            )
            results.append(res)

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "complexity": complexity,
            "execution_mode": RaceExecutionMode.VALIDATOR_ONLY.value,
            "tasks_evaluated": len(sample_tasks),
            "races": [r.to_dict() for r in results],
            "leaderboard": {
                mid: stat.to_dict()
                for mid, stat in sorted(
                    self.ledger.stats.items(),
                    key=lambda item: item[1].elo_rating,
                    reverse=True,
                )
            },
        }
