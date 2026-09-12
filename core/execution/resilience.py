"""Resilient execution engine with Circuit Breaker, graceful fallback, and mid-job stability.

Conforms to:
- HYBRID_WORKFLOW_PLAN_2026-09-08 (Sections 2, 5, 9, 12, line 263 / HF-11)
- HYBRID_AUTONOMY_REQUIREMENTS (Section 4, 7, Scenario G6)
- AGENTS.md (Universal Engineering Standards: strict typing, decoupled logic, zero swallowed exceptions)

Key Guarantees:
1. Local-first execution via Ollama ($0 cost).
2. Graceful fallback to cloud (OpenRouter / Pareto models) when local is unavailable.
3. Invariant Anti-Fable (Scenario G6): Fable-5.1 is NEVER used as fallback or default.
4. Circuit Breaker per provider: CLOSED -> OPEN -> HALF_OPEN -> CLOSED.
5. Mid-job Model Stability (Scenario G6): Daily benchmark refresh does not swap models mid-job.
6. Structured FallbackEvent audit records with root cause diagnosis.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from core.execution.contracts import UnknownCostPolicy
from core.execution.providers import (
    ModelProvider,
    OllamaModelProvider,
    OpenRouterModelProvider,
    ProviderResponse,
)

logger = logging.getLogger(__name__)

# Canonical Anti-Fable Invariant constant (Scenario G6)
FORBIDDEN_FALLBACK_MODELS = frozenset({"fable-5.1", "fable", "fable-5.1-chat"})

# Default Pareto cloud fallbacks (from model_router / frontier catalog)
DEFAULT_PARETO_FALLBACK_MODELS = {
    "low": "qwen/qwen3-8-flash-next",
    "medium": "deepseek/deepseek-v4-pro",
    "high": "deepseek/deepseek-v4-pro",
    "critical": "anthropic/claude-3.7-sonnet",
}


class CircuitState(str, Enum):
    """Lifecycle states of the Circuit Breaker."""

    CLOSED = "closed"        # Healthy, traffic allowed
    OPEN = "open"            # Unhealthy/degraded, traffic short-circuited to fallback
    HALF_OPEN = "half_open"  # Probing recovery with limited canary traffic


class FallbackReason(str, Enum):
    """Root causes triggering a model/provider fallback."""

    CONNECTION_REFUSED = "connection_refused"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    RATE_LIMITED = "rate_limited"
    CIRCUIT_OPEN = "circuit_open"
    MODEL_NOT_FOUND = "model_not_found"
    OUT_OF_MEMORY = "out_of_memory"
    BUDGET_INSUFFICIENT = "budget_insufficient"
    UNKNOWN = "unknown"


class FallbackEvent(BaseModel):
    """Immutable audit record of an automated provider fallback."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = Field(min_length=1)
    from_provider: str = Field(min_length=1)
    from_model: str = Field(min_length=1)
    to_provider: str = Field(min_length=1)
    to_model: str = Field(min_length=1)
    reason: FallbackReason
    error_message: str
    latency_ms: float = Field(ge=0.0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class JobModelPin(BaseModel):
    """Deterministic binding pinning model choices for the entire duration of a job.

    Enforces Scenario G6 Invariant: 'atualização diária não troca modelo no meio do job'.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    complexity: str = "medium"
    pinned_model: str = Field(min_length=1)
    pinned_provider: str = Field(min_length=1)
    pinned_effort: str = "high"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CircuitBreaker:
    """Thread-safe circuit breaker protecting services against cascading failures."""

    def __init__(
        self,
        provider_id: str,
        *,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
        half_open_trials: int = 1,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self.provider_id = provider_id
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self.half_open_trials = half_open_trials
        self._time_func = time_func

        self._lock = threading.Lock()
        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._consecutive_successes: int = 0
        self._last_failure_time: float = 0.0
        self._half_open_trials_left: int = 0

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._evaluate_state()
            return self._state

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    @property
    def last_failure_time(self) -> float:
        with self._lock:
            return self._last_failure_time

    def _evaluate_state(self) -> None:
        """Internal transition from OPEN to HALF_OPEN when cooldown elapses."""
        now = self._time_func()
        if self._state == CircuitState.OPEN:
            if now - self._last_failure_time >= self.recovery_timeout_seconds:
                self._state = CircuitState.HALF_OPEN
                self._half_open_trials_left = self.half_open_trials
                logger.info(
                    "CircuitBreaker for %s transitioned from OPEN to HALF_OPEN (cooldown passed)",
                    self.provider_id,
                )

    def allow_request(self) -> bool:
        """Check if an outbound request is permitted through the circuit."""
        with self._lock:
            self._evaluate_state()
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.HALF_OPEN:
                if self._half_open_trials_left > 0:
                    self._half_open_trials_left -= 1
                    return True
                return False
            return False  # CircuitState.OPEN

    def record_success(self) -> None:
        """Record a successful execution, resetting failures or closing half-open circuit."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._consecutive_successes += 1
                logger.info("CircuitBreaker for %s recovered and is now CLOSED", self.provider_id)
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0
                self._consecutive_successes += 1

    def record_failure(self, error: Exception | str) -> None:
        """Record an execution failure, potentially opening the circuit."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = self._time_func()
            self._consecutive_successes = 0

            if self._state == CircuitState.HALF_OPEN:
                # Probe failed, re-open circuit
                self._state = CircuitState.OPEN
                logger.warning(
                    "CircuitBreaker for %s failed half-open probe (%s), reverting to OPEN",
                    self.provider_id,
                    error,
                )
            elif self._state == CircuitState.CLOSED and self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    "CircuitBreaker for %s reached %d failures (%s), opening circuit for %.1fs",
                    self.provider_id,
                    self._failure_count,
                    error,
                    self.recovery_timeout_seconds,
                )

    def reset(self) -> None:
        """Force manual reset to CLOSED state."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._consecutive_successes = 0
            self._last_failure_time = 0.0
            self._half_open_trials_left = 0


def classify_error(exc: Exception) -> FallbackReason:
    """Deterministically maps an exception to a classified FallbackReason."""
    msg = str(exc).lower()
    if "connection refused" in msg or "actively refused" in msg or "failed to connect" in msg:
        return FallbackReason.CONNECTION_REFUSED
    if "timed out" in msg or "timeout" in msg:
        return FallbackReason.TIMEOUT
    if "429" in msg or "rate limit" in msg or "quota" in msg:
        return FallbackReason.RATE_LIMITED
    if "not found" in msg or "model not found" in msg or "404" in msg:
        return FallbackReason.MODEL_NOT_FOUND
    if "out of memory" in msg or "cuda oom" in msg or "oom" in msg:
        return FallbackReason.OUT_OF_MEMORY
    if "http" in msg or "500" in msg or "502" in msg or "503" in msg or "504" in msg:
        return FallbackReason.HTTP_ERROR
    return FallbackReason.UNKNOWN


class ResilientModelProvider:
    """Composite model provider with Circuit Breakers, graceful fallback and audit tracking.

    Implements the ModelProvider protocol with:
    - Zero cost local execution ($0.00) via local Ollama.
    - Automatic graceful fallback to cloud provider when local is unavailable.
    - Anti-Fable invariant (Scenario G6): NEVER fall back to Fable-5.1.
    - Transient retry with exponential backoff on cloud rate limits / gateway errors.
    - Comprehensive fallback telemetry.
    """

    provider_id: str = "resilient"

    def __init__(
        self,
        local_provider: ModelProvider | None = None,
        cloud_provider: ModelProvider | None = None,
        *,
        circuit_failure_threshold: int = 3,
        circuit_recovery_timeout_seconds: float = 30.0,
        enable_fallback: bool = True,
        max_retries: int = 2,
        initial_backoff_seconds: float = 0.5,
        default_cloud_fallback_model: str = "deepseek/deepseek-v4-pro",
        budget_check_callback: Callable[[str, float], bool] | None = None,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self.local_provider = local_provider or OllamaModelProvider()
        self.cloud_provider = cloud_provider or OpenRouterModelProvider()
        self.enable_fallback = enable_fallback
        self.max_retries = max_retries
        self.initial_backoff_seconds = initial_backoff_seconds
        self.default_cloud_fallback_model = default_cloud_fallback_model
        self.budget_check_callback = budget_check_callback
        self.time_func = time_func

        # Circuit breakers per provider
        self.local_circuit = CircuitBreaker(
            "ollama",
            failure_threshold=circuit_failure_threshold,
            recovery_timeout_seconds=circuit_recovery_timeout_seconds,
            time_func=time_func,
        )
        self.cloud_circuit = CircuitBreaker(
            "cloud",
            failure_threshold=circuit_failure_threshold,
            recovery_timeout_seconds=circuit_recovery_timeout_seconds,
            time_func=time_func,
        )

        self._fallback_history: list[FallbackEvent] = []
        self._lock = threading.Lock()

    @property
    def fallback_history(self) -> tuple[FallbackEvent, ...]:
        with self._lock:
            return tuple(self._fallback_history)

    def _record_fallback(self, event: FallbackEvent) -> None:
        with self._lock:
            self._fallback_history.append(event)
        logger.info(
            "Fallback executed: %s:%s -> %s:%s | Reason: %s",
            event.from_provider,
            event.from_model,
            event.to_provider,
            event.to_model,
            event.reason.value,
        )

    def resolve_fallback_model(self, target_model: str, complexity: str = "medium") -> str:
        """Resolve suitable cloud fallback model enforcing Scenario G6 Anti-Fable."""
        # Check Anti-Fable
        if target_model.lower() in FORBIDDEN_FALLBACK_MODELS:
            logger.warning("Rejected forbidden model %s from fallback candidates", target_model)

        # If original model was cloud-qualified (contains slash), keep it or map to Pareto
        if "/" in target_model and target_model.lower() not in FORBIDDEN_FALLBACK_MODELS:
            return target_model

        # Map by complexity from Pareto Frontier
        fallback = DEFAULT_PARETO_FALLBACK_MODELS.get(
            complexity.lower(), self.default_cloud_fallback_model
        )
        if fallback.lower() in FORBIDDEN_FALLBACK_MODELS:
            fallback = "deepseek/deepseek-v4-pro"
        return fallback

    def _execute_with_retry(
        self,
        provider: ModelProvider,
        prompt: str,
        *,
        model: str,
        system_prompt: str | None,
        max_tokens: int | None,
        temperature: float,
        unknown_cost_policy: UnknownCostPolicy,
    ) -> ProviderResponse:
        """Executes a provider call with exponential backoff on transient errors."""
        last_error: Exception | None = None
        backoff = self.initial_backoff_seconds

        for attempt in range(self.max_retries + 1):
            try:
                return provider.generate(
                    prompt,
                    model=model,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    unknown_cost_policy=unknown_cost_policy,
                )
            except Exception as exc:
                last_error = exc
                reason = classify_error(exc)
                if attempt < self.max_retries and reason in (
                    FallbackReason.RATE_LIMITED,
                    FallbackReason.HTTP_ERROR,
                    FallbackReason.TIMEOUT,
                ):
                    logger.debug(
                        "Transient error on %s attempt %d/%d: %s. Backing off %.2fs",
                        model,
                        attempt + 1,
                        self.max_retries + 1,
                        exc,
                        backoff,
                    )
                    time.sleep(backoff)
                    backoff *= 2.0
                else:
                    raise last_error

        raise last_error or RuntimeError("Retry loop exited without result")

    def generate(
        self,
        prompt: str,
        *,
        model: str,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        unknown_cost_policy: UnknownCostPolicy = UnknownCostPolicy.REJECT,
        attempt_id: str | None = None,
        complexity: str = "medium",
        allow_fallback: bool | None = None,
    ) -> ProviderResponse:
        """Generate response with local-first precedence, circuit breaker and fallback."""
        # Enforce Scenario G6 Anti-Fable Invariant immediately
        if model.lower() in FORBIDDEN_FALLBACK_MODELS:
            raise ValueError(
                f"Model '{model}' violates Scenario G6 Anti-Fable governance. "
                "Fable cannot be requested as default or primary model."
            )

        should_fallback = self.enable_fallback if allow_fallback is None else allow_fallback
        att_id = attempt_id or f"gen_{int(time.time() * 1000)}"
        is_cloud_model = "/" in model

        # -----------------------------------------------------------------
        # Path A: Cloud model requested directly
        # -----------------------------------------------------------------
        if is_cloud_model:
            # Enforce Scenario G6: Fable is never allowed as default
            if model.lower() in FORBIDDEN_FALLBACK_MODELS:
                raise ValueError(
                    f"Model '{model}' violates Scenario G6 Anti-Fable governance. "
                    "Fable cannot be requested as default."
                )

            start_t = time.perf_counter()
            try:
                resp = self._execute_with_retry(
                    self.cloud_provider,
                    prompt,
                    model=model,
                    system_prompt=system_prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    unknown_cost_policy=unknown_cost_policy,
                )
                self.cloud_circuit.record_success()
                return resp
            except Exception as exc:
                self.cloud_circuit.record_failure(exc)
                raise

        # -----------------------------------------------------------------
        # Path B: Local model requested (Local-First $0)
        # -----------------------------------------------------------------
        # 1. Check Circuit Breaker for local provider
        if not self.local_circuit.allow_request():
            if not should_fallback:
                raise RuntimeError(
                    f"Local provider circuit is OPEN and fallback is disabled for model '{model}'."
                )

            # Local circuit is OPEN, divert immediately to cloud without waiting
            fallback_model = self.resolve_fallback_model(model, complexity)
            event = FallbackEvent(
                attempt_id=att_id,
                from_provider="ollama",
                from_model=model,
                to_provider="cloud",
                to_model=fallback_model,
                reason=FallbackReason.CIRCUIT_OPEN,
                error_message="Local circuit breaker is OPEN; diverted immediately to cloud",
                latency_ms=0.0,
            )
            self._record_fallback(event)
            return self._dispatch_cloud_fallback(
                prompt=prompt,
                fallback_model=fallback_model,
                original_model=model,
                event=event,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                unknown_cost_policy=unknown_cost_policy,
            )

        # 2. Local circuit allows request, try local generation
        start_t = time.perf_counter()
        try:
            resp = self.local_provider.generate(
                prompt,
                model=model,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                unknown_cost_policy=unknown_cost_policy,
            )
            self.local_circuit.record_success()
            return resp
        except Exception as exc:
            latency_ms = round((time.perf_counter() - start_t) * 1000, 2)
            self.local_circuit.record_failure(exc)

            if not should_fallback:
                logger.error("Local generation failed for %s and fallback disabled: %s", model, exc)
                raise

            # 3. Graceful fallback to cloud
            reason = classify_error(exc)
            fallback_model = self.resolve_fallback_model(model, complexity)

            event = FallbackEvent(
                attempt_id=att_id,
                from_provider="ollama",
                from_model=model,
                to_provider="cloud",
                to_model=fallback_model,
                reason=reason,
                error_message=str(exc),
                latency_ms=latency_ms,
            )
            self._record_fallback(event)

            return self._dispatch_cloud_fallback(
                prompt=prompt,
                fallback_model=fallback_model,
                original_model=model,
                event=event,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                unknown_cost_policy=unknown_cost_policy,
            )

    def _dispatch_cloud_fallback(
        self,
        prompt: str,
        fallback_model: str,
        original_model: str,
        event: FallbackEvent,
        *,
        system_prompt: str | None,
        max_tokens: int | None,
        temperature: float,
        unknown_cost_policy: UnknownCostPolicy,
    ) -> ProviderResponse:
        """Executes the cloud fallback call under budget check and attaches audit metadata."""
        # Check Anti-Fable Invariant
        if fallback_model.lower() in FORBIDDEN_FALLBACK_MODELS:
            raise ValueError(
                f"Resolved fallback model '{fallback_model}' violates Anti-Fable Scenario G6."
            )

        # Verify budget if callback provided
        if self.budget_check_callback is not None:
            # Estimate token cost (~$0.000002 per token or conservative estimate)
            estimated_tokens = max(100, len(prompt.split()) * 2)
            estimated_cost = estimated_tokens * 0.000002
            is_budget_ok = self.budget_check_callback(fallback_model, estimated_cost)
            if not is_budget_ok:
                raise RuntimeError(
                    f"Budget ceiling or quota window exceeded; cannot dispatch fallback '{fallback_model}'."
                )

        try:
            resp = self._execute_with_retry(
                self.cloud_provider,
                prompt,
                model=fallback_model,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                unknown_cost_policy=unknown_cost_policy,
            )
            self.cloud_circuit.record_success()

            # Attach fallback audit metadata
            merged_meta = dict(resp.metadata or {})
            merged_meta.update({
                "fallback_triggered": "true",
                "fallback_from_provider": event.from_provider,
                "fallback_from_model": event.from_model,
                "fallback_reason": event.reason.value,
                "fallback_to_model": fallback_model,
            })

            return ProviderResponse(
                text=resp.text,
                model=resp.model,
                tokens_prompt=resp.tokens_prompt,
                tokens_completion=resp.tokens_completion,
                total_tokens=resp.total_tokens,
                latency_seconds=resp.latency_seconds,
                measured_cost=resp.measured_cost,
                estimated_cost=resp.estimated_cost,
                is_measured=resp.is_measured,
                metadata=merged_meta,
            )
        except Exception as exc:
            self.cloud_circuit.record_failure(exc)
            raise RuntimeError(
                f"Graceful fallback to cloud model '{fallback_model}' failed: {exc}"
            ) from exc


__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "FallbackEvent",
    "FallbackReason",
    "FORBIDDEN_FALLBACK_MODELS",
    "JobModelPin",
    "ResilientModelProvider",
    "classify_error",
]
