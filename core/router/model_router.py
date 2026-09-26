#!/usr/bin/env python3
"""
Model Router CLI & Engine (2026 Edition)
Routes tasks dynamically between:
- Local Ollama Cluster (qwen-fast, qwen-deep, gpt-review) -> Cost $0, Latency 0ms
- Antigravity Native Engine (Gemini 3.8 Flash) -> High context, 1M+ tokens
- Cloud Frontier Tier (Grok 4.6, Claude Opus 5, GPT-6 Astra, DeepSeek V4.1 Flash)
"""

import sys
import json
import argparse
import time
import urllib.request
import urllib.error
from typing import Dict, Any, Optional
from pathlib import Path

from core.paths import project_root

# Ensure root directory in sys.path
_ROOT = project_root()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.benchmarks.fetcher import ensure_daily_benchmark
from core.benchmarks.frontier import (
    select_best_model_for_task,
    build_frontier_summary,
    get_top_candidates_for_tier,
)
from core.router.token_budget import plan_token_stress
from core.usage.ledger import ModelUsageLedger
from core.usage.models import AccountUsageReport, ModelCallEvent, ModelModality, ModelTier

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

OLLAMA_BASE_URL = "http://localhost:11434"

# Model Capability & Strategy Matrix
MODEL_MATRIX = {
    "research": {
        "primary": "gemini-3.8-flash",
        "primary_provider": "antigravity",
        "secondary": "grok-4.6",
        "secondary_provider": "xai",
        "local_fallback": "qwen-code-fast:latest",
        "notes": "Gemini 3.8 Flash handles 1M-2M token context ingestion; Grok 4.6 for live web/docs research."
    },
    "topic_research": {
        "primary": "gemini-3.8-flash",
        "primary_provider": "antigravity",
        "secondary": "grok-4.6",
        "secondary_provider": "xai",
        "local_fallback": "qwen-code-deep:latest",
        "notes": "Gemini 3.8 Flash for paper/arXiv ingestion and formal theory synthesis; Grok 4.6 for cutting-edge live docs."
    },
    "code_scout": {
        "primary": "gemini-3.8-flash",
        "primary_provider": "antigravity",
        "secondary": "claude-opus-5",
        "secondary_provider": "anthropic",
        "local_fallback": "qwen-code-fast:latest",
        "notes": "Gemini 3.8 Flash for repository tree inspection & AST checks; Claude Opus 5 for component reuse safety & license audits."
    },
    "architecture": {
        "primary": "claude-opus-5",
        "primary_provider": "anthropic",
        "secondary": "gpt-sol",
        "secondary_provider": "openai",
        "local_fallback": "qwen-code-deep:latest",
        "notes": "Claude Opus 5 / GPT Sol for PRD and strict non-goals; DeepSeek-V4.1-Flash for cost-effective deep reasoning."
    },
    "coding": {
        "high_complexity": {
            "primary": "claude-opus-5",
            "primary_provider": "anthropic",
            "secondary": "gpt-sol",
            "secondary_provider": "openai",
            "local_fallback": "qwen-code-deep:latest",
        },
        "medium_complexity": {
            "primary": "gemini-3.8-flash",
            "primary_provider": "antigravity",
            "secondary": "deepseek/deepseek-v4.1-flash",
            "secondary_provider": "openrouter",
            "local_fallback": "qwen-code-deep:latest",
        },
        "low_complexity": {
            "primary": "gpt-luna-xhigh",
            "primary_provider": "openai",
            "secondary": "gemini-3.8-flash",
            "secondary_provider": "antigravity",
            "local_fallback": "qwen-code-fast:latest",
        }
    },
    "testing": {
        "primary": "runner-deterministic",
        "primary_provider": "local_process",
        "secondary": "qwen-code-fast:latest",
        "secondary_provider": "ollama",
        "evaluator": "gemini-3.8-flash",
        "notes": "Tests MUST be deterministic code. qwen-code-fast writes fixtures; Gemini 3.8 Flash analyzes failures."
    },
    "adversarial_review": {
        "tier1_local": "gpt-oss-clean:latest",
        "tier1_provider": "ollama",
        "tier2_cloud": "deepseek/deepseek-v4.1-flash",
        "tier2_provider": "openrouter",
        "tier2_alt": "grok-4.6",
        "notes": "Cross-model review rule: The reviewer MUST belong to a different model family than the implementer. DeepSeek-V4.1-Flash via OpenRouter as primary cloud reviewer (1M context, $0.15/M); Grok-4.6 as alt."
    },
    "content_generation": {
        "primary": "claude-opus-5",
        "primary_provider": "anthropic",
        "secondary": "gemini-3.8-flash",
        "secondary_provider": "antigravity",
        "local_fallback": "qwen-code-deep:latest",
        "notes": "Claude Opus 5 for high-signal, anti-slop prose; Gemini 3.8 Flash for high-speed drafting; Qwen-deep for $0 local offline."
    },
    "anti_slop_scrub": {
        "primary": "gpt-oss-clean:latest",
        "primary_provider": "ollama",
        "secondary": "gemini-3.8-flash",
        "secondary_provider": "antigravity",
        "local_fallback": "deterministic-linter",
        "notes": "Deterministic regex & cadence linter + local GPT-OSS / Gemini 3.8 Flash for critique and polish passes."
    },
    "visual_synthesis": {
        "fast": "google/gemini-3.1-flash-lite-image",
        "balanced": "google/gemini-3.1-flash-image",
        "frontier": "openai/gpt-image-2",
        "primary_provider": "openrouter",
        "secondary": "google/gemini-3-pro-image",
        "secondary_provider": "openrouter",
        "local_fallback": "darkfac-vector-v1",
        "notes": "OpenRouter Images API selects fast, balanced, or frontier image models by complexity; Pillow remains the deterministic $0 local fallback."
    }
}

HIGH_INTELLIGENCE_BY_HARNESS: Dict[str, Dict[str, str]] = {
    "antigravity": {"model": "gemini-3.8-flash", "provider": "google", "default_effort": "high"},
    "grok_build": {"model": "grok-4.6", "provider": "xai", "default_effort": "high"},
    "claude_code": {"model": "opus-5.1", "provider": "anthropic", "default_effort": "high"},
    "codex": {"model": "gpt-6-astra", "provider": "openai", "default_effort": "high"},
}

ALLOWED_REASONING_EFFORTS = {"minimal", "low", "medium", "high", "max"}
FORBIDDEN_REASONING_EFFORTS = {"xhigh", "extra-high", "extra_high", "ultra"}


def sanitize_reasoning_effort(effort: Optional[str], complexity: str = "medium") -> str:
    """Enforce HF-07 / Section 4 reasoning effort constraints.

    Allowed: 'high' by default for planning; 'max' when complexity justifies and supported.
    Never 'xhigh', 'extra-high', or 'ultra' (sanitized to 'max' without sending invalid parameters).
    """
    if not effort:
        return "max" if complexity in {"critical", "high"} else "high"
    normalized = effort.strip().lower()
    if normalized in FORBIDDEN_REASONING_EFFORTS:
        return "max"
    if normalized in ALLOWED_REASONING_EFFORTS:
        return normalized
    return "high"


_QUOTA_FAILOVER_MODELS = {
    "antigravity": "gemini-3.8-flash",
    "google": "gemini-3.8-flash",
    "xai": "grok-4.6",
    "anthropic": "claude-opus-5",
    "claude_code": "opus-5.1",
    "openai": "gpt-sol",
    "codex": "gpt-sol",
    "deepseek": "deepseek/deepseek-v4.1-flash",   # openrouter: V4.1-Flash replaces V4-Pro as default DeepSeek failover
    "openrouter": "deepseek/deepseek-v4.1-flash",  # explicit openrouter failover
    "siliconflow": "deepseek/deepseek-v4.1-flash", # siliconflow routes to Pareto leader V4.1-Flash
    "qwen": "qwen3-8-flash-next",
}


def _apply_token_plan(
    result: Dict[str, Any],
    task_type: str,
    complexity: str,
    description: str,
    expected_steps: int,
    usage_report: Optional[AccountUsageReport],
    remaining_hourly_percent: Optional[float],
    offline: bool,
) -> Dict[str, Any]:
    """Attach a forecast and displace a route when quota pressure requires it."""
    preferred_provider = result.get("provider")
    plan = plan_token_stress(
        task_type=task_type,
        complexity=complexity,
        description=description,
        expected_steps=expected_steps,
        accounts=usage_report.accounts if usage_report else (),
        preferred_provider=preferred_provider,
        remaining_hourly_percent=remaining_hourly_percent,
        offline=offline,
    )
    if task_type not in {"visual", "visual_synthesis", "image_gen", "diagram"}:
        if plan.prefer_local and preferred_provider not in {"local_process", "ollama"}:
            result["quota_displaced_recommendation"] = {
                "model": result.get("model"), "provider": preferred_provider,
            }
            result["model"] = (
                "qwen-code-deep:latest"
                if complexity in {"high", "critical"} or task_type in {"architecture", "plan", "prd"}
                else "qwen-code-fast:latest"
            )
            result["provider"] = "ollama"
        elif plan.failover_required and plan.selected_provider:
            selected_provider = plan.selected_provider
            replacement = _QUOTA_FAILOVER_MODELS.get(selected_provider)
            if plan.use_paid_api:
                replacement = (
                    "deepseek/deepseek-v4-pro"
                    if complexity in {"high", "critical"}
                    else "qwen/qwen3-8-flash-next"
                )
            if replacement:
                result["quota_displaced_recommendation"] = {
                    "model": result.get("model"), "provider": preferred_provider,
                }
                result["model"] = replacement
                result["provider"] = selected_provider
    result["token_budget"] = plan.model_dump(mode="json")
    return result


def query_ollama(endpoint: str, data: Optional[Dict[str, Any]] = None, timeout: int = 120) -> Dict[str, Any]:
    """Interacts with local Ollama API."""
    url = f"{OLLAMA_BASE_URL}{endpoint}"
    req_data = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(
        url,
        data=req_data,
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        return {"error": f"Ollama connection failed: {e}"}
    except Exception as e:
        return {"error": str(e)}

def list_local_models():
    """Lists currently installed Ollama models."""
    res = query_ollama("/api/tags")
    if "error" in res:
        print(f"[WARN] Local Ollama unavailable: {res['error']}", file=sys.stderr)
        return []
    models = []
    for m in res.get("models", []):
        models.append({
            "name": m.get("name"),
            "size_gb": round(m.get("size", 0) / (1024**3), 2),
            "family": m.get("details", {}).get("family", "unknown"),
            "parameter_size": m.get("details", {}).get("parameter_size", "unknown"),
            "quantization": m.get("details", {}).get("quantization_level", "unknown")
        })
    return models

def recommend_model(
    task_type: str,
    complexity: str = "medium",
    offline: bool = False,
    task_description: str = "",
    expected_steps: int = 1,
    usage_report: Optional[AccountUsageReport] = None,
    remaining_hourly_percent: Optional[float] = None,
) -> Dict[str, Any]:
    """Recommends best model according to 2026 benchmark and local availability."""
    task_type = task_type.lower()
    complexity = complexity.lower()

    # Trigger automatic fail-safe daily benchmark check (runs at most once per calendar day)
    frontier_data: Optional[Dict[str, Any]] = None
    speculative_candidates = []
    try:
        ledger = ensure_daily_benchmark()
        if ledger and ledger.models:
            best_frontier, f_reason = select_best_model_for_task(
                models=list(ledger.models.values()),
                task_type=task_type,
                complexity=complexity,
                offline=offline
            )
            top3 = get_top_candidates_for_tier(
                models=list(ledger.models.values()),
                tier="local_fast" if offline else complexity,
                k=3
            )
            speculative_candidates = [c.to_dict() for c in top3]
            frontier_data = {
                "date": ledger.date,
                "optimal_cloud_model": best_frontier.model_id,
                "coding_score": best_frontier.coding_score,
                "cost_per_task": best_frontier.cost_per_task,
                "efficiency_score": best_frontier.efficiency_score_coding,
                "throughput_tps": best_frontier.output_speed_tps,
                "rationale": f_reason,
                "speculative_top3": speculative_candidates,
            }
    except Exception as exc:
        frontier_data = {"status": "benchmark_fallback", "note": str(exc)}

    if offline:
        if task_type == "review":
            result = {
                "model": "gpt-oss-clean:latest",
                "provider": "ollama",
                "mode": "offline",
                "reason": "Local GPT-OSS 20B limpo com 16k de contexto, 20 threads e sem prompt prévio ($0 custo)."
            }
        elif complexity in ["high", "critical"] or task_type == "architecture":
            result = {
                "model": "qwen-code-deep:latest",
                "provider": "ollama",
                "mode": "offline",
                "reason": "Local Qwen3-Coder 30B MoE com contexto de 16k e 20 threads (sem prompt)."
            }
        elif task_type in ["visual", "visual_synthesis", "image_gen", "diagram"]:
            result = {
                "model": "darkfac-vector-v1",
                "provider": "local_procedural",
                "mode": "offline",
                "reason": "Local procedural vector rendering engine (Pillow) ($0 custo)."
            }
        else:
            result = {
                "model": "qwen-code-fast:latest",
                "provider": "ollama",
                "mode": "offline",
                "reason": "Local Qwen3-Coder 30B fast worker em 8 P-Cores e contexto 4k (sem prompt)."
            }
        return _apply_token_plan(
            result,
            task_type,
            complexity,
            task_description,
            expected_steps,
            usage_report,
            remaining_hourly_percent,
            offline,
        )

    result: Dict[str, Any] = {}
    if task_type in ["research", "topic_research", "papers"]:
        key = "topic_research" if task_type in ["topic_research", "papers"] else "research"
        result = {
            "model": MODEL_MATRIX[key]["primary"],
            "provider": MODEL_MATRIX[key]["primary_provider"],
            "secondary": MODEL_MATRIX[key]["secondary"],
            "local_fallback": MODEL_MATRIX[key]["local_fallback"],
            "reason": MODEL_MATRIX[key]["notes"]
        }
    elif task_type in ["scout", "code_scout", "repo_scout"]:
        result = {
            "model": MODEL_MATRIX["code_scout"]["primary"],
            "provider": MODEL_MATRIX["code_scout"]["primary_provider"],
            "secondary": MODEL_MATRIX["code_scout"]["secondary"],
            "local_fallback": MODEL_MATRIX["code_scout"]["local_fallback"],
            "reason": MODEL_MATRIX["code_scout"]["notes"]
        }
    elif task_type in ["architecture", "plan", "prd"]:
        result = {
            "model": MODEL_MATRIX["architecture"]["primary"],
            "provider": MODEL_MATRIX["architecture"]["primary_provider"],
            "secondary": MODEL_MATRIX["architecture"]["secondary"],
            "local_fallback": MODEL_MATRIX["architecture"]["local_fallback"],
            "reason": MODEL_MATRIX["architecture"]["notes"]
        }
    elif task_type in ["coding", "implement"]:
        tier = "high_complexity" if complexity in ["high", "critical"] else ("medium_complexity" if complexity == "medium" else "low_complexity")
        primary_model = MODEL_MATRIX["coding"][tier]["primary"]
        primary_provider = MODEL_MATRIX["coding"][tier]["primary_provider"]

        # If daily benchmark identified a Pareto leader with superior cost-benefit, highlight it.
        # We resolve the provider from the ledger entry itself (not a family-name heuristic)
        # so that models like deepseek-v4.1-flash that live on openrouter are mapped correctly.
        if frontier_data and "optimal_cloud_model" in frontier_data:
            opt_model = frontier_data["optimal_cloud_model"]
            # Resolve provider from live ledger entry first
            try:
                entry = ledger.models.get(opt_model)
                if entry is not None:
                    # Map provider field to routing provider key
                    _PROVIDER_MAP = {
                        "deepseek": "openrouter",
                        "anthropic": "anthropic",
                        "openai": "openai",
                        "google": "google",
                        "qwen": "qwen",
                        "xai": "xai",
                    }
                    primary_model = opt_model  # use fully qualified id
                    primary_provider = _PROVIDER_MAP.get(entry.provider, entry.provider)
            except Exception:
                pass  # keep MODEL_MATRIX defaults if ledger lookup fails

        result = {
            "model": primary_model,
            "provider": primary_provider,
            "secondary": MODEL_MATRIX["coding"][tier]["secondary"],
            "local_fallback": MODEL_MATRIX["coding"][tier]["local_fallback"],
            "reason": f"Selected via daily benchmark efficiency surface for {tier} task balance between precision, SWE-bench and cost."
        }
    elif task_type in ["testing", "test", "validate"]:
        result = {
            "model": MODEL_MATRIX["testing"]["primary"],
            "provider": MODEL_MATRIX["testing"]["primary_provider"],
            "helper": MODEL_MATRIX["testing"]["secondary"],
            "evaluator": MODEL_MATRIX["testing"]["evaluator"],
            "reason": MODEL_MATRIX["testing"]["notes"]
        }
    elif task_type in ["review", "audit", "adversarial"]:
        result = {
            "tier1_local": MODEL_MATRIX["adversarial_review"]["tier1_local"],
            "tier2_cloud": MODEL_MATRIX["adversarial_review"]["tier2_cloud"],
            "reason": MODEL_MATRIX["adversarial_review"]["notes"]
        }
    elif task_type in ["content", "content_generation", "copywriting"]:
        result = {
            "model": MODEL_MATRIX["content_generation"]["primary"],
            "provider": MODEL_MATRIX["content_generation"]["primary_provider"],
            "secondary": MODEL_MATRIX["content_generation"]["secondary"],
            "local_fallback": MODEL_MATRIX["content_generation"]["local_fallback"],
            "reason": MODEL_MATRIX["content_generation"]["notes"]
        }
    elif task_type in ["anti_slop", "anti_slop_scrub", "lint_content"]:
        result = {
            "model": MODEL_MATRIX["anti_slop_scrub"]["primary"],
            "provider": MODEL_MATRIX["anti_slop_scrub"]["primary_provider"],
            "secondary": MODEL_MATRIX["anti_slop_scrub"]["secondary"],
            "local_fallback": MODEL_MATRIX["anti_slop_scrub"]["local_fallback"],
            "reason": MODEL_MATRIX["anti_slop_scrub"]["notes"]
        }
    elif task_type in ["visual", "visual_synthesis", "image_gen", "diagram"]:
        if complexity in ["high", "critical"]:
            visual_model = MODEL_MATRIX["visual_synthesis"]["frontier"]
        elif complexity == "medium":
            visual_model = MODEL_MATRIX["visual_synthesis"]["balanced"]
        else:
            visual_model = MODEL_MATRIX["visual_synthesis"]["fast"]
        result = {
            "model": visual_model,
            "provider": MODEL_MATRIX["visual_synthesis"]["primary_provider"],
            "secondary": MODEL_MATRIX["visual_synthesis"]["secondary"],
            "local_fallback": MODEL_MATRIX["visual_synthesis"]["local_fallback"],
            "reason": MODEL_MATRIX["visual_synthesis"]["notes"]
        }
    else:
        result = {
            "model": "gemini-3.8-flash",
            "provider": "antigravity",
            "reason": "Default versatile high-throughput orchestrator."
        }

    if usage_report is None and remaining_hourly_percent is None:
        try:
            from core.usage.monitor import AccountUsageMonitor

            usage_report = AccountUsageMonitor(_ROOT / ".factory" / "usage" / "providers").inspect(force=False)
        except Exception:
            pass

    if frontier_data and task_type not in ["visual", "visual_synthesis", "image_gen", "diagram"]:
        result["daily_efficiency_frontier"] = frontier_data

    planned_result = _apply_token_plan(
        result,
        task_type,
        complexity,
        task_description,
        expected_steps,
        usage_report,
        remaining_hourly_percent,
        offline,
    )

    # Strictly ban autonomous invocation of Fable and Astra
    if planned_result.get("model") in ("gpt-6-astra", "astra", "claude-fable", "fable"):
        planned_result["model"] = "gpt-sol" if "astra" in planned_result["model"] else "claude-opus-5"

    return planned_result


def main():
    parser = argparse.ArgumentParser(description="Model Router & Execution CLI (2026 Edition)")
    subparsers = parser.add_subparsers(dest="command")

    # Command: list-local
    subparsers.add_parser("list-local", help="List local Ollama models and status")

    # Command: recommend
    rec_parser = subparsers.add_parser("recommend", help="Recommend best model for task")
    rec_parser.add_argument(
        "--task-type",
        choices=[
            "research",
            "topic_research",
            "scout",
            "code_scout",
            "architecture",
            "coding",
            "testing",
            "review",
            "content",
            "content_generation",
            "anti_slop",
            "visual",
            "visual_synthesis",
        ],
        required=True,
    )
    rec_parser.add_argument("--complexity", choices=["low", "medium", "high", "critical"], default="medium")
    rec_parser.add_argument("--offline", action="store_true", help="Force local-only Ollama models")
    rec_parser.add_argument("--task-description", default="", help="Task text used by the token forecaster")
    rec_parser.add_argument("--expected-steps", type=int, default=1)
    rec_parser.add_argument("--remaining-hourly-percent", type=float)
    rec_parser.add_argument("--no-quota-scan", action="store_true", help="Skip inspection of configured accounts")


    # Command: call-local
    call_parser = subparsers.add_parser("call-local", help="Call a local Ollama model directly")
    call_parser.add_argument("--model", default="qwen-code-fast:latest")
    call_parser.add_argument("--prompt", required=True)
    call_parser.add_argument("--timeout", type=int, default=180, help="Timeout in seconds for model inference")

    # Command: frontier
    subparsers.add_parser("frontier", help="Display latest daily Coding Pareto Frontier")

    # Command: benchmark-status
    subparsers.add_parser("benchmark-status", help="Show daily benchmark execution status and metrics")

    # Command: top3
    top3_parser = subparsers.add_parser("top3", help="Display top 3 speculative candidate models for tier")
    top3_parser.add_argument("--complexity", choices=["low", "medium", "high", "critical", "local_fast"], default="high")
    top3_parser.add_argument("--offline", action="store_true", help="Force local Ollama tier")

    args = parser.parse_args()

    if args.command == "list-local":
        models = list_local_models()
        print(json.dumps(models, indent=2))
    elif args.command == "frontier":
        ledger = ensure_daily_benchmark()
        all_models = list(ledger.models.values())
        summary = build_frontier_summary(ledger.date, all_models)
        print(f"Daily Pareto Frontier for {ledger.date}:")
        for m in summary.coding_frontier:
            print(f"  - {m.model_id:<32} (Score: {m.coding_score}, Cost/Task: ${m.cost_per_task}, Eff: {m.efficiency_score_coding})")
    elif args.command == "benchmark-status":
        ledger = ensure_daily_benchmark()
        print(f"Daily Benchmark Date: {ledger.date}")
        print(f"Total Models: {ledger.total_models_scanned}")
        print(f"Coding Pareto Models: {len(ledger.pareto_coding_models)}")
        print(f"Recommendations: {json.dumps(ledger.recommendations_by_tier, indent=2)}")
    elif args.command == "top3":
        ledger = ensure_daily_benchmark()
        all_models = list(ledger.models.values())
        tier_key = "local_fast" if args.offline else args.complexity
        top3_list = get_top_candidates_for_tier(all_models, tier=tier_key, k=3)
        print(json.dumps([c.to_dict() for c in top3_list], indent=2))
    elif args.command == "recommend":
        usage_report = None
        if not args.offline and not args.no_quota_scan:
            try:
                from core.usage.monitor import AccountUsageMonitor

                usage_report = AccountUsageMonitor(
                    _ROOT / ".factory" / "usage" / "providers"
                ).inspect()
            except Exception as exc:
                print(f"[WARN] Quota scan unavailable: {exc}", file=sys.stderr)
        rec = recommend_model(
            args.task_type,
            args.complexity,
            args.offline,
            task_description=args.task_description,
            expected_steps=max(1, args.expected_steps),
            usage_report=usage_report,
            remaining_hourly_percent=args.remaining_hourly_percent,
        )
        print(json.dumps(rec, indent=2))
    elif args.command == "call-local":
        payload = {
            "model": args.model,
            "prompt": args.prompt,
            "stream": False
        }
        started = time.perf_counter()
        res = query_ollama("/api/generate", payload, timeout=args.timeout)
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        try:
            ModelUsageLedger(_ROOT / ".factory" / "usage").record(ModelCallEvent(
                provider="ollama",
                model=str(res.get("model") or args.model),
                tier=ModelTier.LOCAL,
                harness="model_router_cli",
                modality=ModelModality.TEXT,
                success="error" not in res,
                input_tokens=res.get("prompt_eval_count"),
                output_tokens=res.get("eval_count"),
                cost_usd=0.0,
                latency_ms=latency_ms,
                source="core.router.call_local",
            ))
        except Exception as exc:
            print(f"Telemetry warning: {exc}", file=sys.stderr)
        if "error" in res:
            print(f"Error: {res['error']}", file=sys.stderr)
            sys.exit(1)
        print(res.get("response", ""))
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
