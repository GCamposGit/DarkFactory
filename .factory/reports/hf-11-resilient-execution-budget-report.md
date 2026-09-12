# Relatório de Conclusão: HF-11 — Execução Local com Fallback Graceful para Nuvem; Resiliência de Provedores e Orçamento com Tolerância a Falhas

- **Ticket**: `HF-11`
- **Data**: 11/09/2026
- **Status**: `COMPLETED`
- **Governança**: `HYBRID_WORKFLOW_PLAN_2026-09-08` (Seções 2, 5, 9, 12, linha 263) e `HYBRID_AUTONOMY_REQUIREMENTS` (Seções 4, 5, 7, Cenários G6 e G8)
- **Reúso / Complemento**: `DF-12/13/14/17/20`, Skills 03, 04, 05, 12 e 14
- **Módulos Afetados**: `core.execution.resilience`, `core.execution.budget`, `core.execution.agent_executor`, `core.execution.providers`, `core.orchestrator.delivery_executor`, `core.execution.cli`, `tests/test_hf11_resilient_execution_budget.py`, `docs/handoffs/HF-11.md`

---

## 1. Contexto e Objetivos

O ticket **HF-11** resolve o gargalo de resiliência e continuidade operacional da esteira autônoma de desenvolvimento da Dark Factory. 

Anteriormente, falhas de conectividade ou indisponibilidade do motor local Ollama (`localhost:11434`) interrompiam abruptamente as tarefas dos agentes, e chamadas em nuvem sofriam riscos de falha por rate-limiting ou estouros acidentais de cota financeira. 

O HF-11 introduz uma arquitetura em malha resiliente: execução primária local com custo zero ($0.00), desvio automático e transparente para rotas em nuvem na fronteira de Pareto, proteção por Circuit Breakers dedicados por provedor, retries exponenciais para erros transitórios, blindagem do orçamento antes de despachos pagos, imunidade contra troca de modelos mid-job (Cenário G6), além do reconciliador de entrega remota e verificador de checks DF-20.

---

## 2. Entregas e Invariantes Comprovadas

### 2.1. Execução Local-First ($0.00) com Fallback Graceful
- `ResilientModelProvider` opera por padrão direcionando prompts a executores locais ($0.00 de custo medido).
- Caso o Ollama esteja offline, indisponível, demore além do timeout ou recuse conexão:
  - O erro é capturado e classificado deterministicamente via `classify_error` (`CONNECTION_REFUSED`, `TIMEOUT`, `OUT_OF_MEMORY`, etc.).
  - Emite o registro de auditoria imutável `FallbackEvent` contendo latência, causa-raiz, modelo de origem e modelo de destino.
  - Desvia graciosamente a execução para o provedor em nuvem (`OpenRouterModelProvider`), mapeando a complexidade da tarefa para o modelo ótimo da fronteira de Pareto (`qwen3-8-flash-next`, `deepseek-v4-pro`, `claude-3.7-sonnet`).
  - Anexa metadados auditáveis à resposta: `fallback_triggered: "true"`, `fallback_from_provider`, `fallback_reason`.

### 2.2. Invariante Anti-Fable (Cenário G6)
- Bloqueio terminante de modelos `fable-5.1`:
  - `FORBIDDEN_FALLBACK_MODELS` impede que Fable seja configurado ou resolvido como fallback.
  - Solicitação direta de Fable como modelo primário levanta imediatamente `ValueError` de governança, garantindo custos previsíveis e evitando dependência de rotas não econômicas.

### 2.3. Circuit Breaker com Probes Half-Open
- Cada provedor (`ollama`, `cloud`) possui sua instância de `CircuitBreaker`.
- Após 3 falhas consecutivas, o circuito abre (`OPEN`).
- Enquanto `OPEN`, chamadas locais são curto-circuitadas para a nuvem em 0ms, sem aguardar timeouts de rede que atrasariam o fluxo.
- Após o tempo de resfriamento (`recovery_timeout_seconds`), o circuito transiciona para `HALF_OPEN`. Um probe canary bem-sucedido restaura o circuito para `CLOSED`; nova falha reabre o circuito imediatamente.

### 2.4. Retries Exponenciais e Proteção Transitória
- Erros de rede efêmeros (HTTP 429 Too Many Requests, HTTP 500/502/503/504 e timeouts transitórios) executam automaticamente até 2 retries com backoff exponencial (`initial_backoff_seconds * 2^attempt`), preservando a continuidade operacional sem acionar o supervisor humano.

### 2.5. Estabilidade Mid-Job de Modelos (Cenário G6)
- `JobModelPin` vincula o modelo, provedor e nível de esforço de raciocínio no instante de criação do job.
- Atualizações de benchmark diário em background (`ensure_daily_benchmark()`) não trocam nem alteram a configuração de tarefas em execução, garantindo determinismo reproduzível.

### 2.6. Orçamento com Reserva Dinâmica e Liberação Segura
- `ExecutionBudgetManager.upgrade_reservation`:
  - Ao realizar fallback de um modelo local ($0) para nuvem, a reserva é atualizada atomicamente no SQLite, checando previamente o saldo disponível no teto da tarefa e nas janelas rolantes (`short_window`, `long_window`).
  - Se a reserva exceder o saldo permitido, a chamada é bloqueada (`BudgetExceededError`), assegurando o princípio fail-closed.
- `release_if_active` em `AgentExecutor.execute_step`:
  - Falhas não tratadas na inferência garantem a liberação imediata da reserva ativa no SQLite, prevenindo bloqueios órfãos perpétuos.
- Replay idempotente de attempts no commit impede contabilidade duplicada de custos.

### 2.7. Reconciliação e Executor de Entrega Remota DF-20
- `RemoteDeliveryReconciler`:
  - Compara o commit local do candidato com o head SHA remoto do Pull Request no GitHub.
  - Rejeita checks de CI antigos (`stale_checks`) pertencentes a commits anteriores.
  - Enfileira na `MergeQueue` e valida que a conclusão remota confirma o estado operacional sem confundir merge com prova isolada de saúde em produção (Cenário G8).

---

## 3. Matriz de Testes e Evidências

| Teste | Escopo / Invariante | Resultado |
|---|---|---|
| `test_local_first_execution_success_at_zero_cost` | Execução local a $0.00 de custo e circuito CLOSED | PASSED |
| `test_local_failure_triggers_graceful_cloud_fallback` | Fallback gracioso automático para nuvem com telemetria | PASSED |
| `test_anti_fable_invariant_scenario_g6` | Cenário G6: Fable-5.1 terminantemente bloqueado | PASSED |
| `test_circuit_breaker_tripping_and_immediate_bypass` | Curto-circuito após 3 falhas e desvio imediato | PASSED |
| `test_circuit_breaker_half_open_recovery` | Recuperação com probe canary em HALF_OPEN | PASSED |
| `test_circuit_breaker_half_open_failure_reopens` | Falha na sonda canary reabre o circuito para OPEN | PASSED |
| `test_dynamic_reservation_upgrade_and_ceiling_check` | Reserva dinâmica no fallback e bloqueio em teto excedido | PASSED |
| `test_safe_reservation_cleanup_on_unhandled_failure` | Liberação garantida de reservas ativas em falhas de geração | PASSED |
| `test_mid_job_model_pin_stability_scenario_g6` | Cenário G6: imutabilidade de modelo no meio do job | PASSED |
| `test_transient_cloud_error_retries_with_backoff` | Retries automáticos com backoff para 429/503 | PASSED |
| `test_idempotent_attempt_replay_does_not_double_count_spend` | Replay idempotente de commit sem dupla cobrança financeira | PASSED |
| `test_remote_delivery_reconciler_checks_and_sha_validation` | DF-20: validação de SHA, bloqueio de checks obsoletos | PASSED |
| `test_get_model_provider_factory_resilient` | Factory cria provedor resiliente configurado | PASSED |
| `test_cli_circuit_status_and_execute_headless` | Diagnóstico de circuitos e CLI headless com `--json` | PASSED |

---

## 4. Validação do Portão Oficial

- **Comando**: `python core/harness/runner.py --quick`
- **Total de Testes**: **781 descobertos** (779 passaram, 2 skipped, 0 falhas)
- **Status do Portão**: **`[HARNESS_PASS]`**
- **Exit Code**: `0`

---

## 5. Próximo Sucessor

O próximo ticket na sequência do Plano Híbrido é **HF-12** (Build, staging, aceite, produção, smoke, rollback e backups exercitados).
