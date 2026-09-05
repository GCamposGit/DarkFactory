#!/usr/bin/env python3
"""
Benchmark real para validar velocidade, latência e neutralidade das novas versões limpas.
"""

import sys
import json
import time
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TEST_PROMPT = """Escreva uma classe Python chamada `TokenBucket` com tipagem estrita para controle de taxa (rate limiting).
Deve incluir métodos `consume(tokens: int) -> bool` e `refill()`. Retorne apenas o código."""

MODELS = ["qwen-code-fast:latest", "qwen-code-deep:latest"]

def run_test(model_name: str):
    print(f"\n" + "=" * 60)
    print(f" Testando modelo: {model_name}")
    print("=" * 60)

    url = "http://localhost:11434/api/generate"
    payload = {
        "model": model_name,
        "prompt": TEST_PROMPT,
        "stream": False
    }

    start = time.perf_counter()
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            elapsed = time.perf_counter() - start

            eval_count = data.get("eval_count", 0)
            eval_duration_ns = data.get("eval_duration", 1)
            eval_rate = eval_count / (eval_duration_ns / 1e9) if eval_duration_ns > 0 else 0

            prompt_count = data.get("prompt_eval_count", 0)
            prompt_duration_ns = data.get("prompt_eval_duration", 1)
            prompt_rate = prompt_count / (prompt_duration_ns / 1e9) if prompt_duration_ns > 0 else 0

            print(f" -> Tempo Total: {elapsed:.2f}s")
            print(f" -> Tokens Gerados: {eval_count} tokens")
            print(f" -> Velocidade de Geração: {eval_rate:.2f} tokens/s")
            print(f" -> Velocidade do Prompt: {prompt_rate:.2f} tokens/s")
            print("\nPreview da Resposta:")
            code_lines = data.get("response", "").strip().split("\n")
            for line in code_lines[:12]:
                print(f"   {line}")
            if len(code_lines) > 12:
                print("   ...")
    except Exception as e:
        print(f"Erro no teste: {e}")

if __name__ == "__main__":
    for m in MODELS:
        run_test(m)
