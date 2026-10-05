# Relatório de Conclusão: USR-91

**Ticket**: USR-91  
**Título**: Fechar o ciclo de aprendizado da linha: a etapa retrospective grava lições que o planning lê  
**Status**: `completed`  
**Data**: 2026-10-05  
**PR**: [#160](https://github.com/GCamposGit/DarkFactory/pull/160) (commit `87ca1a5`)  
**Portão Determinístico Oficial**: 3.098 testes descobertos, 3.090 aprovados, 8 pulados, 0 falhas (`[HARNESS_PASS]`)  
**Suíte Line**: 581 testes aprovados, 1 pulado  

---

## 1. Contexto e Problema

O estágio `stage_planning` lia `.darkfac/LESSONS.md` do repositório alvo através da função `read_lessons(repo_path: Path)` para enriquecer o prompt do agente de planejamento (`prompts/planning.md`), porém nenhum componente do sistema escrevia esse arquivo.

O manipulador `RetrospectiveStageHandler` (`core/line/bindings.py`) apenas registrava um log com o resumo e gravava dados no `TelemetryStore`, sem transformar o histórico de falhas e execuções em lições acionáveis. Como consequência, o ciclo aprender -> planejar permanecia aberto, e repetições de erros do mesmo padrão não eram prevenidas nos planejamentos subsequentes.

---

## 2. Solução Implementada

1. **Módulo Determinístico de Retrospectiva (`core/line/retrospective.py`)**:
   - Implementado sem chamadas a LLM, operando por regras estritas causa -> lição:
     - `cause_codes` (`validate_exhausted`, `build_failed`, `base_red`, `ci_pending`/`ci_failed`, `review_exhausted`, `grill_timeout`, `no_route`/`quota`, `timeout`): geram lições preventivas específicas categorizadas por `MistakeCategory`.
     - `max_iteration >= 2`: identifica estágios com iterações excessivas e prescreve diffs atômicos e validações intermediárias.
     - `total_cost_usd >= 1.0`: recomenda otimização de custo e priorização de modelos eficientes.
     - Falhas gerais de outcome: instruem revisão de pré-condições.
     - Runs 100% limpos e de baixo custo: 0 lições emitidas (respeitando o critério de que lições são geradas por falhas/retries).
     - As lições geradas são limitadas deterministicamente entre 1 e 3 por run (`lessons[:3]`).

2. **Persistência em `.darkfac/LESSONS.md`**:
   - `append_lessons_to_markdown_file`: formata as lições em Markdown com tags estruturadas (`- [tag] lição`) preservando o histórico anterior.
   - Aplica a escrita diretamente no repositório do projeto (`project.resolve_path()`) e na worktree do run (`ws.path / ".darkfac" / "LESSONS.md"`).
   - Realiza commit isolado (`ws_mod.commit_paths`) com trailer `DarkFac-Job: <run_id>:retrospective` e push best-effort.

3. **Integração no Ledger de Aprendizado Contínuo (`ContinuousLearningTracker`)**:
   - `record_lessons_in_learning_tracker`: registra cada lição no `learning_ledger.json` (`core/learning/tracker.py`) via `record_mistake_rca`, alimentando o subsistema de auto-aperfeiçoamento perpétuo da fábrica.

4. **Integração no `RetrospectiveStageHandler` (`core/line/bindings.py`)**:
   - Invoca `record_retrospective_lessons` durante o ciclo de retrospectiva do run.
   - Protegido por bloco defensivo `try/except`: falhas em escrita de disco, rede ou git são logadas como warning e NUNCA alteram o outcome do run (mantém `outcome="success"`).

---

## 3. Evidências de Teste e Validação

- **Nova Suíte de Testes (`tests/line/test_retrospective.py`)**:
  - `test_generate_lessons_clean_run_yields_zero_lessons`: valida que runs sem retry/falha produzem 0 lições.
  - `test_generate_lessons_from_cause_codes`: valida mapeamento exato de cause_codes para regras preventivas.
  - `test_generate_lessons_from_high_iterations_and_cost`: valida detecção de retries e custos elevados.
  - `test_append_lessons_to_markdown_file_and_read_lessons`: valida formatação, append cumulativo e leitura por `read_lessons`.
  - `test_record_lessons_in_learning_tracker`: valida persistência de MistakeRCA no `learning_ledger.json`.
  - `test_retrospective_stage_handler_records_lessons_and_never_fails`: valida o ciclo completo na worktree, commit isolado e injeção da lição no prompt de planning do run seguinte.
  - `test_retrospective_stage_handler_safe_on_project_error`: valida que falhas ou projetos desconhecidos não alteram o resultado de sucesso da entrega.
- **Suíte Completa da Linha (`tests/line/`)**:
  - 581 testes aprovados, 1 pulado (0 falhas).
- **Portão Determinístico Oficial (`runner.py --quick --local`)**:
  - 3.098 testes descobertos, 3.090 aprovados, 8 pulados, 0 falhas (`[HARNESS_PASS]`).
