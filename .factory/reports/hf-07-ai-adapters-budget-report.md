# HF-07 — Relatório de Adaptadores de IA, Despacho por Host/Cota e Orçamento Reconciliado

## Resumo

- **Ticket**: `HF-07`
- **Parent**: `HF-07`
- **Status**: **CONCLUÍDO (100% Validado Deterministicamente)**
- **Escopo**: Reconciliação financeira rigorosa (saldo vs limite vs mês vs acumulado), mapeamento normativo de modelos de alta inteligência por harness, sanitização de esforço de raciocínio, remoção de Fable default e failover de cota.
- **Ambiente**: Python 3.12, Windows 11, Worktree Local.

---

## 1. Entregas e Modificações

### 1.1. Reconciliação Financeira de Créditos e Uso (HF-07 / Seção 2)
- Resolução da pendência documental da Seção 2 do Plano Híbrido:
  - `ProviderCreditCard` em `core/usage/api_credits.py` agora expõe formalmente quatro grandezas distintas:
    1. `available_credit_usd`: saldo líquido restante para consumo.
    2. `credit_limit_usd`: limite nominal atribuído à chave de API.
    3. `current_month_spend_usd`: consumo real no ciclo mensal vigente (`usage_monthly` da API OpenRouter).
    4. `cumulative_spend_usd`: consumo acumulado total histórico da chave (`usage` da API OpenRouter).
  - O método `inspect_openrouter()` consome os dados do endpoint `/api/v1/auth/key` mapeando com precisão `usage_monthly` e `usage`.

### 1.2. Mapeamento de Alta Inteligência por Harness (Seção 4)
- Adicionada a tabela canônica `HIGH_INTELLIGENCE_BY_HARNESS` em `core/router/model_router.py`:
  - **Antigravity**: `gemini-3.8-flash` (Google).
  - **Grok Build**: `grok-4.6` (xAI).
  - **Claude Code**: `opus-5.1` (Anthropic).
  - **Codex**: `gpt-6-astra` (OpenAI).

### 1.3. Governança de Esforço de Raciocínio (Reasoning Effort)
- Implementada a função `sanitize_reasoning_effort(effort, complexity)`:
  - Esforço `high` como padrão para tarefas de alta inteligência e planejamento.
  - Esforço `max` para alta complexidade ou tarefas críticas justificadas.
  - Sanitização automática de rótulos inválidos (`xhigh`, `extra-high`, `ultra`) para `max`, impedindo o envio de parâmetros não suportados aos adaptadores.

### 1.4. Invariante Anti-Fable (Cenário G6)
- Verificado e comprovado por testes automatizados que `Fable-5.1` **não** consta como modelo padrão ou rota de fallback na fábrica.
- Rotas de contingência utilizam a fronteira de Pareto (DeepSeek-V4-Pro, Qwen3-Max/Next) e modelos locais ($0, Ollama Qwen) para garantir previsibilidade de custos.

### 1.5. Despacho Concorrente e Failover por Cota
- A política de estresse em `core/router/token_budget.py` (`plan_token_stress`) avalia os headrooms de todas as contas conectadas.
- Ao identificar estresse horário (<= 15% restante), o despachante transfere dinamicamente a carga para contas mais saudáveis ou para APIs pagas autorizadas (`deepseek`, `openrouter`, `siliconflow`), mantendo o avanço contínuo do workflow.

---

## 2. Validação Determinística

| Suíte / Comando | Testes Executados | Resultado |
| :--- | :--- | :--- |
| `python -m pytest tests/test_model_router_hf07.py -v` | 6 testes dedicados HF-07 | **6 PASSED (100%)** |
| `python -m pytest tests/test_token_budget_router.py -v` | 5 testes de orçamento/stress | **5 PASSED (100%)** |
| `python -m pytest tests/test_benchmark_router.py -v` | 19 testes de roteamento e Pareto | **19 PASSED (100%)** |
| `python -m pytest tests/test_controle_de_cr_ditos_de_a.py -v` | 9 testes de créditos de API | **9 PASSED (100%)** |
| `python -m pytest tests/test_usage_monitor.py -v` | 21 testes de telemetria de uso | **21 PASSED (100%)** |

---

## 3. Próximo Passo

Conforme o caminho crítico da Seção 12 (`HF-01 ⟶ HF-02 ⟶ HF-03/04 ⟶ HF-05 ⟶ HF-06/07 ⟶ HF-08...`), o próximo ticket da fila é **HF-08** (Entrada de demandas, Grill, especificação e planejamento integrado; bootstrap de projeto novo).
