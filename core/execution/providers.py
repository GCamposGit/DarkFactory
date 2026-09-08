"""Model provider abstraction separating pure inference from tool-calling executors.

Conforms to Section 2 and Section 3 of DEVELOPMENT_PLAN_2026-09-05:
- ModelProvider (inference) is strictly separated from AgentExecutor (tool execution).
- Models report tokens, latency, measured cost, or estimated cost under UnknownCostPolicy.
- Common adapter for Ollama, OpenRouter, and offline Mock providers.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from core.execution.contracts import UnknownCostPolicy
from core.usage.ledger import ModelUsageLedger, infer_model_tier
from core.usage.models import ModelCallEvent, ModelModality, ModelTier

logger = logging.getLogger(__name__)


def get_openrouter_api_key() -> str | None:
    """Recovers OpenRouter API key from environment variable or Windows registry."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key and sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
                key, _ = winreg.QueryValueEx(k, "OPENROUTER_API_KEY")
        except Exception:
            pass
    return key.strip() if (key and key.strip()) else None


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
        responses_by_model: dict[str, str] | None = None,
        response_sequence: list[str] | None = None,
        token_rate_per_word: float = 1.3,
        cost_per_token: float = 0.000002,
        simulate_unknown_cost: bool = False,
    ) -> None:
        self.provider_id = provider_id
        self.fixed_response = fixed_response
        self.responses_by_model = responses_by_model or {}
        self.response_sequence = list(response_sequence) if response_sequence is not None else None
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

        if self.response_sequence is not None and self.invocation_count < len(self.response_sequence):
            resp_text = self.response_sequence[self.invocation_count]
        elif model in self.responses_by_model:
            resp_text = self.responses_by_model[model]
        else:
            resp_text = self.fixed_response

        self.invocation_count += 1

        prompt_tokens = max(1, int(len(prompt.split()) * self.token_rate_per_word))
        completion_tokens = max(1, int(len(resp_text.split()) * self.token_rate_per_word))
        total_tokens = prompt_tokens + completion_tokens

        latency = max(0.001, time.perf_counter() - start_time)

        if self.simulate_unknown_cost:
            if unknown_cost_policy == UnknownCostPolicy.REJECT:
                raise ValueError(
                    f"Provider {self.provider_id} returned unknown cost and policy is REJECT."
                )
            if unknown_cost_policy == UnknownCostPolicy.ESTIMATE:
                return ProviderResponse(
                    text=resp_text,
                    model=model,
                    tokens_prompt=prompt_tokens,
                    tokens_completion=completion_tokens,
                    total_tokens=total_tokens,
                    latency_seconds=latency,
                    measured_cost=None,
                    estimated_cost=round(total_tokens * self.cost_per_token, 6),
                    is_measured=False,
                    metadata={"backend": "mock"},
                )
            # CONSERVATIVE_MAX
            return ProviderResponse(
                text=resp_text,
                model=model,
                tokens_prompt=prompt_tokens,
                tokens_completion=completion_tokens,
                total_tokens=total_tokens,
                latency_seconds=latency,
                measured_cost=None,
                estimated_cost=round(total_tokens * self.cost_per_token * 2.5, 6),
                is_measured=False,
                metadata={"backend": "mock"},
            )

        measured_cost = round(total_tokens * self.cost_per_token, 6)
        return ProviderResponse(
            text=resp_text,
            model=model,
            tokens_prompt=prompt_tokens,
            tokens_completion=completion_tokens,
            total_tokens=total_tokens,
            latency_seconds=latency,
            measured_cost=measured_cost,
            estimated_cost=measured_cost,
            is_measured=True,
            metadata={"backend": "mock"},
        )


class OllamaModelProvider:
    """Ollama local model provider executing inference via HTTP API."""

    provider_id: str = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        *,
        timeout: float = 60.0,
        usage_ledger: ModelUsageLedger | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.usage_ledger = usage_ledger

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
        url = f"{self.base_url}/api/generate"
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if max_tokens is not None:
            payload["options"]["num_predict"] = max_tokens
        if system_prompt:
            payload["system"] = system_prompt

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                latency = max(0.001, time.perf_counter() - start)
                returned_model = data.get("model", model)
                text = data.get("response", "")
                prompt_tokens = data.get("prompt_eval_count") or 0
                completion_tokens = data.get("eval_count") or 0
                total_tokens = prompt_tokens + completion_tokens

                if self.usage_ledger is not None:
                    try:
                        self.usage_ledger.record(
                            ModelCallEvent(
                                provider="ollama",
                                model=returned_model,
                                tier=ModelTier(infer_model_tier("ollama", returned_model)),
                                harness="core.execution",
                                modality=ModelModality.TEXT,
                                success=True,
                                input_tokens=prompt_tokens,
                                output_tokens=completion_tokens,
                                cost_usd=0.0,
                                latency_ms=round(latency * 1000, 1),
                                source="core.execution.providers.ollama",
                            )
                        )
                    except Exception as exc:
                        logger.debug("Failed to record ollama usage: %s", exc)

                return ProviderResponse(
                    text=text,
                    model=returned_model,
                    tokens_prompt=prompt_tokens,
                    tokens_completion=completion_tokens,
                    total_tokens=total_tokens,
                    latency_seconds=latency,
                    measured_cost=0.0,
                    estimated_cost=0.0,
                    is_measured=True,
                    metadata={"backend": "ollama"},
                )
        except Exception as exc:
            latency = max(0.001, time.perf_counter() - start)
            if self.usage_ledger is not None:
                try:
                    self.usage_ledger.record(
                        ModelCallEvent(
                            provider="ollama",
                            model=model,
                            tier=ModelTier(infer_model_tier("ollama", model)),
                            harness="core.execution",
                            modality=ModelModality.TEXT,
                            success=False,
                            latency_ms=round(latency * 1000, 1),
                            source="core.execution.providers.ollama",
                        )
                    )
                except Exception:
                    pass
            logger.error("Ollama generation failed for %s: %s", model, exc)
            raise RuntimeError(f"Ollama generation failed: {exc}") from exc


class OpenRouterModelProvider:
    """OpenRouter cloud gateway model provider with token and cost tracking."""

    provider_id: str = "openrouter"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 90.0,
        usage_ledger: ModelUsageLedger | None = None,
    ) -> None:
        self.api_key = api_key or get_openrouter_api_key()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.usage_ledger = usage_ledger

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
        key = self.api_key or get_openrouter_api_key()
        if not key:
            raise RuntimeError("OPENROUTER_API_KEY não encontrada no ambiente ou registro.")

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or 1024,
        }

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/DarkFac",
                "X-Title": "DarkFactory Autonomous Provider",
            },
            method="POST",
        )

        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                latency = max(0.001, time.perf_counter() - start)
                choices = data.get("choices", [])
                response_text = choices[0].get("message", {}).get("content", "") if choices else ""
                returned_model = data.get("model", model)
                usage = data.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens") or 0
                completion_tokens = usage.get("completion_tokens") or 0
                total_tokens = usage.get("total_tokens") or (prompt_tokens + completion_tokens)

                # Cost lookup in daily benchmarks catalog
                cost_usd: float | None = None
                try:
                    from core.benchmarks.fetcher import ensure_daily_benchmark

                    ledger = ensure_daily_benchmark()
                    model_entry = ledger.models.get(model)
                    if model_entry:
                        cost_usd = round(
                            (
                                prompt_tokens * model_entry.input_cost_per_m
                                + completion_tokens * model_entry.output_cost_per_m
                            )
                            / 1_000_000,
                            6,
                        )
                except Exception:
                    pass

                is_measured = cost_usd is not None
                if cost_usd is None:
                    if unknown_cost_policy == UnknownCostPolicy.REJECT:
                        raise ValueError(
                            f"Provider {self.provider_id} returned unknown cost for model {model} and policy is REJECT."
                        )
                    elif unknown_cost_policy == UnknownCostPolicy.ESTIMATE:
                        estimated_cost = round(total_tokens * 0.000002, 6)
                    else:  # CONSERVATIVE_MAX
                        estimated_cost = round(total_tokens * 0.000002 * 2.5, 6)
                else:
                    estimated_cost = cost_usd

                if self.usage_ledger is not None:
                    try:
                        self.usage_ledger.record(
                            ModelCallEvent(
                                provider="openrouter",
                                model=returned_model,
                                tier=ModelTier(infer_model_tier("openrouter", returned_model)),
                                harness="core.execution",
                                modality=ModelModality.TEXT,
                                success=True,
                                input_tokens=prompt_tokens,
                                output_tokens=completion_tokens,
                                cost_usd=cost_usd,
                                latency_ms=round(latency * 1000, 1),
                                source="core.execution.providers.openrouter",
                            )
                        )
                    except Exception as exc:
                        logger.debug("Failed to record openrouter usage: %s", exc)

                return ProviderResponse(
                    text=response_text,
                    model=returned_model,
                    tokens_prompt=prompt_tokens,
                    tokens_completion=completion_tokens,
                    total_tokens=total_tokens,
                    latency_seconds=latency,
                    measured_cost=cost_usd,
                    estimated_cost=estimated_cost,
                    is_measured=is_measured,
                    metadata={"backend": "openrouter"},
                )
        except Exception as exc:
            latency = max(0.001, time.perf_counter() - start)
            if self.usage_ledger is not None:
                try:
                    self.usage_ledger.record(
                        ModelCallEvent(
                            provider="openrouter",
                            model=model,
                            tier=ModelTier(infer_model_tier("openrouter", model)),
                            harness="core.execution",
                            modality=ModelModality.TEXT,
                            success=False,
                            latency_ms=round(latency * 1000, 1),
                            source="core.execution.providers.openrouter",
                        )
                    )
                except Exception:
                    pass
            if not isinstance(exc, ValueError):
                logger.error("OpenRouter generation failed for %s: %s", model, exc)
                raise RuntimeError(f"OpenRouter generation failed: {exc}") from exc
            raise


class UnifiedModelProvider:
    """Multiplexing model provider routing between local and cloud backends."""

    provider_id: str = "unified"

    def __init__(
        self,
        ollama_provider: ModelProvider | None = None,
        openrouter_provider: ModelProvider | None = None,
        mock_provider: ModelProvider | None = None,
    ) -> None:
        self.ollama = ollama_provider or OllamaModelProvider()
        self.openrouter = openrouter_provider or OpenRouterModelProvider()
        self.mock = mock_provider

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
        # Route to mock if configured and model starts with mock
        if self.mock is not None and (model.startswith("mock") or model == "mock"):
            return self.mock.generate(
                prompt,
                model=model,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                unknown_cost_policy=unknown_cost_policy,
            )

        # Route cloud models (containing slash) to OpenRouter
        if "/" in model:
            return self.openrouter.generate(
                prompt,
                model=model,
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                unknown_cost_policy=unknown_cost_policy,
            )

        # Route local models to Ollama
        return self.ollama.generate(
            prompt,
            model=model,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            unknown_cost_policy=unknown_cost_policy,
        )


def get_model_provider(
    provider_id: str = "auto",
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    usage_ledger: ModelUsageLedger | None = None,
    **kwargs: Any,
) -> ModelProvider:
    """Factory creating configured ModelProvider instances."""
    pid = provider_id.lower().strip()
    if pid == "mock":
        return MockModelProvider(provider_id="mock", **kwargs)
    if pid == "ollama":
        url = base_url or "http://localhost:11434"
        return OllamaModelProvider(base_url=url, usage_ledger=usage_ledger, **kwargs)
    if pid == "openrouter":
        return OpenRouterModelProvider(api_key=api_key, usage_ledger=usage_ledger, **kwargs)
    if pid in ("auto", "unified"):
        ollama = OllamaModelProvider(base_url=base_url or "http://localhost:11434", usage_ledger=usage_ledger)
        openrouter = OpenRouterModelProvider(api_key=api_key, usage_ledger=usage_ledger)
        mock = MockModelProvider() if kwargs.get("include_mock") else None
        return UnifiedModelProvider(
            ollama_provider=ollama,
            openrouter_provider=openrouter,
            mock_provider=mock,
        )
    raise ValueError(f"Unknown provider_id: {provider_id}. Must be 'ollama', 'openrouter', 'mock', or 'auto'.")

