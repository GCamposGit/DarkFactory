# Relatório de Conclusão: USR-92

**Ticket**: USR-92  
**Título**: Backlog interno sem consumidor: o dogfood deve ler tickets planned com tag line-ok de demands.json, nao so o manifesto do roadmap  
**Status**: `completed`  
**Data**: 2026-10-05  

---

## 1. Contexto e Problema

O consumidor de dogfood da fábrica autônoma (`core.line.dogfood`) lia primariamente itens do manifesto de roadmap (`.factory/roadmap/darkfac.json`) marcados com a tag `line-ok`. No entanto, o backlog operacional da fábrica é mantido dinamicamente no ledger de demandas (`.factory/demands/demands.json`), onde residem os tickets `USR-*`.

Como consequência:
1. Tickets aprovados no backlog interno com tag `line-ok` não eram consumidos automaticamente na ausência de intervenção humana (dependendo de chamadas pontuais a `run_ticket.py` ou `/linha`).
2. A entrega de um ticket pela esteira não fechava o ciclo no ledger de demandas com atualização para `status: "completed"` e evidência estruturada de entrega (`delivery_evidence`), deixando o ticket indefinidamente em `implementing`.
3. Não havia uma interface unificada em `pick_candidate` para avaliar tanto tickets de demandas quanto itens de roadmap sob os mesmos critérios e precedência estrita.

---

## 2. Solução Implementada

1. **Persistência de Evidência de Entrega (`core/demands/store.py`)**:
   - Atualizado o método `DemandsStore.update_status` para suportar o parâmetro opcional `delivery_evidence`. Ao atualizar para `DeliveryStatus.COMPLETED`, a evidência é atribuída atomicamente antes da validação do modelo Pydantic, garantindo aderência estrita à regra de integridade do ledger.

2. **Extração de Evidência e Reconciliação no Owner Intake (`core/line/owner_intake.py`)**:
   - Implementada a função `extract_run_delivery_evidence(store, run_id)` que inspeciona os resultados dos jobs registrados no `ControlStore`. Localiza a saída do estágio `integration` (`output_refs=[pr_url, merge_sha]`) para produzir evidência estruturada `f"{pr_url} (commit {merge_sha})"`. Se o ticket exigir live deploy (`is_live_deploy`), garante a anotação `:live_converged`.
   - Em `submit_ticket_to_line`, quando uma tentativa anterior for detectada como `state == "succeeded"`, o ticket em `demands_store` é atualizado para `DeliveryStatus.COMPLETED` com a evidência extraída.

3. **Consumo Unificado e Priorizado no Dogfood (`core/line/dogfood.py`)**:
   - Atualizada a função `pick_candidate(items=..., tickets=..., exclude_ids=...)` para aceitar tanto listas de tickets de demandas quanto itens de roadmap. Enforça precedência estrita: tickets do backlog operacional (`demands.json`) são avaliados primeiro via `eligible_ticket_candidates` (ordenados por horizonte `NOW > NEXT > LATER`, data de criação e ID). Se nenhum ticket for elegível, recorre aos itens do roadmap.
   - Ambos os fluxos respeitam os portões obrigatórios: tag `line-ok`, resolução completa de dependências (`dependencies` em `completed`), e bloqueio estrito contra toque em arquivos protegidos por `guard.py`.
   - Em `submit_dogfood_item`, iterações sobre tickets que já tenham alcançado `state == "succeeded"` têm seu status reconciliado imediatamente para `completed` com evidência antes de avaliar o próximo item.

4. **Reconciliação Automática no Retrospectivo (`core/line/bindings.py`)**:
   - No estágio `RetrospectiveStageHandler.handle`, adicionado o método `_reconcile_ticket_delivery` em bloco defensivo (`try/except` não-bloqueante). Se o run pertencer a um ticket e todas as etapas da linha tiverem sido concluídas com sucesso, o ticket é marcado como `completed` no `.factory/demands/demands.json` do repositório alvo com evidência de PR/SHA no encerramento da entrega.

---

## 3. Evidências de Teste e Validação

- **Suíte de Testes do Dogfood (`tests/test_line_dogfood.py`)**:
  - `test_ticket_selection_requires_line_ok_resolved_deps_and_unprotected_paths`: valida filtros de tag `line-ok`, resolução de dependências e exclusão de caminhos protegidos.
  - `test_dogfood_uses_ledger_first_and_does_not_duplicate_an_active_ticket`: valida prioridade do ledger sobre o roadmap e política de um por vez.
  - `test_roadmap_cannot_bypass_ledger_tag_for_same_id`: valida que itens do roadmap não sobrepõem o estado do ledger.
  - `test_failed_ticket_run_gets_a_new_attempt_without_human_resubmission`: valida nova tentativa em caso de falha transitória sem intervenção humana.
  - `test_ticket_without_line_ok_is_never_submitted`: valida que tickets sem a tag `line-ok` nunca são submetidos.
  - `test_ticket_with_open_dependency_is_not_submitted`: valida que dependências em aberto bloqueiam a submissão.
  - `test_completed_ticket_run_updates_status_with_delivery_evidence`: valida a reconciliação e evidência de entrega ponta a ponta via dogfood.
  - `test_replayed_succeeded_ticket_submission_marks_completed`: valida a reconciliação no reenvio de ticket já entregue.
  - `test_pick_candidate_prioritizes_tickets_over_roadmap`: valida `pick_candidate` com tickets e roadmap.
  - `test_retrospective_stage_handler_reconciles_delivered_ticket`: valida a reconciliação automática pelo manipulador de retrospectiva.
  - Resultado: 10/10 testes aprovados.

- **Suítes Conexas**:
  - `tests/test_line_canary_hf2710.py`: 72/72 testes aprovados.
  - `tests/line/test_retrospective.py`: 7/7 testes aprovados.
