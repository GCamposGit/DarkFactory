"""Model provider abstraction separating pure inference from tool-calling executors.

Conforms to Section 2 and Section 3 of DEVELOPMENT_PLAN_2026-09-05:
- ModelProvider (inference) is strictly separated from AgentExecutor (tool execution).
- Models report tokens, latency, measured cost, or estimated cost under UnknownCostPolicy.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from core.execution.contracts import UnknownCostPolicy


@dataclass(frozen=True)
class ProviderResponse:
    """Immutable response payload from a model provider invocation."""

    text: str
    model: str
    tokens_prompt: int
    tokens_completion: int
    total_tokens: int
    latency_seconds: float
    measured_cost: float | None = None
    estimated_cost: float = 0.0
    is_measured: bool = True
    metadata: dict[str, str] | None = None


@runtime_checkable
class ModelProvider(Protocol):
    """Protocol for model inference engines."""

    provider_id: str

    def generate(
        self,
        prompt: str,
        *,
        model: str,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        unknown_cost_policy: UnknownCostPolicy = UnknownCostPolicy.REJECT,
    ) -> ProviderResponse:
        """Execute a text generation call within budget constraints."""
        ...


class MockModelProvider:
    """Deterministic mock provider for offline testing and verification."""

    def __init__(
        self,
        provider_id: str = "mock-provider",
        *,
        fixed_response: str = "Mock execution completed successfully.",
        token_rate_per_word: float = 1.3,
        cost_per_token: float = 0.000002,
        simulate_unknown_cost: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.fixed_response = fixed_response
        self.token_rate_per_word = token_rate_per_word
        self.cost_per_token = cost_per_token
        self.simulate_unknown_cost = simulate_unknown_cost
        self.invocation_count = 0

    def generate(
        self,
        prompt: str,
        *,
        model: str,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.0,
        unknown_cost_policy: UnknownCostPolicy = UnknownCostPolicy.REJECT,
    ) -> ProviderResponse:
        start_time = time.perf_counter()
        self.invocation_count += 1

        prompt_tokens = max(1, int(len(prompt.split()) * self.token_rate_per_word))
        completion_tokens = max(1, int(len(self.fixed_response.split()) * self.token_rate_per_word))
        total_tokens = prompt_tokens + completion_tokens

        latency = max(0.001, time.perf_counter() - start_time)

        if self.simulate_unknown_cost:
            if unknown_cost_policy == UnknownCostPolicy.REJECT:
                raise ValueError(
                    f"Provider {self.provider_id} returned unknown cost and policy is REJECT."
                )
            if unknown_cost_policy == UnknownCostPolicy.ESTIMATE:
                return ProviderResponse(
                    text=self.fixed_response,
                    model=model,
                    tokens_prompt=prompt_tokens,
                    tokens_completion=completion_tokens,
                    total_tokens=total_tokens,
                    latency_seconds=latency,
                    measured_cost=None,
                    estimated_cost=round(total_tokens * self.cost_per_token, 6),
                    is_measured=False,
                )
            # CONSERVATIVE_MAX
            return ProviderResponse(
                text=self.fixed_response,
                model=model,
                tokens_prompt=prompt_tokens,
                tokens_completion=completion_tokens,
                total_tokens=total_tokens,
                latency_seconds=latency,
                measured_cost=None,
                estimated_cost=round(total_tokens * self.cost_per_token * 2.5, 6),
                is_measured=False,
            )

        measured_cost = round(total_tokens * self.cost_per_token, 6)
        return ProviderResponse(
            text=self.fixed_response,
            model=model,
            tokens_prompt=prompt_tokens,
            tokens_completion=completion_tokens,
            total_tokens=total_tokens,
            latency_seconds=latency,
            measured_cost=measured_cost,
            estimated_cost=measured_cost,
            is_measured=True,
        )
