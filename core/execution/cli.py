"""Headless Command-Line Interface for Resilient Model Execution and Circuit Diagnostics.

Conforms to:
- Universal Engineering Standards (AGENTS.md)
- Windows UTF-8 console output
- Structured JSON output with --json flag
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from core.execution.contracts import UnknownCostPolicy
from core.execution.resilience import (
    CircuitBreaker,
    CircuitState,
    ResilientModelProvider,
)

# Enforce UTF-8 on Windows CLI
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Shared singleton provider for CLI sessions
_GLOBAL_RESILIENT_PROVIDER: ResilientModelProvider | None = None


def get_cli_provider() -> ResilientModelProvider:
    global _GLOBAL_RESILIENT_PROVIDER
    if _GLOBAL_RESILIENT_PROVIDER is None:
        _GLOBAL_RESILIENT_PROVIDER = ResilientModelProvider()
    return _GLOBAL_RESILIENT_PROVIDER


def cmd_execute(args: argparse.Namespace) -> int:
    """Executes a prompt through the resilient model provider."""
    provider = get_cli_provider()
    try:
        resp = provider.generate(
            prompt=args.prompt,
            model=args.model,
            system_prompt=args.system_prompt,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            complexity=args.complexity,
            allow_fallback=not args.no_fallback,
            unknown_cost_policy=UnknownCostPolicy.ESTIMATE,
        )
        data: dict[str, Any] = {
            "status": "success",
            "model": resp.model,
            "text": resp.text,
            "tokens_prompt": resp.tokens_prompt,
            "tokens_completion": resp.tokens_completion,
            "total_tokens": resp.total_tokens,
            "latency_seconds": resp.latency_seconds,
            "measured_cost": resp.measured_cost,
            "estimated_cost": resp.estimated_cost,
            "metadata": resp.metadata or {},
        }
        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(f"[SUCCESS] Model: {resp.model}")
            print(f"Tokens: {resp.total_tokens} | Cost: ${resp.estimated_cost:.6f}")
            print("--- Output ---")
            print(resp.text)
        return 0
    except Exception as exc:
        data = {
            "status": "error",
            "error": str(exc),
            "model": args.model,
        }
        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            print(f"[ERROR] Execution failed: {exc}", file=sys.stderr)
        return 1


def cmd_circuit_status(args: argparse.Namespace) -> int:
    """Inspects the circuit breaker status of model providers."""
    provider = get_cli_provider()
    data = {
        "providers": {
            "ollama": {
                "state": provider.local_circuit.state.value,
                "failure_count": provider.local_circuit.failure_count,
                "recovery_timeout_seconds": provider.local_circuit.recovery_timeout_seconds,
            },
            "cloud": {
                "state": provider.cloud_circuit.state.value,
                "failure_count": provider.cloud_circuit.failure_count,
                "recovery_timeout_seconds": provider.cloud_circuit.recovery_timeout_seconds,
            },
        },
        "fallback_history_count": len(provider.fallback_history),
        "recent_fallbacks": [
            event.model_dump(mode="json")
            for event in provider.fallback_history[-5:]
        ],
    }
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print("=== Circuit Breaker Status ===")
        for p_id, p_info in data["providers"].items():
            state_str = p_info["state"].upper()
            print(f"[{p_id.upper()}] State: {state_str} | Failures: {p_info['failure_count']}")
        print(f"Total Fallbacks Recorded: {data['fallback_history_count']}")
    return 0


def cmd_reset_circuit(args: argparse.Namespace) -> int:
    """Resets circuit breakers to CLOSED."""
    provider = get_cli_provider()
    target = args.provider.lower()
    if target in ("ollama", "all", "local"):
        provider.local_circuit.reset()
    if target in ("cloud", "all", "openrouter"):
        provider.cloud_circuit.reset()

    data = {"status": "reset", "target": target}
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(f"[OK] Circuit breaker for '{target}' reset to CLOSED.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resilient Model Execution & Circuit Diagnostics CLI (HF-11)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # execute
    p_exec = subparsers.add_parser("execute", help="Execute a prompt through resilient providers")
    p_exec.add_argument("--prompt", required=True, help="Prompt text to process")
    p_exec.add_argument("--model", default="qwen-code-fast:latest", help="Target model identifier")
    p_exec.add_argument("--system-prompt", default=None, help="Optional system prompt")
    p_exec.add_argument("--max-tokens", type=int, default=1024, help="Maximum completion tokens")
    p_exec.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature")
    p_exec.add_argument("--complexity", default="medium", choices=["low", "medium", "high", "critical"])
    p_exec.add_argument("--no-fallback", action="store_true", help="Disable graceful fallback to cloud")
    p_exec.add_argument("--json", action="store_true", help="Format output as JSON")

    # circuit-status
    p_status = subparsers.add_parser("circuit-status", help="Display circuit breaker status")
    p_status.add_argument("--json", action="store_true", help="Format output as JSON")

    # reset-circuit
    p_reset = subparsers.add_parser("reset-circuit", help="Reset circuit breaker to CLOSED")
    p_reset.add_argument("--provider", default="all", choices=["ollama", "cloud", "all"])
    p_reset.add_argument("--json", action="store_true", help="Format output as JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "execute":
        return cmd_execute(args)
    if args.command == "circuit-status":
        return cmd_circuit_status(args)
    if args.command == "reset-circuit":
        return cmd_reset_circuit(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
