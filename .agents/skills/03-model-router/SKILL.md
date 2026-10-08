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

### Preferência pelo Harness de Operação (USR-109)
- No estágio `development`, o roteador (`core.line.routing.pick`) elege o harness pelo qual o usuário está operando a fábrica sempre que ele estiver elegível (cota > 15%, sem cooldown, modo `write` declarado), à frente do maior headroom e da preferência por complexidade. Se não estiver elegível, vale o Dynamic Headroom. Detalhes e precedência da detecção na skill 19-run-ticket (seção 2b).

### Portões, Política e Validação
- **Conformidade com GatePolicy**: O modelo e o papel atribuídos devem pertencer aos papéis habilitados na política do supervisor (`GatePolicy.is_role_enabled(role)`).
- **Proibição de Fable como Default**: Modelos caros exigem justificativa empírica de Pareto; Fable não é rota de contingência padrão.
- **Fail-Closed em Falta de Orçamento**: Se nenhuma rota qualificada couber no saldo autorizado, emita formalmente o estado `WorkflowState.WAITING_BUDGET` com diagnóstico das contas consultadas, sem comprar créditos ou travar em loop.

---

## 2. Configuração Dinâmica de Roteamento

A fonte única e canônica de modelos, provedores, timeouts e cascatas para a esteira é `.factory/config/line_routing.json`, consumida por `core.line.routing.pick`. Exemplos ilustrativos de despacho por complexidade:

| Tipo de Tarefa | Complexidade | Perfil Típico | Provedor / Endpoint |
| :--- | :--- | :--- | :--- |
| **Ingestão & Mapeamento** | Qualquer | Gemini 3.8 Flash | Antigravity / Google |
| **Pesquisa Web & Docs Vivos** | Média / Alta | Grok 4.6 | xAI |
| **Arquitetura & PRD** | Alta / Crítica | Claude 3.7 Sonnet / Opus | Anthropic |
| **Implementação de Código** | Alta / Média | Claude 3.7 Sonnet / Codex | Anthropic / OpenAI |
| **Tarefas Rápidas / Boilerplate** | Baixa | Qwen Fast / Deep | Ollama (Local $0) |
| **Auditoria Local Nível 1** | Local | GPT Review / Qwen | Ollama (Local $0) |
| **Auditoria Cruzada Nível 2** | Independente | Família distinta do coder | xAI / DeepSeek / Google |

---

## 2b. Subagentes de desenvolvimento: elegibilidade do Haiku 5.5

Esta regra vale para SUBAGENTES do harness (Agent tool com `model` haiku, sonnet ou opus). Ela não altera o cascade da esteira (`.factory/config/line_routing.json`) nem `core.line.routing.pick`. Implementação pura em `core/line/subagent_routing.py` (`pick_subagent_model`), parâmetros em `.factory/config/subagent_model_routing.json` (modelos: Haiku `claude-haiku-5-5`, Sonnet `claude-sonnet-5-5`, Opus `claude-opus-5-5`).

| Situação | Modelo | Condições |
| :--- | :--- | :--- |
| **Elegível ao Haiku** | `claude-haiku-5-5` | TODAS: (1) `task_type` em mechanical_edit (rename, format, lint, typing), docs_sync, test_from_spec, config_data, boilerplate_from_template, test_run_distill, ledger_update; (2) complexidade `low` (docs_sync e test_run_distill também `medium`); (3) no máximo 3 arquivos e 150 linhas alteradas; (4) critério de aceite executável e rápido pré-definido pelo planejador; (5) ambiguidade resolvida (Gate G1 decidido, handoff aprovado); (6) zero falhas anteriores do Haiku na tarefa |
| **Proibido ao Haiku (vai ao Sonnet)** | `claude-sonnet-5-5` | QUALQUER: caminho protegido por governança (`core.orchestrator.guard.audit_paths`); tag de risco (security, credentials, auth, payments, concurrency, locking, transactions, migration, data_deletion, public_contract, routing, quota, flaky_test, root_cause_debug, architecture); estágio grill, review ou integration; sem aceite executável; ambiguidade pendente; arquivos ou linhas acima do limite; tipo ou complexidade fora da lista |
| **Planejamento** | `claude-opus-5-5` | Estágio planning segue a regra existente (Opus); Haiku nunca planeja, revisa nem resolve conflito de integração |
| **Escalonamento** | Haiku, depois Sonnet | UMA tentativa Haiku. Se o portão focado falhar ou a revisão apontar defeito de correção, nova tentativa com Sonnet (nunca 3 iterações com Haiku). Haiku nunca escala sozinho para Opus nem Fable |

Invariantes inalteradas: piso de cota de $15\%$ (o Haiku consome a mesma conta Anthropic), revisão sempre em família diferente do implementador (o revisor nunca é Haiku), portão único `python core/harness/runner.py --quick` e `forbidden_autonomous_models` (fable, claude-fable, astra, gpt-6-astra) continuam proibidos. Qualquer entrada inválida resolve para Sonnet (fail-closed).

---

## 3. Continuous Self-Improvement & RCA de Roteamento

- **RCA em Falhas de Inferência**: Se um modelo produzir falhas de sintaxe repetidas ou alucinações, registre o RCA via `python -m core.learning.cli rca` e recalibre a classe de complexidade ou ordem de fallback na configuração.

---

## 4. Binding com a Esteira (HF-27)

- **Módulo na Linha**: `core.line.routing` (`core/line/routing.py`) com configuração única em `.factory/config/line_routing.json`.
- **Capacidades e Modos**: O roteador respeita estritamente `core.line.agent_cli.HARNESS_CAPABILITIES`. O Antigravity é configurado apenas com modo de leitura (`read`) e nunca é eleito para estágios que exigem escrita de código (`development`).
- **Dynamic Headroom & Piso Crítico**: Seleção automática pelo maior headroom com bloqueio preventivo (fail-closed) para contas com cota $\le 15.0\%$.
- **Harness de Operação (USR-109)**: Quando o operador utiliza um harness específico, ele tem preferência no estágio de desenvolvimento se sua cota for saudável (> 15%) e possuir capacidade de escrita.

