#!/usr/bin/env python3
"""
Benchmark real para validar GPT-OSS 20B na versão limpa e otimizada.
"""

import sys
import json
import time
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TEST_PROMPT = """Analise o seguinte trecho de código Python e aponte eventuais riscos de concorrência, contenção de recursos ou vazamento de memória. Seja objetivo e técnico:

```python
import threading
import time

class SharedResourcePool:
    def __init__(self, max_items: int = 100):
        self.max_items = max_items
        self.items = []
        self._lock = threading.Lock()

    def add(self, item: str) -> bool:
        if len(self.items) < self.max_items:
            time.sleep(0.001)
            self.items.append(item)
            return True
        return False
```"""

def run_test():
    model_name = "gpt-oss-clean:latest"
    print(f"\n" + "=" * 60)
    print(f" Testando modelo: {model_name} (GPT-OSS 20B Limpo)")
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
            print("\nPreview da Análise Gerada:")
            lines = data.get("response", "").strip().split("\n")
            for line in lines[:18]:
                print(f"   {line}")
            if len(lines) > 18:
                print("   ...")
    except Exception as e:
        print(f"Erro no teste: {e}")

if __name__ == "__main__":
    run_test()
