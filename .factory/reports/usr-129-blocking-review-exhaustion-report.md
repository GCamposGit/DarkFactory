# Relatório de Implementação — USR-129

## Identificação
- **Ticket**: `USR-129`
- **Título**: Revisão bloqueante não pode ser aprovada ao esgotar rodadas
- **Branch**: `feature/usr-129-blocking-review-exhaustion`
- **Data**: 2026-10-06

## 1. Problema e Diagnóstico
Historicamente, o `ReviewStage` possuía uma lógica de tolerância (`force_approve`) que, ao esgotar o limite configurado de rodadas (`run_caps.review_rounds`), convertia `changes_required` em `approve` (desde que a validação estivesse verde), anotando os itens pendentes como não-bloqueantes para evitar supostos loops infinitos. Isso violava o princípio fundamental de isolamento de revisão independente da fábrica autônoma: uma objeção técnica bloqueante legítima levantada por um revisor independente de família diferente nunca pode ser tacitamente aprovada nem passar para o estágio de integração.

## 2. Mudanças Implementadas
1. **Bloqueio Incondicional de Aprovação com Itens Bloqueantes**:
   - Atualizado `core/line/stage_review.py`:
     ```python
     approved = verdict.verdict == "approve" and not bool(verdict.blocking)
     exhausted = round_num > max_rounds and not approved
     ```
   - Impede que um parecer que declare `verdict: "approve"` mas que contenha itens na lista `blocking` seja considerado aprovado.
2. **Falha Estruturada ao Esgotar Rodadas**:
   - Ao ultrapassar `max_rounds` sem aprovação, `ReviewStage.run()` retorna `StageResult(outcome="failed", cause_code=f"review_exhausted_changes_required:round={round_num}", output_refs=[sha])`.
   - Nenhuma transição para `integration` ocorre.
   - Replay idempotente: re-executar sobre um estado já esgotado retorna imediatamente o status `failed` com o mesmo código de causa sem chamar o agente novamente.
   - Proteção contra dados legados: se `last.forced == True`, retorna `StageResult(outcome="failed", cause_code="review_forced_approval_legacy")`.
3. **Isolamento de DAG no Workflow**:
   - Em `core/workflow/successors.py`, quando um estágio falha, `successors = []` e apenas `retrospective` é acionado para capturar lições aprendidas (`emit_retrospective = True`).
   - O ticket/run encerra de forma limpa como `failed`, permitindo que o owner ou a esteira realize replanejamento ou novo attempt (`attempt + 1`) sem entrar em loop infinito.

## 3. Cobertura de Testes
Adicionados e validados testes em `tests/line/test_stage_review.py`:
- `test_blocking_review_objection_across_all_rounds_prevents_success_and_integration`:
  - Simula 3 rodadas com pareceres bloqueantes (`changes_required`).
  - Rodada 1 e 2: retornam `outcome="retry"` com direcionamento para desenvolvimento (`retry:development\nchanges_required:round=N`).
  - Rodada 3 (esgotando `review_rounds=2`): retorna `outcome="failed"` com causa `review_exhausted_changes_required:round=3`.
  - Prova que `materialize_result` no `SQLiteControlStore` produz apenas `['retrospective']`, nunca `integration`.
  - Prova idempotência de replay sem invocar o agente novamente.
- `test_review_verdict_approve_with_blocking_items_fails_to_approve`:
  - Simula parecer contraditório (`verdict: "approve"` com itens em `blocking`).
  - Garante que a aprovação é rejeitada e mapeada para `changes_required` e gravada no log de revisão.

## 4. Critérios de Aceite
- [x] **Esgotamento retorna causa estruturada sem aprovar nem integrar**: Retorna `failed` com `review_exhausted_changes_required:round={round_num}` e output refs comitados.
- [x] **Run pode seguir para correção ou replanejamento sem loop infinito**: O estágio encerra terminalmente ou retenta limitadamente para desenvolvimento sem loop infinito.
- [x] **Teste com objeção bloqueante em todas as rodadas impede sucesso**: Coberto por suíte automatizada determinística.
