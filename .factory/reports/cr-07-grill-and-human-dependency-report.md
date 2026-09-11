# Relatório de Conclusão — CR-07: Fazer Grill e Dependência Humana Refletirem a Resolução

**Data:** 11/09/2026  
**Status:** Concluído com Sucesso (100% PASS)  
**Ticket:** CR-07 (Trilha HF-04 Gates, depende diretamente de CR-06)  
**Achado endereçado:** F09 (decisão material sem resposta burlando `ready_for_spec` com `pending_questions=[]`; `resolved_at` isolado retirando dependência humana sem receipt de probe)  
**Documentos canônicos:** `docs/handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md`, `docs/reviews/HF_CRITICAL_REVIEW_2026-09-09.md`, `core/workflow/contracts.py`, `core/workflow/readiness.py`

---

## 1. Escopo e Invariantes Implementadas

1. **Garantia de Resolução e Proveniência de Decisões do Grill**:
   - **`GrillDecision`**:
     - Adicionado campo `is_material: bool = True`.
     - Adicionado campo `reused_decision_ref: Identifier | None = None`.
     - Método `is_answered() -> bool`: confere se a decisão possui alternativa selecionada, resposta não vazia ou referência de reuso, combinada com proveniência (`decision_source`).
   - **`GrillRecord.validate_readiness`**:
     - Quando `ready_for_spec=True`, além de conferir `not pending_questions` e `example_criteria`, valida que nenhuma decisão com `is_material=True` esteja sem resposta (`is_answered()`).
     - Decisão material não respondida levanta `ValidationError` mesmo com `pending_questions=[]`.
     - Pressupostos reversíveis explícitos (`assumptions`) e decisões não materiais (`is_material=False`) continuam assumidos e não bloqueiam.
   - **`ReadinessGate.evaluate`**:
     - Valida que todas as decisões com `is_material=True` estejam respondidas com origem, impedindo avanço de readiness.

2. **Garantia Confiável de Resolução de Dependência Humana**:
   - **`ManualDependency`**:
     - Adicionado campo `resolution_receipt_ref: ShortText | None = None`.
     - Adicionado campo `blocked_stages: list[WorkflowState] = Field(default_factory=list)`.
     - Validação estrita:
       - Se `status is RESOLVED`: exige obrigatoriamente tanto `resolved_at` quanto `resolution_receipt_ref`.
       - Se `status is not RESOLVED`: proíbe tanto `resolved_at` quanto `resolution_receipt_ref`.
   - **`ReadinessGate.evaluate` (Sem Confiança Cega em `resolved_at`)**:
     - `resolved_at` isolado **não desbloqueia**; exige `context` e resolução do `resolution_receipt_ref`.
     - **Resultado do Probe**: `receipt.result is EvidenceResult.PASSED`.
     - **Probe Vinculado**: `receipt.requirement` ou `receipt.probe` deve coincidir com `dependency.final_probe` (ou `dependency_id`).
     - **Sujeito**: `receipt.subject` deve pertencer a `{ticket_id, dependency_id, *dependency.ticket_ids}`.
     - **Ambiente e Configuração**: `receipt.environment_ref` e `receipt.config_version` devem conferir com o contexto esperado.
     - **Validade Temporal e TTL em UTC**:
       - `receipt.observed_at >= dependency.created_at` (rejeição de receipt anterior à criação da dependência).
       - `0 <= now - observed_at <= max_age` (rejeição de data no futuro e expiração por TTL).
     - **Papel do Produtor**: `receipt.producer.role` deve ser habilitado pela política.
   - **Escopo Delimitado de Bloqueio**:
     - **Por Ticket**: Se `handoff.ticket_id not in dependency.ticket_ids`, a dependência não se aplica nem bloqueia o ticket atual.
     - **Por Estágio**: Se `dependency.blocked_stages` for especificado e `requested_state not in dependency.blocked_stages`, a dependência não bloqueia o estágio avaliado.
   - **Determinismo e Replay**: Avaliações repetidas produzem exatamente o mesmo veredito de elegibilidade e bloqueio.

---

## 2. Arquivos Modificados

| Arquivo | Mudança |
|---|---|
| `core/workflow/contracts.py` | Adicionados `is_material`, `reused_decision_ref` e `is_answered()` em `GrillDecision`; validação de decisões materiais em `GrillRecord.validate_readiness`; adicionados `resolution_receipt_ref` e `blocked_stages` em `ManualDependency` com validação de consistência mútua com `resolved_at`. |
| `core/workflow/readiness.py` | Validação de decisões materiais no `ReadinessGate.evaluate`; verificação estrita de resolução de `ManualDependency` via `resolution_receipt_ref` no `VerificationContext` (probe, sujeito, ambiente, config, ordenação temporal `observed_at >= created_at`, TTL, escopo por ticket e estágio). |
| `tests/test_workflow_contracts.py` | Atualizados fixtures `make_dependency` e `make_simulation_context`; adicionados 4 testes de contrato para decisões materiais, reuso, pressupostos opcionais e validação de `resolution_receipt_ref`. |
| `tests/test_workflow_verification.py` | Adicionado helper `make_test_dependency` e 14 testes de aceitação cobrindo todas as variantes positivas e negativas de CR-07 (decisões do Grill, receipt ausente, falho, probe divergente, sujeito divergente, alvo divergente, config divergente, receipt anterior à dependência, TTL expirado, isolamento entre tickets e escopo de estágios). |

---

## 3. Validação Executada

### 3.1 Testes do Módulo de Workflow
```powershell
python -m pytest tests/test_workflow_contracts.py tests/test_workflow_verification.py -v
```
Resultado: **64 passed in 0.18s (100% PASS)**

### 3.2 Harness Determinístico Completo
```powershell
python core/harness/runner.py --quick
```
Resultado: **592 passed, 2 skipped in 89.53s**
`[STEP_PASS] syntax_and_types`
`[STEP_PASS] unit_and_integration_tests`
`[HARNESS_PASS]`
