# Relatório de Remediação CR-05: Aplicar Política, Autoridade e Etapa no Gate

**Data:** 10/09/2026  
**Status:** Concluído com Sucesso  
**Ticket:** CR-05 (HF-04 P1)  
**Achados endereçados:** F01 (autorização de entrega sem política/provas) e F03 (falha na aplicação de transições legais e cobrança de provas na etapa errada)  
**Documentos de referência:** `docs/handoffs/HF-REVIEW-REMEDIATION-2026-09-09.md`, `core/workflow/contracts.py`, `core/workflow/verification.py`

---

## 1. Escopo e Objetivos

O objetivo de CR-05 foi tornar o `ReadinessGate` rigorosamente governado por autoridade externa confiável (`VerificationContext`), aplicando a política de estágios (`GatePolicy`) sem confiar em autodeclarações do candidato.

Principais entregas:
1. `ReadinessGate.evaluate(handoff, *, context=None, target_state=None) -> ReadinessReport`:
   - Se `context is None`: bloqueia com `eligible=False` e código estável `missing verification context: CONTEXT_REQUIRED`.
   - `target_state=None`: avalia a etapa atual (`requested_state = target_state or handoff.state`) sem inventar auto-transições (*self-edge*).
   - `target_state != handoff.state`: valida transição pelo grafo fechado `validate_transition(handoff.state, target_state)`.
   - Estados terminais (`CANCELLED`, `FAILED`): não avançam e nunca são considerados elegíveis (`readiness=BLOCKED`).
2. Validação da autoridade do plano:
   - `approval_reference` deve existir no `context.resolve_approval(...)`.
   - `approval.plan_digest == context.plan_digest`.
   - `approval.planner_tier == handoff.planner_tier` e rejeição absoluta de `PlannerTier.ECONOMY` para aprovação de planos.
   - Validação de papel habilitado no approver se a política declarar `enabled_roles`.
3. Requisitos por etapa sem ordinais arbitrários:
   - Mapeamento explícito `STAGE_READINESS_APPLICABILITY: dict[WorkflowState, frozenset[ReadinessState]]`.
   - Provas operacionais futuras (`required_for=ReadinessState.OPERATIONALLY_VERIFIED`) não bloqueiam estágios de planejamento (`READY_FOR_HANDOFF`).
   - A lista de requisitos do candidato (`handoff.required_evidence`) é meramente informativa e não pode diminuir os requisitos impostos por `GatePolicy.requirements_for_stage(stage)`.
4. Garantias estritas para entrega (`requested_state is WorkflowState.DELIVERED`):
   - Estado anterior deve ser `INDEPENDENT_REVIEW` ou `DELIVERED`.
   - Ambiente deve ser `TARGET_ENVIRONMENT` (rejeição explícita de `MOCK_ONLY`).
   - Requer receipt de revisão independente emitido por verificador com sujeito distinto do implementador (`receipt.producer.subject != context.expected_identity.subject`).
   - Requer pelo menos uma prova operacional aprovada no ambiente alvo (`ValidationMode.TARGET_ENVIRONMENT`).
   - Rejeição absoluta de dispensas operacionais em entrega.
5. Repasse de contexto:
   - `ReadinessGate.require_delivery(handoff, *, context=None)` e `mark_delivered(handoff, gate=None, *, context=None)` recebem e aplicam o contexto.

---

## 2. Modificações Realizadas

| Arquivo | Mudança |
|---|---|
| `core/workflow/readiness.py` | Integrado `VerificationContext`, `GatePolicy`, `ValidationMode`, `STAGE_READINESS_APPLICABILITY`, checagens de autoridade, transições estritas e regras de entrega. |
| `tests/test_workflow_contracts.py` | Adicionado helper `make_simulation_context(...)`; ajustados testes positivos para prover contexto de simulação confiável sem restaurar confiança cega em flags. |
| `tests/test_workflow_verification.py` | Adicionados 7 testes cobrindo os critérios de aceite (1) a (6) e o código estável `CONTEXT_REQUIRED`. |

---

## 3. Matriz de Aceite e Evidências

| Critério de Aceite | Teste Automatizado | Status |
|---|---|---|
| (1) Controle de planejamento sem prova operacional futura | `test_planning_does_not_require_future_operational_evidence` | **PASS** |
| (2) Lista vazia do candidato não elimina requisito da política | `test_empty_candidate_evidence_list_does_not_bypass_policy_requirements` | **PASS** |
| (3) Bloqueio de aprovação falsa, autodeclaração economy e receipt de outro candidato | `test_plan_approval_and_candidate_binding_rejections` | **PASS** |
| (4) Cancelado ou falho não avança | `test_cancelled_and_failed_workflows_cannot_advance` | **PASS** |
| (5) Fluxo progressivo válido por etapas funciona até entrega | `test_valid_progressive_workflow_stages_flow` | **PASS** |
| (6) Entrega sem revisão independente ou sem prova de alvo bloqueia | `test_delivery_fails_closed_without_independent_review_or_target_proof` | **PASS** |
| Contexto ausente retorna `CONTEXT_REQUIRED` | `test_missing_context_fails_closed_with_stable_code` | **PASS** |
| Regressões independentes de revisão (F01/F02/F03/F09) | `.factory/reviews/hf-critical-20260909/test_gate_regressions.py` | **10 PASS / 2 FAIL (CR-08 pendente)** |

---

## 4. Próximos Passos
- Avançar para o ticket subsequente da trilha HF-04: **CR-06: Calcular validade e conferir o sujeito da evidência** (regras de TTL, relógio UTC e vinculação estrita de sujeito da evidência).
