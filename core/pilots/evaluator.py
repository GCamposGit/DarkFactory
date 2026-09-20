"""Reusable statistical evaluator for pilot experiments and shadow models (HF-23-02).

This module is 100% agnostic to the candidate under evaluation (works equally for
TypeSafe / Jev, local models, synthetic fakes, or future routers).
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Sequence

from core.pilots.contracts import (
    PilotObservation,
    PilotReport,
    PilotSpec,
    PilotVerdict,
)


def compute_wilson_interval(
    successes: int, total: int, z: float = 1.95996
) -> tuple[float, float]:
    """Computes the two-sided Wilson score interval for a binomial proportion."""
    if total <= 0:
        return (0.0, 0.0)
    p = successes / total
    denom = 1.0 + (z**2) / total
    center = (p + (z**2) / (2.0 * total)) / denom
    radicand = (p * (1.0 - p) / total) + (z**2) / (4.0 * total**2)
    margin = (z / denom) * math.sqrt(max(0.0, radicand))
    return (max(0.0, round(center - margin, 4)), min(1.0, round(center + margin, 4)))


def compute_paired_delta_ci(
    differences: Sequence[int], z: float = 1.95996
) -> tuple[float, float, float]:
    """Computes mean paired difference and asymptotic confidence interval.

    diff_i = cand_error_i - base_error_i in {-1, 0, +1}.
    Negative mean_d indicates candidate had fewer errors than baseline (better).
    """
    if not differences:
        return (0.0, 0.0, 0.0)
    n = len(differences)
    mean_d = sum(differences) / n
    if n <= 1:
        return (round(mean_d, 4), max(-1.0, round(mean_d - 0.5, 4)), min(1.0, round(mean_d + 0.5, 4)))
    variance = sum((d - mean_d) ** 2 for d in differences) / (n - 1)
    se = math.sqrt(variance / n)
    ci_lower = max(-1.0, round(mean_d - z * se, 4))
    ci_upper = min(1.0, round(mean_d + z * se, 4))
    return (round(mean_d, 4), ci_lower, ci_upper)


def compute_mcnemar_p_value(b: int, c: int) -> float | None:
    """Computes McNemar's test p-value with continuity correction."""
    total = b + c
    if total == 0:
        return 1.0
    diff = abs(b - c)
    if diff <= 1:
        return 1.0
    stat = ((diff - 1) ** 2) / total
    # Survival function approximation for chi2 with 1 df: erfc(sqrt(stat/2))
    p_val = math.erfc(math.sqrt(stat / 2.0))
    return round(min(1.0, max(0.0, p_val)), 4)


def compute_percentile(values: list[float], percentile: float) -> float:
    """Computes percentile from a list of float values."""
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(math.ceil((percentile / 100.0) * len(s))) - 1
    idx = max(0, min(idx, len(s) - 1))
    return round(s[idx], 2)


class PilotEvaluator:
    """Headless paired statistical evaluator for candidate vs baseline pilots."""

    def __init__(self, spec: PilotSpec) -> None:
        self.spec = spec

    def extract_predicted_stage(self, prediction: dict[str, Any] | None) -> str | None:
        """Extracts the predicted stage label from structured prediction dictionary."""
        if not prediction:
            return None
        if "choice" in prediction and isinstance(prediction["choice"], str):
            return prediction["choice"].strip().lower()
        if "stage" in prediction:
            stage_val = prediction["stage"]
            if isinstance(stage_val, dict) and "choice" in stage_val:
                return str(stage_val["choice"]).strip().lower()
            if isinstance(stage_val, str):
                return stage_val.strip().lower()
        return None

    def check_sentinel_risks(self, obs: PilotObservation) -> list[dict[str, Any]]:
        """Identifies any sentinel risk violations for a given observation."""
        violations: list[dict[str, Any]] = []

        # 1. Offline Leak: offline task must NOT have cloud candidate prediction or transmission
        is_offline_obs = (
            obs.stratum.lower() == "offline"
            or (obs.exclusion_reason is not None and "offline" in obs.exclusion_reason.lower())
        )
        if is_offline_obs:
            if obs.eligible:
                violations.append({
                    "risk": "offline_leak",
                    "case_id": obs.case_id,
                    "reason": "Observation in offline stratum was marked eligible for cloud dispatch",
                })
            if obs.candidate_prediction is not None:
                violations.append({
                    "risk": "offline_leak",
                    "case_id": obs.case_id,
                    "reason": "Candidate prediction was generated or transmitted for offline task",
                })

        # 2. Production Executor Mutation in shadow mode
        if obs.provenance and obs.provenance.get("executor_mutated"):
            violations.append({
                "risk": "production_executor_mutation",
                "case_id": obs.case_id,
                "reason": "Shadow candidate attempted to mutate production executor",
            })

        # 3. Bypass Grill G1 / Fake ready_for_spec
        if obs.provenance and obs.provenance.get("bypassed_grill"):
            violations.append({
                "risk": "fake_ready_for_spec",
                "case_id": obs.case_id,
                "reason": "Observation recorded bypass of G1 Grill gate",
            })

        # 4. Out of Catalog
        pred_label = self.extract_predicted_stage(obs.candidate_prediction)
        if pred_label and pred_label not in self.spec.labels:
            violations.append({
                "risk": "out_of_catalog",
                "case_id": obs.case_id,
                "reason": f"Predicted label '{pred_label}' is outside spec.labels catalog",
            })

        return violations

    def evaluate(self, observations: Sequence[PilotObservation]) -> PilotReport:
        """Runs the statistical evaluation over observations."""
        # 1. Deduplicate observations by case_id: replay of same case does NOT increase n
        deduped: dict[str, PilotObservation] = {}
        duplicates_collapsed = 0
        for obs in observations:
            if obs.case_id in deduped:
                duplicates_collapsed += 1
            deduped[obs.case_id] = obs

        unique_observations = list(deduped.values())
        total_cases = len(unique_observations)

        # 2. Collect sentinel violations across ALL observations (including ineligible)
        all_sentinel_violations: list[dict[str, Any]] = []
        for obs in unique_observations:
            v = self.check_sentinel_risks(obs)
            if v:
                all_sentinel_violations.extend(v)

        # 3. Partition eligible vs excluded vs unlabeled
        eligible_cases = 0
        excluded_cases = 0
        unlabeled_cases = 0

        eval_cases: list[PilotObservation] = []
        latencies: list[float] = []
        cost_total = 0.0

        for obs in unique_observations:
            if not obs.eligible:
                excluded_cases += 1
                continue
            eligible_cases += 1

            if obs.latency_ms is not None:
                latencies.append(obs.latency_ms)
            if obs.cost_usd is not None:
                cost_total += obs.cost_usd

            if obs.ground_truth is None:
                unlabeled_cases += 1
                continue

            eval_cases.append(obs)

        # 4. Compute error rates and paired delta
        candidate_errors = 0
        baseline_errors = 0
        paired_diffs: list[int] = []
        b_cand_better = 0  # candidate right, baseline wrong
        c_base_better = 0  # baseline right, candidate wrong

        candidate_confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        baseline_confusion: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        strata_stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "total": 0,
                "candidate_errors": 0,
                "baseline_errors": 0,
                "paired_delta": 0.0,
            }
        )

        for obs in eval_cases:
            truth = (obs.ground_truth or "").strip().lower()
            cand_label = self.extract_predicted_stage(obs.candidate_prediction)
            base_label = self.extract_predicted_stage(obs.baseline_prediction)

            cand_err = 1 if cand_label != truth else 0
            base_err = 1 if base_label != truth else 0

            candidate_errors += cand_err
            baseline_errors += base_err

            diff = cand_err - base_err
            paired_diffs.append(diff)

            if diff == -1:
                b_cand_better += 1
            elif diff == 1:
                c_base_better += 1

            candidate_confusion[truth][cand_label or "none"] += 1
            baseline_confusion[truth][base_label or "none"] += 1

            # Stratum tracking
            strat = obs.stratum
            st = strata_stats[strat]
            st["total"] += 1
            st["candidate_errors"] += cand_err
            st["baseline_errors"] += base_err

        # Finalize strata breakdown
        for strat, st in strata_stats.items():
            tot = st["total"]
            if tot > 0:
                c_rate = st["candidate_errors"] / tot
                b_rate = st["baseline_errors"] / tot
                st["paired_delta"] = round(c_rate - b_rate, 4)

        n_eval = len(eval_cases)
        if n_eval > 0:
            cand_error_rate = round(candidate_errors / n_eval, 4)
            base_error_rate = round(baseline_errors / n_eval, 4)
            mean_delta, ci_lower, ci_upper = compute_paired_delta_ci(paired_diffs)
            p_val = compute_mcnemar_p_value(b_cand_better, c_base_better)
        else:
            cand_error_rate = 0.0
            base_error_rate = 0.0
            mean_delta = 0.0
            ci_lower = 0.0
            ci_upper = 0.0
            p_val = 1.0

        p50_lat = compute_percentile(latencies, 50.0)
        p95_lat = compute_percentile(latencies, 95.0)

        # 5. Limitations
        limitations: list[str] = []
        if duplicates_collapsed > 0:
            limitations.append(f"{duplicates_collapsed} duplicate replay runs collapsed into unique case IDs")
        if n_eval < self.spec.sample_size_target:
            limitations.append(f"Evaluated labeled sample size ({n_eval}) is below target ({self.spec.sample_size_target})")
        if unlabeled_cases > 0:
            limitations.append(f"{unlabeled_cases} eligible cases lack independent ground truth labels")

        # 6. Verdict determination
        verdict: PilotVerdict
        verdict_reason: str

        if all_sentinel_violations:
            verdict = "harmful"
            verdict_reason = (
                f"Evaluation detected {len(all_sentinel_violations)} sentinel risk violations: "
                + ", ".join(f"{v['risk']} ({v['case_id']})" for v in all_sentinel_violations[:3])
            )
        elif n_eval < self.spec.min_sample_size:
            verdict = "inconclusive"
            verdict_reason = (
                f"Sample size {n_eval} is insufficient (minimum required is {self.spec.min_sample_size})"
            )
        elif mean_delta > 0.05 and ci_lower > 0.0:
            verdict = "harmful"
            verdict_reason = (
                f"Candidate error rate is significantly higher than baseline (delta={mean_delta:+.4f}, "
                f"95% CI [{ci_lower:+.4f}, {ci_upper:+.4f}])"
            )
        elif mean_delta < -0.05 and ci_upper < 0.0:
            verdict = "promising"
            verdict_reason = (
                f"Candidate demonstrated statistically significant error reduction (delta={mean_delta:+.4f}, "
                f"95% CI [{ci_lower:+.4f}, {ci_upper:+.4f}])"
            )
        else:
            verdict = "inconclusive"
            verdict_reason = (
                f"No statistically significant difference found (delta={mean_delta:+.4f}, "
                f"95% CI [{ci_lower:+.4f}, {ci_upper:+.4f}])"
            )

        # Convert defaultdicts to regular dicts for JSON serialization
        c_conf_dict = {k: dict(v) for k, v in candidate_confusion.items()}
        b_conf_dict = {k: dict(v) for k, v in baseline_confusion.items()}
        strata_dict = {k: dict(v) for k, v in strata_stats.items()}

        return PilotReport(
            spec_id=self.spec.spec_id,
            total_cases=total_cases,
            eligible_cases=eligible_cases,
            excluded_cases=excluded_cases,
            unlabeled_cases=unlabeled_cases,
            candidate_error_rate=cand_error_rate,
            baseline_error_rate=base_error_rate,
            paired_delta=mean_delta,
            ci_lower=ci_lower,
            ci_upper=ci_upper,
            confidence_level=1.0 - self.spec.alpha,
            p_value=p_val,
            candidate_confusion=c_conf_dict,
            baseline_confusion=b_conf_dict,
            strata_breakdown=strata_dict,
            cost_total_usd=round(cost_total, 6),
            latency_p50_ms=p50_lat,
            latency_p95_ms=p95_lat,
            sentinel_violations=all_sentinel_violations,
            limitations=limitations,
            verdict=verdict,
            verdict_reason=verdict_reason,
        )
