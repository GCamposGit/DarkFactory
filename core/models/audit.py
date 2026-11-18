"""Fail-closed audit receipts for effective model routes and execution outcomes."""

from __future__ import annotations

import json
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from core.line.routing import harness_cli_pin, load_routing_config
from core.models.registry import ModelEntry, get_registry, resolve_known

logger = logging.getLogger(__name__)


class ExecutionSample(BaseModel):
    """Executor facts for one completed call, with provider identity kept distinct from the route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str = Field(min_length=1)
    ticket_id: str = Field(min_length=1)
    stage: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    provider: Optional[str] = None
    model_source: Optional[str] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    execution_parameters: dict[str, Any] = Field(default_factory=dict)
    reported_model: Optional[str] = None
    complexity: Optional[str] = None
    implementing_harness: Optional[str] = None
    quota_remaining_percent: Optional[float] = Field(default=None, ge=0, le=100)
    latency_ms: int = Field(ge=0)
    timeout_sec: float = Field(gt=0)
    timed_out: bool = False
    cost_usd: float = Field(default=0, ge=0)
    spent_usd: float = Field(default=0, ge=0)
    reported_context_tokens: Optional[int] = Field(default=None, gt=0)
    context_sha256: Optional[str] = None
    context_bytes: Optional[int] = Field(default=None, ge=0)
    reported_effort: Optional[str] = None
    expected_model: Optional[str] = None
    expected_effort: Optional[str] = None
    candidate_digest: Optional[str] = None
    schema_valid: bool
    real_delivery: bool
    error_kind: Optional[str] = None
    finish_reason: Optional[str] = None


class AuditReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 2
    audited_at: str
    passed: bool
    samples: list[dict[str, Any]]
    configured_models: dict[str, str]
    divergences: list[str]
    violations: list[str]


def record_execution_sample(
    sample: ExecutionSample,
    *,
    ticket_id: str,
    runs_dir: Optional[Path] = None,
) -> None:
    """Append one real `ExecutionSample` for `ticket_id` to its model-route audit ledger (USR-471 T1).

    Target: `STATE_ROOT/runs/model_audit/<ticket_id>.jsonl`. A single `write()` call per line in
    append mode means concurrent writers (build/review stages, different hosts) never interleave a
    partial line. `ExecutionSample` carries only provider-reported facts -- no prompt text, token
    contents or credential ever reaches this ledger.

    This is observation only: callers must catch any exception raised here themselves (a narrow
    `except Exception`) and log it, never let a write failure change a stage's own outcome.
    """
    if not ticket_id or not ticket_id.strip():
        raise ValueError("ticket_id e obrigatorio para registrar amostra de execucao")
    if sample.ticket_id != ticket_id:
        raise ValueError("sample.ticket_id deve corresponder ao ticket_id do ledger")
    from core.runtime_artifacts import runtime_runs_dir

    target_dir = runs_dir or (runtime_runs_dir() / "model_audit")
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / f"{ticket_id}.jsonl"
    line = sample.model_dump_json() + "\n"
    with target_file.open("a", encoding="utf-8") as handle:
        handle.write(line)


def reported_context_tokens(usage: Optional[dict[str, Any]]) -> Optional[int]:
    """Best-effort total context size (prompt + completion) from a provider's own `usage` dict.

    Shared by `stage_build` and `stage_review` so both report the same field under the same
    convention the provider adapters already use elsewhere (`core.line.agent_cli`): accepts either
    OpenAI-style (`prompt_tokens`/`completion_tokens`) or generic (`input_tokens`/`output_tokens`)
    keys. `None` when the provider reported nothing usable -- never an invented value.
    """
    if not usage:
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and total > 0:
        return total
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens")
    completion = usage.get("completion_tokens") or usage.get("output_tokens")
    if isinstance(prompt, int) and isinstance(completion, int):
        combined = prompt + completion
        if combined > 0:
            return combined
    if isinstance(prompt, int) and prompt > 0:
        return prompt
    return None


def _model_for(model_id: str) -> ModelEntry:
    registry = get_registry()
    resolved = resolve_known(model_id, registry)
    for entry in registry.models:
        if entry.id == resolved:
            return entry
    raise KeyError(f"model entry missing for {resolved!r}")


def _route_model_for(
    sample: ExecutionSample,
    config: Any,
    registry: Any,
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(route_model, violation)`` before the harness pin is applied."""
    stage_cfg = config.stages.get(sample.stage)
    if stage_cfg is None:
        return None, f"missing_configured_route:{sample.stage}.{sample.harness}"
    route_model: Optional[str] = None
    if sample.harness == "openrouter" and stage_cfg.openrouter_ok:
        route_model = stage_cfg.openrouter_model
    elif isinstance(stage_cfg.cascade, list):
        if not any(harness == sample.harness for harness, _ in stage_cfg.cascade):
            return None, f"missing_configured_route:{sample.stage}.{sample.harness}"
        route_model = next(model for harness, model in stage_cfg.cascade if harness == sample.harness)
    elif sample.stage == "review" and stage_cfg.cascade == "other_family_than_development":
        if sample.harness not in {"claude", "codex", "grok", "antigravity", "openrouter"}:
            return None, f"missing_configured_route:{sample.stage}.{sample.harness}"
    else:
        return None, f"missing_configured_route:{sample.stage}.{sample.harness}"
    if route_model is None and sample.harness != "openrouter":
        route_model = registry.roles.get(f"{sample.stage}.{sample.harness}")
    if sample.stage == "review" and sample.harness == "openrouter" and sample.implementing_harness:
        route_model = _independent_openrouter_route(sample, route_model, registry)
    return route_model, None


def _independent_openrouter_route(
    sample: ExecutionSample,
    route_model: Optional[str],
    registry: Any,
) -> Optional[str]:
    """Use the distillation model when the configured OpenRouter reviewer shares the implementer's family."""
    implementation_model = registry.roles.get(f"development.{sample.implementing_harness}")
    if not implementation_model or not route_model:
        return route_model
    implementation_family = _model_for(resolve_known(implementation_model, registry)).family
    requested = (sample.execution_parameters or {}).get("requested_model") or sample.expected_model
    if isinstance(requested, str) and requested in ("review.openrouter", "distill.openrouter"):
        explicit = registry.roles.get(requested)
        if explicit:
            explicit_id = resolve_known(explicit, registry)
            if _model_for(explicit_id).family != implementation_family:
                return explicit_id
    candidate_family = _model_for(resolve_known(route_model, registry)).family
    if candidate_family != implementation_family:
        return route_model
    fallback = registry.roles.get("distill.openrouter")
    if not fallback:
        return route_model
    fallback_id = resolve_known(fallback, registry)
    if _model_for(fallback_id).family == implementation_family:
        return route_model
    return fallback_id


def _binding_violations(
    sample: ExecutionSample,
    registry: Any,
    effective_id: str,
    effort: Optional[str],
) -> tuple[list[str], Optional[str]]:
    """Violations for the ticket/candidate/context/expected-route binding. Latency is not a gate."""
    found: list[str] = []
    expected_id: Optional[str] = None
    expected_provider = {
        "claude": "anthropic",
        "codex": "openai",
        "grok": "xai",
        "antigravity": "google",
        "openrouter": "openrouter",
    }.get(sample.harness)
    if not sample.provider:
        found.append(f"missing_provider:{sample.execution_id}")
    elif expected_provider and sample.provider.lower() != expected_provider:
        found.append(
            f"provider_mismatch:{sample.execution_id}:expected={expected_provider}:reported={sample.provider}"
        )
    if not sample.model_source:
        found.append(f"missing_model_source:{sample.execution_id}")
    if sample.started_at is None or sample.finished_at is None:
        found.append(f"missing_execution_timestamps:{sample.execution_id}")
    elif (
        sample.started_at.tzinfo is None
        or sample.finished_at.tzinfo is None
        or sample.finished_at < sample.started_at
    ):
        found.append(f"invalid_execution_timestamps:{sample.execution_id}")
    if not sample.execution_parameters:
        found.append(f"missing_execution_parameters:{sample.execution_id}")
    elif not isinstance(sample.execution_parameters.get("reasoning"), dict):
        found.append(f"missing_reasoning_parameters:{sample.execution_id}")
    requested_model = sample.execution_parameters.get("requested_model")
    if requested_model:
        try:
            requested_id = resolve_known(str(requested_model), registry)
        except KeyError:
            found.append(f"unresolvable_requested_model:{sample.execution_id}:{requested_model}")
        else:
            if requested_id != effective_id:
                found.append(
                    f"requested_model_mismatch:{sample.execution_id}:configured={effective_id}:requested={requested_id}"
                )
    else:
        found.append(f"missing_requested_model:{sample.execution_id}")
    if not (sample.candidate_digest or "").strip():
        found.append(f"missing_candidate_digest:{sample.execution_id}")
    if not (sample.context_sha256 or "").strip() or sample.context_bytes is None:
        found.append(f"missing_context_binding:{sample.execution_id}")
    raw_expected = (sample.expected_model or "").strip()
    if not raw_expected:
        found.append(f"missing_expected_model:{sample.execution_id}")
    else:
        try:
            expected_id = resolve_known(raw_expected, registry)
        except KeyError as exc:
            found.append(f"unresolvable_expected_model:{sample.execution_id}:{exc}")
        else:
            if expected_id != effective_id:
                found.append(
                    f"expected_model_mismatch:{sample.execution_id}:expected={expected_id}:configured={effective_id}"
                )
    if effort is not None:
        raw_effort = (sample.expected_effort or "").strip()
        if not raw_effort:
            found.append(f"missing_expected_effort:{sample.execution_id}")
        elif raw_effort.lower() != effort.strip().lower():
            found.append(
                f"expected_effort_mismatch:{sample.execution_id}:configured={effort}:expected={sample.expected_effort}"
            )
    return found, expected_id


def _outcome_violations(
    sample: ExecutionSample,
    *,
    config: Any,
    registry: Any,
    effective: ModelEntry,
    reported: ModelEntry,
    effort: Optional[str],
) -> list[str]:
    """Reject model/effort divergence, same-family review, ceilings, timeout, schema and empty delivery."""
    found: list[str] = []
    execution_id = sample.execution_id
    if effort is not None and (sample.reported_effort or "").strip().lower() != effort.strip().lower():
        found.append(f"reported_effort_mismatch:{execution_id}:configured={effort}:reported={sample.reported_effort}")
    if sample.harness != "openrouter":
        floor = registry.quota_floor_percent
        if sample.quota_remaining_percent is None or sample.quota_remaining_percent <= floor:
            found.append(f"quota_floor:{execution_id}:remaining={sample.quota_remaining_percent}")
    if sample.stage == "review":
        if not sample.implementing_harness:
            found.append(f"missing_implementing_harness:{execution_id}")
        else:
            impl_route = f"development.{sample.implementing_harness}"
            impl_model = registry.roles.get(impl_route)
            if not impl_model:
                found.append(f"missing_implementer_role:{execution_id}:{impl_route}")
            elif _model_for(impl_model).family == reported.family:
                found.append(f"same_family_review:{execution_id}")
    if sample.timed_out or sample.latency_ms > sample.timeout_sec * 1000:
        found.append(f"timeout_exceeded:{execution_id}")
    if sample.cost_usd > config.run_caps.openrouter_usd:
        found.append(f"per_run_cost_cap:{execution_id}")
    if sample.spent_usd > config.run_caps.openrouter_monthly_cap_usd:
        found.append(f"monthly_cost_cap:{execution_id}")
    if not sample.schema_valid:
        found.append(f"invalid_delivery_schema:{execution_id}")
    if not sample.real_delivery:
        found.append(f"no_real_delivery:{execution_id}")
    if sample.reported_context_tokens is not None and effective.context_tokens is not None:
        if sample.reported_context_tokens > effective.context_tokens:
            found.append(f"context_exceeds_configured:{execution_id}")
    return found


def _judge_sample(
    raw: ExecutionSample | dict[str, Any],
    *,
    config: Any,
    registry: Any,
) -> tuple[Optional[dict[str, Any]], list[str], list[str]]:
    """Judge one sample. ``None`` means the row could not be bound to a route at all."""
    try:
        sample = raw if isinstance(raw, ExecutionSample) else ExecutionSample.model_validate(raw)
    except ValidationError as exc:
        return None, [f"invalid_execution_sample:{exc.errors()[0]['loc']}"], []

    route_model, route_violation = _route_model_for(sample, config, registry)
    if route_violation:
        return None, [route_violation], []
    configured, effort = harness_cli_pin(
        sample.harness, route_model, stage=sample.stage, config=config, complexity=sample.complexity,
    )
    if not configured:
        return None, [f"missing_configured_route:{sample.stage}.{sample.harness}"], []
    try:
        effective_id = resolve_known(configured, registry)
        if not sample.reported_model:
            return None, [f"missing_reported_model:{sample.execution_id}"], []
        reported_id = resolve_known(sample.reported_model, registry)
        effective = _model_for(effective_id)
        reported = _model_for(reported_id)
    except KeyError as exc:
        return None, [f"unresolvable_model:{sample.execution_id}:{exc}"], []

    violations = _outcome_violations(
        sample, config=config, registry=registry, effective=effective, reported=reported, effort=effort,
    )
    binding, expected_id = _binding_violations(sample, registry, effective_id, effort)
    violations.extend(binding)
    divergences: list[str] = []
    if effective_id != reported_id:
        divergences.append(
            f"reported_model_mismatch:{sample.execution_id}:configured={effective_id}:reported={reported_id}"
        )
        violations.append(f"reported_model_mismatch:{sample.execution_id}")
    return {
        "execution_id": sample.execution_id,
        "ticket_id": sample.ticket_id,
        "stage": sample.stage,
        "harness": sample.harness,
        "route": f"{sample.stage}.{sample.harness}",
        "provider": sample.provider,
        "model_source": sample.model_source,
        "started_at": sample.started_at.isoformat() if sample.started_at else None,
        "finished_at": sample.finished_at.isoformat() if sample.finished_at else None,
        "execution_parameters": sample.execution_parameters,
        "requested_model": sample.execution_parameters.get("requested_model"),
        "complexity": sample.complexity or "medium",
        "effective_model": effective_id,
        "reported_model": reported_id,
        "expected_model": expected_id or sample.expected_model,
        "model_family": effective.family,
        "context_tokens": effective.context_tokens,
        "reported_context_tokens": sample.reported_context_tokens,
        "context_sha256": sample.context_sha256,
        "context_bytes": sample.context_bytes,
        "candidate_digest": sample.candidate_digest,
        "effective_effort": effort,
        "expected_effort": sample.expected_effort,
        "reported_effort": sample.reported_effort,
        "latency_ms": sample.latency_ms,
        "timeout_sec": sample.timeout_sec,
        "timed_out": sample.timed_out,
        "quota_remaining_percent": sample.quota_remaining_percent,
        "cost_usd": sample.cost_usd,
        "spent_usd": sample.spent_usd,
        "schema_valid": sample.schema_valid,
        "real_delivery": sample.real_delivery,
        "delivery_result": (
            "delivered" if sample.real_delivery and sample.schema_valid
            else "schema_invalid" if sample.real_delivery
            else "no_delivery"
        ),
        "error_kind": sample.error_kind,
        "finish_reason": sample.finish_reason,
    }, violations, divergences


def audit_executions(
    samples: Iterable[ExecutionSample | dict[str, Any]],
    *,
    receipt_path: Optional[Path] = None,
) -> AuditReport:
    """Resolve and compare execution reports against configured stage/harness routes.

    Missing/unknown route facts are violations. The receipt is written even on failure so
    rejected samples retain their evidence for diagnosis. Writes use the shared atomic sidecar
    writer so a reader never observes a partially written receipt (USR-471 T6).
    """
    registry = get_registry()
    config = load_routing_config()
    configured_models = {
        role: resolved
        for role, raw_model in registry.roles.items()
        if role.startswith(("planning.", "development.", "review."))
        for resolved in [resolve_known(raw_model, registry)]
    }
    recorded: list[dict[str, Any]] = []
    divergences: list[str] = []
    violations: list[str] = []
    seen_models: dict[tuple[str, str], str] = {}

    for raw in samples:
        judged, sample_violations, sample_divergences = _judge_sample(raw, config=config, registry=registry)
        violations.extend(sample_violations)
        divergences.extend(sample_divergences)
        if judged is None:
            continue
        key = (judged["stage"], judged["harness"])
        previous = seen_models.get(key)
        if previous and previous != judged["effective_model"]:
            divergences.append(
                f"route_changed_during_audit:{judged['stage']}.{judged['harness']}:{previous}->{judged['effective_model']}"
            )
        seen_models[key] = judged["effective_model"]
        recorded.append(judged)

    if not recorded and not violations:
        violations.append("no_execution_samples")

    # Flag that one harness runs different configured models across audited stages.
    by_harness: dict[str, dict[str, str]] = {}
    for item in recorded:
        by_harness.setdefault(item["harness"], {})[item["stage"]] = item["effective_model"]
    for harness, routes in by_harness.items():
        if len(set(routes.values())) > 1:
            divergence = f"stage_model_divergence:{harness}:" + ",".join(
                f"{stage}={model}" for stage, model in sorted(routes.items())
            )
            if divergence not in divergences:
                divergences.append(divergence)

    report = AuditReport(
        audited_at=datetime.now(timezone.utc).isoformat(),
        passed=not violations,
        samples=recorded,
        configured_models=configured_models,
        divergences=divergences,
        violations=violations,
    )
    if receipt_path is not None:
        from core.runtime_artifacts import atomic_write_json

        atomic_write_json(Path(receipt_path), report.model_dump(mode="json"))
    return report


def audit_file(input_path: Path, receipt_path: Path) -> AuditReport:
    """Read provider execution samples from JSON and persist the audit receipt."""
    payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    samples = payload.get("samples") if isinstance(payload, dict) else payload
    if not isinstance(samples, list):
        raise ValueError("input must be a JSON array or an object with a samples array")
    return audit_executions(samples, receipt_path=receipt_path)


def receipt_path(root: Optional[Path] = None) -> Path:
    """Where the model-route audit receipt is persisted, under the shared state root."""
    from core.runtime_artifacts import runtime_sidecar_path

    return runtime_sidecar_path(
        "model_audit_receipt.json",
        state_root=root,
    )


def _recorded_samples(runs_dir: Path, ticket_id: Optional[str]) -> list[dict[str, Any]]:
    """Read the `ExecutionSample` ledgers written by `record_execution_sample` (USR-471 T1).

    One ticket's `<ticket_id>.jsonl` when `ticket_id` is given, otherwise every ledger under
    `runs_dir`, oldest file name first. A missing directory or missing file yields no samples,
    never an error -- the ledger only exists once a real executor call has been recorded.
    """
    if ticket_id:
        files = [runs_dir / f"{ticket_id}.jsonl"]
    elif runs_dir.is_dir():
        files = sorted(runs_dir.glob("*.jsonl"))
    else:
        files = []
    samples: list[dict[str, Any]] = []
    for file in files:
        if not file.is_file():
            continue
        for line in file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    return samples


def audit_recorded(
    ticket_id: Optional[str] = None,
    *,
    runs_dir: Optional[Path] = None,
    receipt_target: Optional[Path] = None,
) -> AuditReport:
    """Audit the real executor samples recorded under `runs_dir` and persist the receipt (USR-471 T2).

    The single entry point the CLI (`--from-runs --latest`) and the Hub endpoint both call --
    neither reads the persisted receipt for a decision; both recompute it here, every time, the
    same contract `core.line.fleet_policy.effective_policy_snapshot` already uses for the fleet
    policy snapshot. No recorded samples is a failing receipt (`no_execution_samples`), never a pass.
    """
    from core.runtime_artifacts import runtime_runs_dir

    source_dir = runs_dir if runs_dir is not None else (runtime_runs_dir() / "model_audit")
    samples = _recorded_samples(source_dir, ticket_id)
    target = receipt_target if receipt_target is not None else receipt_path()
    return audit_executions(samples, receipt_path=target)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Audit effective model routes and execution receipts.")
    parser.add_argument("--input", type=Path, default=None, help="JSON array or object with samples")
    parser.add_argument("--receipt", type=Path, default=None, help="Path for the audit JSON receipt")
    parser.add_argument(
        "--from-runs", action="store_true",
        help="Audit real executor samples from STATE_ROOT/runs/model_audit",
    )
    parser.add_argument("--ticket", type=str, default=None, help="With --from-runs, limit to this ticket's ledger")
    parser.add_argument(
        "--latest", action="store_true",
        help="Required with --from-runs: recompute and print the current receipt",
    )
    args = parser.parse_args(argv)
    if args.from_runs:
        if not args.latest:
            parser.error("--from-runs requires --latest")
        report = audit_recorded(args.ticket)
    else:
        if args.input is None or args.receipt is None:
            parser.error("--input and --receipt are required unless --from-runs is set")
        report = audit_file(args.input, args.receipt)
    print(report.model_dump_json(indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
