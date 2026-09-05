#!/usr/bin/env python3
"""
Model Router CLI & Engine (2026 Edition)
Routes tasks dynamically between:
- Local Ollama Cluster (qwen-fast, qwen-deep, gpt-review) -> Cost $0, Latency 0ms
- Antigravity Native Engine (Gemini 3.8 Flash) -> High context, 1M+ tokens
- Cloud Frontier Tier (Grok 4.6, Claude 3.7 Sonnet, DeepSeek-R1, Qwen3-Max)
"""

import sys
import json
import argparse
import urllib.request
import urllib.error
from typing import Dict, Any, Optional
from pathlib import Path

# Ensure root directory in sys.path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.benchmarks.fetcher import ensure_daily_benchmark
from core.benchmarks.frontier import (
    select_best_model_for_task,
    build_frontier_summary,
    get_top_candidates_for_tier,
)

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
        "secondary": "claude-3.7-sonnet",
        "secondary_provider": "anthropic",
        "local_fallback": "qwen-code-fast:latest",
        "notes": "Gemini 3.8 Flash for repository tree inspection & AST checks; Claude 3.7 Sonnet for component reuse safety & license audits."
    },
    "architecture": {

        "primary": "claude-3.7-sonnet",
        "primary_provider": "anthropic",
        "secondary": "deepseek-r1",
        "secondary_provider": "siliconflow",
        "local_fallback": "qwen-code-deep:latest",
        "notes": "Claude 3.7 Sonnet w/ Thinking for PRD and strict non-goals; DeepSeek-R1 for cost-effective deep reasoning."
    },
    "coding": {
        "high_complexity": {
            "primary": "claude-3.7-sonnet",
            "primary_provider": "anthropic",
            "secondary": "deepseek-v4-pro",
            "secondary_provider": "siliconflow",
            "local_fallback": "qwen-code-deep:latest",
        },
        "medium_complexity": {
            "primary": "deepseek-v4-pro",
            "primary_provider": "siliconflow",
            "secondary": "qwen-code-deep:latest",
            "secondary_provider": "ollama",
            "local_fallback": "qwen-code-deep:latest",
        },
        "low_complexity": {
            "primary": "qwen-code-fast:latest",
            "primary_provider": "ollama",
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
        "tier2_cloud": "deepseek-r1",
        "tier2_provider": "siliconflow",
        "tier2_alt": "grok-4.6",
        "notes": "Cross-model review rule: The reviewer MUST belong to a different model family than the implementer."
    },
    "content_generation": {
        "primary": "claude-3.7-sonnet",
        "primary_provider": "anthropic",
        "secondary": "gemini-3.8-flash",
        "secondary_provider": "antigravity",
        "local_fallback": "qwen-code-deep:latest",
        "notes": "Claude 3.7 Sonnet for high-signal, anti-slop prose; Gemini 3.8 Flash for high-speed drafting; Qwen-deep for $0 local offline."
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
        "primary": "gemini-2.5-flash-image",
        "primary_provider": "openrouter",
        "secondary": "dall-e-3",
        "secondary_provider": "openai",
        "local_fallback": "darkfac-vector-v1",
        "notes": "OpenRouter / Gemini 2.5 Flash Image / DALL-E 3 for frontier visuals; Pillow procedural vector engine for $0 offline diagrams & banners."
    }
}

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

def recommend_model(task_type: str, complexity: str = "medium", offline: bool = False) -> Dict[str, Any]:
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
            return {
                "model": "gpt-oss-clean:latest",
                "provider": "ollama",
                "mode": "offline",
                "reason": "Local GPT-OSS 20B limpo com 16k de contexto, 20 threads e sem prompt prévio ($0 custo)."
            }
        elif complexity in ["high", "critical"] or task_type == "architecture":
            return {
                "model": "qwen-code-deep:latest",
                "provider": "ollama",
                "mode": "offline",
                "reason": "Local Qwen3-Coder 30B MoE com contexto de 16k e 20 threads (sem prompt)."
            }
        else:
            return {
                "model": "qwen-code-fast:latest",
                "provider": "ollama",
                "mode": "offline",
                "reason": "Local Qwen3-Coder 30B fast worker em 8 P-Cores e contexto 4k (sem prompt)."
            }

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

        # If daily benchmark identified a Pareto leader with superior cost-benefit, highlight it
        if frontier_data and "optimal_cloud_model" in frontier_data:
            opt_model = frontier_data["optimal_cloud_model"]
            if "deepseek" in opt_model:
                primary_model = opt_model.split("/")[-1]
                primary_provider = "siliconflow"
            elif "claude" in opt_model:
                primary_model = opt_model.split("/")[-1]
                primary_provider = "anthropic"
            elif "openai" in opt_model or "gpt" in opt_model:
                primary_model = opt_model.split("/")[-1]
                primary_provider = "openai"
            elif "gemini" in opt_model:
                primary_model = opt_model.split("/")[-1]
                primary_provider = "google"
            elif "qwen" in opt_model:
                primary_model = opt_model.split("/")[-1]
                primary_provider = "qwen"

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
        result = {
            "model": MODEL_MATRIX["visual_synthesis"]["primary"],
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

    if frontier_data:
        result["daily_efficiency_frontier"] = frontier_data

    return result

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
        rec = recommend_model(args.task_type, args.complexity, args.offline)
        print(json.dumps(rec, indent=2))
    elif args.command == "call-local":
        payload = {
            "model": args.model,
            "prompt": args.prompt,
            "stream": False
        }
        res = query_ollama("/api/generate", payload, timeout=args.timeout)
        if "error" in res:
            print(f"Error: {res['error']}", file=sys.stderr)
            sys.exit(1)
        print(res.get("response", ""))
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
