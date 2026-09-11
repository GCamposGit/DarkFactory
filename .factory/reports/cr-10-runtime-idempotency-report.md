# Walkthrough — CR-10: Deduplicação Transacional Completa no HF-05

O ticket **CR-10** foi concluído com sucesso, remediando em definitivo os achados **F08** e **F16** da auditoria crítica.

---

## 1. Alterações Realizadas

### [`core/workflow/runtime.py`](file:///c:/dev/DarkFac/core/workflow/runtime.py)
- **Tabela de Operações Versionada**:
  - Adicionada a tabela `runtime_operations` (com campos `operation_key`, `operation_type`, `subject_id`, `payload_canonical`, `result_json`, `version` e `created_at`) e índice `idx_operations_subject`.
  - Controle de migração transacional via `PRAGMA user_version = 2`.
  - Definido e exportado o contrato Pydantic [`OperationRecord`](file:///c:/dev/DarkFac/core/workflow/runtime.py).
- **Deduplicação Estrita em `transition_run` (F08)**:
  - Consulta `runtime_operations` antes de qualquer verificação ou mutação de estado.
  - Replay idêntico devolve o [`RunRecord`](file:///c:/dev/DarkFac/core/workflow/runtime.py) histórico original sem mutar `runs.state` nem emitir novo evento na `outbox`.
  - Reuso de chave com payload divergente lança [`RuntimeConflictError`](file:///c:/dev/DarkFac/core/workflow/runtime.py) imediatamente, mesmo se o estado atual do run coincidir com o target da transição solicitada.
- **Deduplicação Estrita em `complete_job` (F16)**:
  - Vincula `lease_id`, `worker_id`, `outcome`, `actual_cost`, `charged_cost` e `error` normalizado na chave de operação `lease:{lease_id}:complete`.
  - Replay idêntico devolve o [`JobRecord`](file:///c:/dev/DarkFac/core/workflow/runtime.py) gerado naquela conclusão, preservando o snapshot histórico mesmo se o job passou por novas tentativas posteriores.
  - Tentativas de conclusão com custo, erro ou outcome divergentes são rejeitadas com [`RuntimeConflictError`](file:///c:/dev/DarkFac/core/workflow/runtime.py), protegendo o orçamento de cobranças duplicadas ou indevidas.

### [`tests/test_workflow_runtime.py`](file:///c:/dev/DarkFac/tests/test_workflow_runtime.py)
Adicionados testes determinísticos:
1. `test_old_transition_replay_does_not_reapply_state_mutation_or_outbox`: Verifica ciclo de rework com replay da chave inicial, comprovando que o estado não regride e a outbox permanece intocada.
2. `test_transition_conflicting_key_rejected_even_if_current_state_matches`: Garante que chave conflitante é rejeitada mesmo quando o estado atual coincide com o target.
3. `test_completion_replay_rejects_changed_cost_or_outcome_or_error`: Garante que variações de custo, outcome ou erro lançam `RuntimeConflictError` sem alterar `budget_spent`.
4. `test_completion_replay_preserves_historical_result_across_retries`: Comprova que o replay da tentativa 1 de um job retorna o snapshot original da tentativa 1 sem regredir o job já concluído na tentativa 2.
5. `test_runtime_operations_survive_database_reload`: Garante que as garantias de idempotência e rejeição de conflito sobrevivem ao restart da aplicação / reload do SQLite.
6. `test_transactional_rollback_on_injected_failure`: Demonstra rollback atômico completo se ocorrer falha na transação.
7. `test_two_distinct_operations_generate_two_valid_transitions_and_events`: Teste de controle de duas operações normais distintas.

---

## 2. Resultados de Validação

### Testes Focais
- **Audit Regressions** (`.factory/reviews/hf-critical-20260909/test_runtime_regressions.py`):
  ```
  4 passed in 0.15s (100% PASS - F08 e F16 eliminados)
  ```
- **Suíte Completa do Módulo** (`tests/test_workflow_runtime.py`):
  ```
  20 passed in 0.35s (100% PASS)
  ```
- **Independent Suite Wave 1** (`.factory/reviews/hf-wave1-luna/test_wave1_independent.py`):
  ```
  15 passed in 1.39s (100% PASS)
  ```
- **Spike Suite** (`tests/test_runtime_spike_contracts.py` + `tests/test_runtime_spike_native.py`):
  ```
  13 passed in 4.02s (100% PASS)
  ```
- **Workflow Contracts Suite** (`tests/test_workflow_contracts.py`):
  ```
  13 passed in 0.12s (100% PASS)
  ```
