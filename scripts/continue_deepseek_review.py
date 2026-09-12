import os
import sys
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

# Configure stdout encoding for Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

api_key = os.environ.get("OPENROUTER_API_KEY")
if not api_key and sys.platform == "win32":
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            api_key, _ = winreg.QueryValueEx(k, "OPENROUTER_API_KEY")
    except Exception:
        pass

if not api_key:
    print("[ERRO] OPENROUTER_API_KEY nao encontrada.")
    sys.exit(1)

api_key = api_key.strip()
review_file = Path(r"c:\dev\DarkFac\.factory\reviews\deepseek_v4_1_flash_model_router_review.md")
content_prev = review_file.read_text(encoding="utf-8")

system_prompt = (
    "Voce e um Arquiteto de Sistemas de IA e Engenheiro Staff especializado em "
    "orquestracao autonoma de agentes. Continue exatamente de onde o texto foi cortado."
)

continuation_prompt = (
    "Sua analise anterior foi interrompida no final do topico 3a ('A separação...'). "
    "Por favor, continue diretamente a partir deste ponto, concluindo o topico 3 "
    "(Calibracao da Matriz de Despacho frente a novos modelos) e entregando o topico 4 completo "
    "(Recomendacoes Praticas de Aprimoramento nos contratos: Inputs, Acoes, Outputs e Portoes). "
    "Mantenha o mesmo tom cirurgico, rigoroso e analitico."
)

payload = {
    "model": "deepseek/deepseek-v4.1-flash",
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": continuation_prompt}
    ],
    "max_tokens": 3000,
    "temperature": 0.3
}

print("Enviando continuacao...")
req = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/DarkFac",
        "X-Title": "DarkFactory Skill Reviewer Continued"
    },
    data=json.dumps(payload).encode("utf-8")
)

t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=90) as response:
        elapsed = round(time.time() - t0, 2)
        raw_body = response.read().decode("utf-8")
        data = json.loads(raw_body)
except Exception as e:
    print(f"[FALHA]: {e}")
    sys.exit(1)

choice = data.get("choices", [{}])[0]
msg = choice.get("message", {})
cont_content = msg.get("content", "")
usage = data.get("usage", {})

print(f"[SUCESSO] Continuacao recebida em {elapsed}s!")
print(f"Tokens adicionais: {usage.get('total_tokens', 0)}")

# Append continuation cleanly
full_text = content_prev + "\n" + cont_content
review_file.write_text(full_text, encoding="utf-8")
print(f"Arquivo atualizado: {review_file}")
