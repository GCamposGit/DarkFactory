---
name: specialized-test-subagent
description: Delega a execução, monitoramento e sumarização de suítes de testes a subagentes rápidos e baratos (flash_lite, haiku, grok-low, luna-low ou qwen-fast local a $0). Isola logs volumosos em disco (.factory/test_logs/) e sintetiza relatórios destilados (DistilledTestReport) para o contexto principal, evitando poluição de tokens e acelerando loops de iteração em qualquer harness (Antigravity, Codex, Grok, Claude Code).
---

# Specialized Test Subagent: Execução e Destilação Multi-Harness

O *Specialized Test Subagent* é a arquitetura headless projetada para isolar a execução de testes em subagentes eficientes e econômicos, protegendo a janela de contexto do agente orquestrador principal contra centenas de linhas de logs, stack traces e fixtures verbosas.

> **Princípio Central**: O agente solicitante precisa saber **o que falhou, onde falhou e por quê**, nunca percorrer centenas de linhas de logs brutos. O log integral é persistido em disco para auditoria perpétua (`.factory/test_logs/<run_id>.log`).

---

## 1. Regra Clara: Modelos Locais vs. Modelos "Light" do Harness

A escolha entre rodar no modelo local ou acionar o modelo *light* da nuvem segue uma regra determinística e inegociável:

| Dimensão | **Modelo Local (`localhost:11434` - $0)** | **Modelo "Light" do Harness (Nuvem)** |
| :--- | :--- | :--- |
| **Modelos Recomendados** | `ollama/qwen-code-fast:latest`, `qwen2.5-coder:7b` | `flash_lite` (AGY), `haiku` (Claude), `grok low` (Grok), `luna low` (Codex) |
| **Custo Financeiro** | **$0.00** (Custo zero absoluto, sem consumo de cota) | Micro-centavos / Consumo de cota de assinatura |
| **Ambiente de Execução** | Estação de desenvolvimento do operador com GPU/CPU local | Servidores remotos, CI/CD, contêiner headless ou sem Ollama |
| **Loop de Trabalho** | **Micro-iterações TDD ("Red-Green-Refactor")** repetidas | Validações de marcos de branch, PR review e testes de integração |
| **Janela de Contexto** | Arquivo único ou suíte rápida (<16k tokens) | Suítes extensas, tracebacks longos (>32k-128k tokens) |
| **Privacidade** | Código 100% isolado na máquina (air-gapped/offline) | Envio via endpoint de API criptografado do provedor |
| **Pressão de Cota** | **Obrigatório** sempre que a cota da nuvem estiver >80% | Utilizado quando a cota estiver saudável e exigir velocidade |

### Heurística em Uma Linha:
> *Se o operador estiver em loop de desenvolvimento na máquina local e o Ollama estiver ativo, execute 100% no Local ($0); migre para o modelo Light do harness apenas quando estiver em ambiente remoto/CI, quando a suite exigir grande janela de contexto (>32k) ou quando a cota local de VRAM estiver sob pressão.*

---

## 2. Matriz de Subagentes por Harness

Cada ambiente de desenvolvimento possui um harness próprio com modelos e níveis de esforço otimizados:

### A. Antigravity (Google Core)
- **Modelo Padrão**: `flash_lite` (ultra-rápido, custo insignificante).
- **Modelo Diagnóstico Profundo**: `pro` (para regressões arquiteturais).
- **Invocação**:
  ```python
  invoke_subagent(
      TypeName="self",
      Role="Specialized Test Runner",
      Model="flash_lite",
      Prompt=generate_subagent_prompt(instruction, harness=HarnessType.ANTIGRAVITY)
  )
  ```

---

### B. OpenAI / GPT / Codex
- **Revisão Técnica do Modelo Luna**:
  - O catálogo da DarkFac integra o `openai/gpt-5-6-luna-high` (input \$0.20/M, output \$1.20/M, 123 TPS).
  - **Runner Rotineiro (Recomendado)**: Configurar com **`reasoning_effort: "low"`** (ou modelo `o3-mini low` / `gpt-4o-mini`). Execuções normais de testes são tarefas sintáticas/algorítmicas de parsing de erros; o esforço `low` reduz custos em 40%, oferece TTFT de ~0.45s e elimina pensamentos prolixos.
  - **Escalação para Luna `max` (Deep RCA)**: O modo **Luna max** multiplica o custo por 3.2x gerando cadeias profundas de reflexão formal. **Use Luna max exclusivamente** quando o teste falhar 2x seguidas por problemas complexos de concorrência, deadlocks assíncronos, vazamentos de memória ou quando a causa raiz não for evidente no traceback.
- **Configuração no Codex / OpenAI API**:
  ```json
  // Execução padrão de testes (Light)
  {
    "model": "openai/gpt-5-6-luna-high",
    "reasoning_effort": "low"
  }

  // Deep RCA para falhas complexas recorrentes
  {
    "model": "openai/gpt-5-6-luna-high",
    "reasoning_effort": "max"
  }
  ```

---

### C. Grok / Grok Builder (xAI)
- **Modelo Recomendado**: `xai/grok-4.6` (ou `grok-3-mini` na linha leve).
- **Nível de Esforço**: **`reasoning_effort: "low"`**.
- **Diferencial Grok Build**: Suporte nativo a subagentes em paralelo (**Arena Mode**, até 8 subagentes concorrentes). O Grok Build executa testes nativamente no shell local e valida soluções competidoras antes de propor diffs.
- **Comando de Invocação**:
  ```powershell
  # Execução de teste via Grok Build CLI com esforço low
  grok build --reasoning-effort low --exec "python C:\dev\DarkFac\core\harness\test_subagent.py --target tests/test_foo.py --scope file"
  ```

---

### D. Claude Code (Anthropic)
- **Modelo Recomendado**: `claude-3-5-haiku` (ou `claude-haiku-4-5`).
- **Nível de Esforço**: Latência ultra-baixa sem *extended thinking*.
  - Se executado sob `claude-3.7-sonnet`, desabilitar ou limitar o thinking (`thinking: { type: "disabled" }` ou `effort: "low"`) para evitar latência em tarefas de execução de comandos.
- **Subagente Declarativo (`.claude/agents/test-runner.md`)**:
  ```yaml
  ---
  name: test-runner
  description: Executa a suíte de testes de forma headless, isola logs volumosos e retorna um DistilledTestReport enxuto.
  model: claude-3-5-haiku-latest
  tools: [Bash, Read, Grep]
  ---
  ```
- **Invocação no Terminal Claude**:
  ```powershell
  claude --model claude-3-5-haiku-latest "python C:\dev\DarkFac\core\harness\test_subagent.py --target tests/test_foo.py --json"
  ```

---

## 3. Contratos de Dados (Reachability Standard)

O módulo [`core/harness/test_subagent.py`](file:///C:/dev/DarkFac/core/harness/test_subagent.py) expõe os contratos Pydantic v2 estritos:

- **`TestScope`**: `ALL`, `QUICK`, `FILE`, `PATTERN`, `FAILED_ONLY`.
- **`HarnessType`**: `ANTIGRAVITY`, `CODEX`, `GROK`, `CLAUDE_CODE`.
- **`ExecutionTier`**: `LOCAL_ZERO_COST`, `LIGHT_HARNESS`, `DEEP_DIAGNOSTIC`.
- **`HarnessRunnerSpec`**: Especifica `harness`, `model_id`, `reasoning_effort`, `execution_tier` e `rationale`.
- **`DistilledTestReport`**:
  - `verdict`: `"PASSED"` | `"FAILED"` | `"ERROR"` | `"TIMEOUT"`.
  - `success`: Booleano determinístico.
  - `passed_count`, `failed_count`, `skipped_count`.
  - `failures`: Lista de `FailedTestItem` com `file`, `line`, `error_type`, `error_message`, `snippet`.
  - `concise_summary`: Resumo em 1 linha.
  - `agent_feedback`: Orientação cirúrgica com arquivo e linha.
  - `raw_log_path`: Caminho absoluto do log isolado em disco.

---

## 4. Modos de Uso

### 1. Como Biblioteca Python (Headless)
```python
from core.harness.test_subagent import (
    TestSubagentEngine,
    TestExecutionInstruction,
    TestScope,
    HarnessType,
    get_harness_test_runner_spec,
)

# Obter spec ótimo para o harness
spec = get_harness_test_runner_spec(HarnessType.CODEX, prefer_local=False, deep_rca=False)
print(f"Using {spec.model_id} with effort={spec.reasoning_effort}")

# Executar testes
engine = TestSubagentEngine()
instruction = TestExecutionInstruction(target="tests/test_auth.py", scope=TestScope.FILE, fail_fast=True)
report = engine.execute(instruction)

if not report.success:
    print(report.agent_feedback)
    print(f"Log detalhado salvo em: {report.raw_log_path}")
```

### 2. Via CLI Standalone
```powershell
# Execução direta com resumo destilado
python C:\dev\DarkFac\core\harness\test_subagent.py --target tests/test_foo.py --scope file -x

# Execução com saída JSON estruturada para integração com scripts
python C:\dev\DarkFac\core\harness\test_subagent.py --target tests/test_foo.py --json
```

### 3. Via DarkHub REST API
```http
POST /api/harness/run-tests HTTP/1.1
Host: 127.0.0.1:8888
Content-Type: application/json

{
  "target": "tests/test_foo.py",
  "scope": "file",
  "timeout_seconds": 60,
  "fail_fast": true
}
```
