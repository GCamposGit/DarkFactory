"""Observed review measurements. Historical latencies are data, never pass/fail gates."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable, Optional

from pydantic import BaseModel, ConfigDict, Field

from core.paths import state_root


class ReviewObservation(BaseModel):
    """One measured or intake-reported review call. Latency is recorded, not scored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    model: Optional[str] = None
    latency_ms: int = Field(ge=0)
    schema_valid: bool
    real_delivery: bool
    delivery_nonempty: bool
    reviewer_family: Optional[str] = None
    implementer_family: Optional[str] = None
    source: str = Field(min_length=1)
    note: Optional[str] = None


def observation_label(harness: str, model: Optional[str], expected_model: Optional[str] = None) -> str:
    """Stable label for a measured call. Sonnet and Grok keep their names; everyone else uses the harness."""
    text = " ".join(part for part in (model, expected_model) if part).lower()
    if "sonnet" in text:
        return "sonnet"
    if harness.strip().lower() == "grok":
        return "grok"
    return harness.strip().lower() or "unknown"


def historical_intake_observations() -> list[ReviewObservation]:
    """Intake claims from 06/11/2026, stored as observations.

    The Grok figure is about 10 minutes without a valid schema. The Sonnet figure is the
    reported 9–13 second band after reasoning was corrected. Neither number is a threshold.
    """
    sonnet = dict(
        label="sonnet",
        harness="openrouter",
        model="sonnet",
        schema_valid=True,
        real_delivery=True,
        delivery_nonempty=True,
        reviewer_family="anthropic",
        implementer_family="openai",
        source="intake-2026-11-06",
    )
    return [
        ReviewObservation(
            label="grok",
            harness="grok",
            model="review.grok",
            latency_ms=600_000,
            schema_valid=False,
            real_delivery=False,
            delivery_nonempty=False,
            source="intake-2026-11-06",
            note="reported review of about 10 minutes without a valid schema",
        ),
        ReviewObservation(
            latency_ms=9_000,
            note="lower edge of the reported 9-13 second band",
            **sonnet,
        ),
        ReviewObservation(
            latency_ms=13_000,
            note="upper edge of the reported 9-13 second band",
            **sonnet,
        ),
    ]


def observation_succeeded(item: ReviewObservation) -> bool:
    """A review delivery is real only with a valid schema, independent family and non-empty output.

    Latency is intentionally unused: a slow valid review and a fast invalid one are judged
    on those facts, not on a historical band.
    """
    if not item.schema_valid or not item.real_delivery or not item.delivery_nonempty:
        return False
    if not item.reviewer_family or not item.implementer_family:
        return False
    return item.reviewer_family != item.implementer_family


def benchmark_has_successful_delivery(payload: dict) -> bool:
    """True only when at least one stored observation is a real successful delivery."""
    rows = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        return False
    return any(observation_succeeded(ReviewObservation.model_validate(row)) for row in rows)


def benchmark_log_path(root: Optional[Path] = None) -> Path:
    """Append-only log of measured review calls under the state root."""
    base = root if root is not None else state_root()
    return base / "runs" / "review_benchmark.jsonl"


def append_review_observation(
    observation: ReviewObservation,
    *,
    runs_dir: Optional[Path] = None,
) -> None:
    """Append one observation. Does not compare it with any historical latency."""
    target = (runs_dir / "review_benchmark.jsonl") if runs_dir is not None else benchmark_log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(observation.model_dump_json() + "\n")


def record_review_benchmark(
    observations: Iterable[ReviewObservation],
    path: Path,
) -> dict:
    """Persist observations. ``latency_gates`` stays empty on purpose."""
    payload = {
        "schema_version": 1,
        "observations": [item.model_dump(mode="json") for item in observations],
        "latency_gates": [],
    }
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, destination)
    return payload


def observe_measured_review(
    *,
    harness: str,
    model: Optional[str],
    expected_model: Optional[str],
    latency_ms: int,
    schema_valid: bool,
    real_delivery: bool,
    delivery_nonempty: bool,
    implementing_harness: Optional[str],
    runs_dir: Optional[Path] = None,
) -> None:
    """Record one live review call, including family when the registry can resolve it."""
    implementer_family: Optional[str] = None
    reviewer_family: Optional[str] = None
    try:
        from core.line.recovery_receipt import route_family

        if implementing_harness:
            implementer_family = route_family(implementing_harness, None)
        reviewer_family = route_family(harness, model)
    except ValueError:
        pass
    append_review_observation(
        ReviewObservation(
            label=observation_label(harness, model, expected_model),
            harness=harness,
            model=model,
            latency_ms=max(0, latency_ms),
            schema_valid=schema_valid,
            real_delivery=real_delivery,
            delivery_nonempty=delivery_nonempty,
            reviewer_family=reviewer_family,
            implementer_family=implementer_family,
            source="measured",
        ),
        runs_dir=runs_dir,
    )
