"""Executable acceptance journey: the plug point for real runs.

The corpus describes *what to ask* the product (CLI args, HTTP requests, chat
messages) and *what to expect*.  A driver turns a step into an
:class:`Observation` against a live product; ``evaluate_journey`` and
``check_expectation`` are pure and shared by every driver, so the verdict never
depends on the implementation under test.  No real driver ships with this
package (see docs/EVAL_E2E.md).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from evals.e2e.models import E2ECase, Expectation, HttpStep, JourneyResult, OracleStep


class Observation(BaseModel):
    """What a driver saw after executing one step against the product."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exit_code: int | None = None
    status: int | None = None
    text: str = ""
    error_text: str = ""
    json_body: Any = None


class OracleDriver(Protocol):
    """Executes one oracle step against a running product."""

    def execute(self, step: OracleStep) -> Observation: ...


def signed_body(step: HttpStep) -> tuple[bytes, dict[str, str]]:
    """Canonical request body and signature headers for a webhook-style step."""

    body = json.dumps(step.json_body or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers: dict[str, str] = {}
    if step.sign is not None:
        digest = hmac.new(step.sign.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers[step.sign.header] = digest
    return body, headers


def _subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(_subset(e, a) for e, a in zip(expected, actual))
    return bool(expected == actual) and type(expected) is type(actual)


def check_expectation(expect: Expectation, observed: Observation) -> list[str]:
    """Return content-free failure codes (empty list means the step passed)."""

    failures: list[str] = []
    if expect.exit_code is not None and observed.exit_code != expect.exit_code:
        failures.append("exit_code")
    if expect.status is not None and observed.status != expect.status:
        failures.append("status")
    for index, needle in enumerate(expect.contains):
        if needle not in observed.text:
            failures.append(f"contains[{index}]")
    for index, needle in enumerate(expect.not_contains):
        if needle in observed.text:
            failures.append(f"not_contains[{index}]")
    for index, pattern in enumerate(expect.regex):
        if not re.search(pattern, observed.text, flags=re.MULTILINE | re.DOTALL):
            failures.append(f"regex[{index}]")
    for index, needle in enumerate(expect.error_contains):
        if needle not in observed.error_text:
            failures.append(f"error_contains[{index}]")
    if expect.json_subset is not None and not _subset(expect.json_subset, observed.json_body):
        failures.append("json_subset")
    return failures


def evaluate_journey(case: E2ECase, driver: OracleDriver) -> list[JourneyResult]:
    """Run every step in order; a driver exception counts as a failed step."""

    results: list[JourneyResult] = []
    for step in case.journey:
        try:
            observed = driver.execute(step)
            passed = not check_expectation(step.expect, observed)
        except Exception:  # noqa: BLE001 - a crashing product must fail the step, not the evaluator
            passed = False
        results.append(JourneyResult(step_id=step.id, passed=passed))
    return results
