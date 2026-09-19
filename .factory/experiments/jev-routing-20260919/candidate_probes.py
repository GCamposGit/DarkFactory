"""Probe Jev on candidate choice after deterministic eligibility filtering."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
MODEL = "typesafe/jev-1.13"
OUTPUT_PATH = Path(__file__).with_name("candidate_results.json")

PROBES = [
    {
        "id": "economy_local_first",
        "expected": "qwen-code-fast:latest",
        "state": {
            "task": "Implementar correção localizada de UTF-8 em CLI Python, contrato e teste focal prontos.",
            "role": "economy",
            "policy": "Preferir primeiro executor local qualificado e disponível; modelos cloud só se local não servir.",
            "eligible_candidates": [
                {"id": "qwen-code-fast:latest", "provider": "ollama", "available": True, "cost_usd": 0, "catalog_order": 1},
                {"id": "qwen-code-deep:latest", "provider": "ollama", "available": True, "cost_usd": 0, "catalog_order": 2},
                {"id": "google/gemini-3.8-flash", "provider": "google", "available": True, "catalog_order": 3},
            ],
        },
        "criteria": {
            "qwen-code-fast:latest": "Executor local qualificado, disponível e primeiro na ordem do catálogo.",
            "qwen-code-deep:latest": "Executor local qualificado, disponível e segundo na ordem do catálogo.",
            "google/gemini-3.8-flash": "Executor cloud qualificado, disponível e terceiro na ordem do catálogo.",
        },
    },
    {
        "id": "architecture_quota_failover",
        "expected": "deepseek/deepseek-v4-pro",
        "state": {
            "task": "Planejar contrato transacional de reservas de orçamento e idempotência após crash para três projetos.",
            "role": "high_architecture",
            "policy": "Piso de alta arquitetura obrigatório. Claude Opus é primário mas está indisponível por cota; selecionar primeiro fallback qualificado disponível. Nunca degradar para economy.",
            "ineligible_primary": {"id": "anthropic/claude-opus-5", "reason": "quota_exhausted"},
            "eligible_candidates": [
                {"id": "deepseek/deepseek-v4-pro", "available": True, "catalog_order": 2},
                {"id": "google/gemini-3.1-pro", "available": True, "catalog_order": 3},
            ],
        },
        "criteria": {
            "deepseek/deepseek-v4-pro": "Modelo qualificado de alta arquitetura, disponível, primeiro fallback elegível.",
            "google/gemini-3.1-pro": "Modelo qualificado de alta arquitetura, disponível, segundo fallback elegível.",
        },
    },
    {
        "id": "independent_review_family",
        "expected": "gpt-review:latest",
        "state": {
            "task": "Revisar patch implementado por DeepSeek sem alterar o código; buscar contraexemplos reproduzíveis.",
            "role": "verifier",
            "implementer_family": "deepseek",
            "policy": "Revisor deve ser de outra família; preferir primeiro revisor local qualificado disponível.",
            "ineligible_candidate": {"id": "deepseek/deepseek-v4.1-flash", "reason": "same_family_as_implementer"},
            "eligible_candidates": [
                {"id": "gpt-review:latest", "family": "gpt-oss", "provider": "ollama", "available": True, "catalog_order": 1},
                {"id": "xai/grok-4.6", "family": "grok", "provider": "xai", "available": True, "catalog_order": 3},
            ],
        },
        "criteria": {
            "gpt-review:latest": "Revisor local qualificado, independente da família implementadora, primeiro elegível na ordem do catálogo.",
            "xai/grok-4.6": "Revisor cloud qualificado, independente da família implementadora, fallback posterior.",
        },
    },
]


def main() -> None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY unavailable")
    records = []
    for probe in PROBES:
        payload = {
            "model": MODEL,
            "state": probe["state"],
            "questions": {
                "executor": {
                    "type": "choice",
                    "instructions": "Qual único executor elegível corresponde à política e à ordem do catálogo neste estado? Use apenas eligible_candidates.",
                    "criteria": probe["criteria"],
                }
            },
        }
        request = urllib.request.Request(
            ENDPOINT,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        record = {
            "id": probe["id"],
            "expected": probe["expected"],
            "state": probe["state"],
            "answer": result["answers"]["executor"],
            "model": result.get("model"),
            "usage": result.get("usage"),
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        records.append(record)
        print(json.dumps({"id": record["id"], "choice": record["answer"]["choice"], "expected": record["expected"], "probabilities": record["answer"]["probabilities"]}, ensure_ascii=False), flush=True)
    OUTPUT_PATH.write_text(json.dumps({"created_at": datetime.now(UTC).isoformat(), "records": records}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
