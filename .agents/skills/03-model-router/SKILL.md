---
name: model-router
description: Avalia tarefa, complexidade, privacidade, consumo previsto e cota disponível em todas as contas para despachar modelos locais ou de nuvem. Use ao iniciar uma etapa ou subagente e quando houver pressão de tokens, failover de conta ou decisão entre assinatura e API paga.
---

# 03 - Model Router: Roteamento Inteligente & Otimização de Recursos

Esta skill governa o despacho eficiente e custo-efetivo de modelos de linguagem para cada unidade de trabalho da Dark Factory, assegurando cumprimento de orçamentos, isolamento de privacidade e conformidade com os papéis do workflow híbrido.

---

## 1. Contratos Normativos da Etapa

### Inputs (Entradas)
- **Classificação da Tarefa**: Tipo de atividade (`planning`, `coding`, `review`, `research`, `test`) e complexidade (`critical`, `high`, `medium`, `low`).
- **Restrições de Governança**: Requisitos de privacidade (offline/local-first) e teto de orçamento do ticket.
- **Telemetria de Cotas**: Headroom disponível nas contas e status do ledger diário de benchmarks (`.factory/benchmarks/latest.json`).

### Ações e Procedimento Executável
1. **Consulta à Fronteira de Pareto**:
   - Determine a recomendação ótima via CLI headless:
     ```bash
     python core/router/model_router.py recommend --task-type coding --complexity high
     ```
   - Para execução local $0 custo (Ollama):
     ```bash
     python core/router/model_router.py recommend --task-type review --offline
     ```
2. **Avaliação de Pressão de Tokens e Estresse de Cota**:
   - Calcule a projeção de consumo (`expected-steps`, `remaining-hourly-percent`).
   - Sob pressão (>80% de cota consumida), aplique failover para contas alternativas, priorize executores locais (`qwen-fast`, `qwen-deep`) ou acione gateway de API paga com teto delimitado.
3. **Resolução de Papéis e Identidade**:
   - Gere metadados de identidade sanitizados (`SanitizedIdentity`) para vincular ao executor da tarefa.

### Outputs Estruturados
- **Decisão de Roteamento Estruturada**:
  - `model_id`: Identificador real qualificado no provedor.
  - `provider`: Provedor ou endpoint (`Antigravity`, `OpenRouter`, `SiliconFlow`, `Ollama`).
  - `token_budget`: Contrato operacional com teto de tokens, nível de esforço (`low`, `high`, `max`) e estratégia de chunking.
  - `worker_identity`: Instância de `SanitizedIdentity` com `subject`, `role` e `account_ref`.

### Portões, Política e Validação
- **Conformidade com GatePolicy**: O modelo e o papel atribuídos devem pertencer aos papéis habilitados na política do supervisor (`GatePolicy.is_role_enabled(role)`).
- **Proibição de Fable como Default**: Modelos caros exigem justificativa empírica de Pareto; Fable não é rota de contingência padrão.
- **Fail-Closed em Falta de Orçamento**: Se nenhuma rota qualificada couber no saldo autorizado, emita formalmente o estado `WorkflowState.WAITING_BUDGET` com diagnóstico das contas consultadas, sem comprar créditos ou travar em loop.

---

## 2. Matriz de Despacho (Mapeamento Dinâmico 2026)

| Tipo de Tarefa | Complexidade | Modelo Recomendado | Provedor / Endpoint |
| :--- | :--- | :--- | :--- |
| **Ingestão & Mapeamento** | Qualquer | `gemini-3.8-flash` | Antigravity Native |
| **Pesquisa Web & Docs Vivos** | Média / Alta | `grok-4.6` | xAI API |
| **Arquitetura & PRD (Alta)** | Alta / Crítica | `claude-3.7-sonnet` / `deepseek-r1` | Anthropic / SiliconFlow |
| **Implementação Cirúrgica** | Crítica / Alta | `claude-3.7-sonnet` | Anthropic / OpenRouter |
| **Implementação Econômica** | Média | `deepseek-v4-pro` / `qwen-deep` | SiliconFlow / Ollama |
| **Tarefas Rápidas / Boilerplate** | Baixa | `qwen-fast:latest` | Ollama (Local $0) |
| **Auditoria Local Nível 1** | Local | `gpt-review:latest` | Ollama (Local $0) |
| **Auditoria Cruzada Nível 2** | Independente | `deepseek-r1` / `grok-4.6` | SiliconFlow / xAI |

---

## 3. Continuous Self-Improvement & RCA de Roteamento

- **RCA em Falhas de Inferência**: Se um modelo econômico produzir loops de sintaxe ou alucinações repetidas, registre o RCA via `python -m core.learning.cli rca` e eleve deterministicamente a classe de complexidade da tarefa na matriz.
