# Relatório de Conclusão — CR-15: Vincular Aprovação ao Sujeito Esperado do Experimento

**Data:** 11/09/2026  
**Status:** Concluído com Sucesso (100% PASS)  
**Ticket:** CR-15 (Trilha Complementos Locais / HF-02 Efeitos)  
**Achado endereçado:** F11 (falta de vínculo prévio entre o workflow e seu release digest esperado; risco de novo decision ID aceitar release digest conflitante)  
**Documentos canônicos:** `docs/handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md`, `docs/reviews/HF_CRITICAL_REVIEW_2026-09-09.md`, `spikes/runtime_choice/effect_store.py`, `spikes/runtime_choice/effect_server.py`, `tests/test_runtime_spike_effects.py`

---

## 1. Escopo e Invariantes Implementadas

1. **Vínculo Imutável de Sujeito (`bind_approval_subject`)**:
   - Criada a tabela SQLite `approval_subjects(workflow_id TEXT PRIMARY KEY, payload_digest TEXT NOT NULL, bound_at TEXT NOT NULL)`.
   - Método `NativeEffectStore.bind_approval_subject(workflow_id, payload_digest)` registra o digest esperado para o workflow antes de qualquer aprovação.
   - Idempotência estrita: invocar o binding com o mesmo digest é idempotente e retorna sem erro; invocar com digest distinto levanta `ApprovalSubjectConflictError`.

2. **Enforcement em `record_approval`**:
   - `NativeEffectStore.record_approval` consulta `approval_subjects` sob transação imediata (`BEGIN IMMEDIATE`).
   - Se o workflow não possuir subject previamente registrado, rejeita levantando `ApprovalSubjectNotBoundError`.
   - Se o `release_digest` da aprovação diferir do binding registrado, rejeita com `ApprovalSubjectConflictError`, mesmo que seja apresentado um `decision_id` novo.
   - Rejeições falham sem efeito colateral no banco de dados (nenhum registro é inserido em `approvals`).
   - Replay com mesmo `decision_id` e atributos correspondentes retorna o `ApprovalRecord` idêntico. Reuso do mesmo `decision_id` com dados divergentes levanta `ApprovalConflictError`.

3. **Isolamento de Escopo e Proteção de Fronteira HTTP**:
   - Cada workflow possui binding e aprovações independentes no store; o registro ou conflito em um workflow não contamina outros workflows.
   - O servidor HTTP (`EffectServer`) encaminha decisões recebidas via `POST /approvals` ao store, mapeando exceções para códigos HTTP estruturados (`409 APPROVAL_SUBJECT_NOT_BOUND`, `409 APPROVAL_SUBJECT_CONFLICT`, `409 APPROVAL_CONFLICT`).
   - O endpoint de binding (`bind_approval_subject`) **não é exposto via HTTP** ao processo candidato (requisições a `/approval_subjects` retornam 404), garantindo que somente o controlador confiável possa fixar o sujeito esperado.

---

## 2. Arquivos Envolvidos

| Arquivo | Descrição |
|---|---|
| `spikes/runtime_choice/effect_store.py` | Modelo de dados, tabela SQLite `approval_subjects`, método `bind_approval_subject`, validações em `record_approval` e exceções `ApprovalSubjectNotBoundError`, `ApprovalSubjectConflictError`, `ApprovalConflictError`. |
| `spikes/runtime_choice/effect_server.py` | Roteamento e tratamento de erros do servidor HTTP para `/approvals` sem exposição de endpoints de redefinição de binding. |
| `tests/test_runtime_spike_effects.py` | Testes determinísticos cobrindo: ausência de binding, rebinding divergente, primeiro digest errado, novo ID com digest errado sem efeito, replay idêntico, isolamento entre workflows e validação HTTP. |

---

## 3. Validação Executada

### 3.1 Testes Focais do Store e Servidor de Efeitos
```powershell
python -m pytest tests/test_runtime_spike_effects.py -v
```
Resultado: **6 passed in 6.56s**

```
tests/test_runtime_spike_effects.py::test_same_operation_is_idempotent_under_concurrency PASSED [ 16%]
tests/test_runtime_spike_effects.py::test_commit_then_disconnect_preserves_receipt_and_restart PASSED [ 33%]
tests/test_runtime_spike_effects.py::test_observations_are_not_deduplicated_and_catalog_is_frozen PASSED [ 50%]
tests/test_runtime_spike_effects.py::test_approval_requires_stable_subject_binding_and_is_scoped PASSED [ 66%]
tests/test_runtime_spike_effects.py::test_http_approval_only_forwards_to_bound_subject PASSED [ 83%]
tests/test_runtime_spike_effects.py::test_http_rejects_payloads_over_the_lab_limit_without_persisting_them PASSED [100%]
```
