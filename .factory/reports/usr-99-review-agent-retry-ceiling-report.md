# Relatório de Conclusão: USR-99 (Estabilidade & Linha Autônoma)

**Data**: 2026-10-04  
**Ticket**: USR-99  
**Status**: Completed  
**Branch**: `feat/usr-99-review-agent-retry-cap`  
**PR**: [#141](https://github.com/GCamposGit/DarkFactory/pull/141)  
**Commit de Entrega**: `5f07182` (código/testes) e `6c92faa` (ledger)  
**Ambiente de Validação**: Notebook (portão completo 100% local)  

---

## 1. Contexto e Problema

Em produção, o run `run-f0c98a90cf0f` entrou em loop infinito na etapa `independent_review` com falhas de agente (`review_agent_empty_output`), gerando novos jobs a cada ~30s por dezenas de iterações sem teto.
O `core/line/stage_review.py` mapeava qualquer falha de execução de agente para `outcome="retry"` sem limite de tentativas consecutivas. Embora modelos que consumiam max_tokens em reasoning tenham sido tratados anteriormente, qualquer erro persistente de agente (timeout, empty output, crash) gerava loop perpétuo de retry no mesmo estágio e rodada.

---

## 2. Solução Implementada

1. **`core/line/stage_review.py`**:
   - Adicionada a constante `MAX_REVIEW_AGENT_RETRIES: int = 5`.
   - Adicionado o campo `agent_error_streak: int = 0` ao modelo `ReviewState` (persistido em `.darkfac/runs/<run_id>/review_state.json` na branch de trabalho).
   - Quando `not agent_result.ok`:
     - Se `state.agent_error_streak >= MAX_REVIEW_AGENT_RETRIES` (5 retries já esgotados): retorna `StageResult(outcome="failed", cause_code="review_agent_exhausted", output_refs=[])`.
     - Caso contrário: incrementa `state.agent_error_streak += 1`, persiste o estado no contexto e retorna `StageResult(outcome="retry", cause_code=f"review_agent_{agent_result.error_kind}", output_refs=[])`.
   - Quando o agente executa com sucesso (`agent_result.ok == True`):
     - Zera `state.agent_error_streak = 0` e persiste o estado.
   - Quando uma rodada de revisão é concluída (aprovação ou pedido de alterações) e gravada em `state.rounds`:
     - Zera `state.agent_error_streak = 0` para garantir que a rodada seguinte inicie com contador limpo.

2. **`tests/test_worker_capacity.py`**:
   - Adicionada a fixture `_clean_worker_env` com `monkeypatch.delenv("DARKFAC_MAX_WORKERS")` e `monkeypatch.delenv("PYTEST_XDIST_AUTO_NUM_WORKERS")` para garantir isolamento e estabilidade total dos testes de capacidade quando a variável de ambiente do harness estiver ativa.

3. **`tests/line/test_stage_review.py`**:
   - `test_consecutive_agent_errors_fail_after_max_retries`: valida 5 retries consecutivos retornando `retry` com `cause_code="review_agent_empty_output"` e o 6º retorno como `failed` com `cause_code="review_agent_exhausted"`.
   - `test_agent_error_streak_resets_on_agent_success`: valida que 2 falhas consecutivas elevam o streak para 2, e o sucesso subsequente do agente zera o streak para 0.
   - `test_agent_error_streak_resets_on_round_change`: valida que uma falha na rodada 1 seguida de sucesso com `changes_required` conclui a rodada com streak 0, e a rodada 2 reinicia a contagem a partir de 1 em caso de nova falha.

---

## 3. Evidências de Validação e Portão Oficial

1. **Portão Determinístico Oficial no Notebook (`runner.py --quick --local`)**:
   - Discovered Count: **3.008 testes**
   - Passed Count: **3.001 testes**
   - Skipped Count: **7 testes** (justificados por plataforma/hardware)
   - Failed Count: **0 testes**
   - Duração da suíte paralela: **744,37s** (12m24s, dentro do limite de 900s)
   - Veredito Oficial: `[HARNESS_PASS]`

2. **CI no GitHub Actions (PR #141)**:
   - `pr-validation (ubuntu-latest)`: PASS (2m32s)
   - `pr-validation (windows-latest)`: PASS (7m48s)
   - `trusted-pr-policy`: PASS (7s)

3. **Deploy em Produção e Resiliência (Dokploy VPS)**:
   - SHA convergido: `6c92faa71c1766685beffe633552a2ba974dadef`
   - Backup 3-tier automático pós-deploy: `snp3t_darkfac_20261004_140450_f45018`
   - Restore drill verificado: `drill_verified=True`
   - `check-main`: `[MAIN_GREEN]` (DarkFac CI no main verde)
