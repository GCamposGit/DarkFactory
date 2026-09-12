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

# Resolve API Key
api_key = os.environ.get("OPENROUTER_API_KEY")
if not api_key and sys.platform == "win32":
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            api_key, _ = winreg.QueryValueEx(k, "OPENROUTER_API_KEY")
    except Exception:
        pass

if not api_key:
    print("[ERRO] OPENROUTER_API_KEY nao encontrada no ambiente ou registro.")
    sys.exit(1)

api_key = api_key.strip()

# Read Skill Content
skill_path = Path(r"c:\dev\DarkFac\.agents\skills\03-model-router\SKILL.md")
if not skill_path.exists():
    print(f"[ERRO] Arquivo de skill nao encontrado: {skill_path}")
    sys.exit(1)

skill_text = skill_path.read_text(encoding="utf-8")

system_prompt = (
    "Voce e um Arquiteto de Sistemas de IA e Engenheiro Staff especializado em "
    "orquestracao autonoma de agentes, analise de custo-eficiencia de LLMs e "
    "governanca corporativa de software. Sua funcao e realizar uma revisao adversarial "
    "e critica minuciosa de especificacoes normativas e contratos de skills operacionais."
)

user_prompt = f"""Por favor, realize uma revisao e analise critica profunda da Skill abaixo (`03-model-router`), utilizada no ecossistema Dark Factory (uma fabrica autonoma de software).

### Contexto da Dark Factory:
- Opera no Windows com ferramentas locais e em nuvem.
- Prioriza Local-First ($0 custo via Ollama) para micro-tarefas e modelos de fronteira para arquitetura e pesquisa.
- Exige determinismo, contratos estritos Pydantic v2 e conformidade com portoes de validacao.

### Conteudo da Skill:
```markdown
{skill_text}
```

### Instrucoes da Analise Critica:
1. **Pontos Fortes**: O que a skill faz muito bem em termos de desacoplamento e governanca?
2. **Fragilidades e Pontos Cegos**: Onde a especificacao e vaga, excessivamente otimista ou vulneravel a falhas em producao (ex: latencia de rede, cotas de provedores, mudancas de preco)?
3. **Calibracao da Matriz de Despacho**: Como a chegada de novos modelos de alta eficiencia e contexto longo (como voce, DeepSeek-V4.1-Flash com arquitetura CED de 1M de contexto, ou Qwen 2.5/3) impacta ou torna obsoleta a matriz atual?
4. **Recomendacoes Praticas de Aprimoramento**: Sugestoes concretas de adicoes ou ajustes nos contratos (Inputs, Acoes, Outputs e Portões).

Seja direto, tecnico, rigoroso e analitico. Responda em portugues.
"""

payload = {
    "model": "deepseek/deepseek-v4.1-flash",
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ],
    "max_tokens": 3500,
    "temperature": 0.3
}

print("=" * 70)
print("EXECUCAO: ANALISE CRITICA VIA DEEPSEEK-V4.1-FLASH (OPENROUTER)")
print(f"Modelo Alvo: {payload['model']}")
print(f"Tamanho do Prompt: {len(user_prompt)} caracteres")
print(f"Skill Analisada: {skill_path.name}")
print("Enviando requisicao...")
print("=" * 70)

req = urllib.request.Request(
    "https://openrouter.ai/api/v1/chat/completions",
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/DarkFac",
        "X-Title": "DarkFactory Skill Reviewer"
    },
    data=json.dumps(payload).encode("utf-8")
)

t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=90) as response:
        elapsed = round(time.time() - t0, 2)
        raw_body = response.read().decode("utf-8")
        data = json.loads(raw_body)
except urllib.error.HTTPError as e:
    err_body = e.read().decode("utf-8") if hasattr(e, "read") else ""
    print(f"[FALHA HTTP {e.code}]: {err_body}")
    sys.exit(1)
except Exception as e:
    print(f"[FALHA]: {e}")
    sys.exit(1)

choice = data.get("choices", [{}])[0]
msg = choice.get("message", {})
content = msg.get("content", "")
reasoning = msg.get("reasoning", "")
usage = data.get("usage", {})

print(f"\n[SUCESSO] Resposta recebida em {elapsed}s!")
print(f"Model: {data.get('model')}")
print(f"Tokens de Prompt: {usage.get('prompt_tokens', 0)}")
print(f"Tokens de Reasoning: {usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)}")
print(f"Tokens de Resposta: {usage.get('completion_tokens', 0)}")
print(f"Tokens Totais: {usage.get('total_tokens', 0)}")
if "cost" in usage:
    print(f"Custo Estimado: ${usage['cost']:.6f}")

output_dir = Path(r"c:\dev\DarkFac\.factory\reviews")
output_dir.mkdir(parents=True, exist_ok=True)
review_file = output_dir / "deepseek_v4_1_flash_model_router_review.md"

with open(review_file, "w", encoding="utf-8") as f:
    f.write("# Revisão Crítica: Skill 03 - Model Router\n\n")
    f.write(f"- **Modelo Revisor**: `{data.get('model')}`\n")
    f.write(f"- **Tempo de Resposta**: {elapsed}s\n")
    f.write(f"- **Tokens Totais**: {usage.get('total_tokens', 0)} (Prompt: {usage.get('prompt_tokens')}, Completion: {usage.get('completion_tokens')}, Reasoning: {usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)})\n")
    f.write(f"- **Custo**: ${usage.get('cost', 0):.6f}\n\n")
    if reasoning:
        f.write("## Cadeia de Raciocínio (Chain-of-Thought / Reasoning Traced)\n\n")
        f.write(f"> {reasoning.replace(chr(10), chr(10) + '> ')}\n\n")
    f.write("## Relatório da Análise Crítica\n\n")
    f.write(content)
    f.write("\n")

print(f"\nRelatorio salvo em: {review_file}")
print("\n" + "=" * 70)
print("PREVIA DA REVISAO GERADA PELO DEEPSEEK-V4.1-FLASH:")
print("=" * 70)
print(content[:1500] + ("..." if len(content) > 1500 else ""))
