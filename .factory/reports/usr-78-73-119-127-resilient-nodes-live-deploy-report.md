# Relatório de Conclusão: Resiliência de Nós e Prova ao Vivo de Deploy (USR-78, USR-119, USR-73, USR-127)

- **Onda**: 3 — Nós e Deploy (`docs/STABILITY_PLAN_2026-09-30.md`, Frente H & Autonomia Git)
- **Tickets Concluídos**: `USR-78`, `USR-119`, `USR-73`, `USR-127`
- **Data**: 2026-10-04
- **Ambiente de Validação**: Notebook local (`--local`)

---

## 1. Resumo Executivo das Entregas

Este lote encerra a resiliência operacional de sincronismo entre nós heterogêneos (Notebook, Desktop worker e Dokploy VPS), a remediação segura e não destrutiva de checkouts divergentes ou sujos, o desacoplamento de checkouts canônicos em relação a worktrees de tickets ativos e a garantia de prova de convergência ao vivo para fechamento de tickets de deploy.

---

### A. USR-78: Proteção Contra Acúmulo de Commits Locais Fora do Fluxo de PR
- **Problema**: Nós com commits locais acumulados entravam em divergência silenciosa (`ahead > 0`), impedindo `git pull --ff-only` sem gerar alertas ou salvaguardas de código do operador.
- **Solução Implementada**:
  - `classify_checkout` em `core/infra/node_sync.py`: inspeciona o checkout via `git status --porcelain` e `git rev-list --left-right --count` para classificar em `converged`, `behind`, `ahead`, `diverged` ou `dirty`.
  - `safe_repair_divergent_checkout`: antes de qualquer alinhamento a `origin/main`, cria e envia uma branch remota `backup/<node>-<date>` contendo os commits locais do operador, abre um ticket formal de cherry-pick no `DemandsStore` e emite alerta. Se o push remoto falhar, o processo aborta imediatamente (*fail-closed*), nunca executando `reset --hard`.
  - `record_divergence_cycle`: persiste ciclos de divergência em `state_root() / "node_sync_divergence.json"` e emite alerta crítico no Telegram quando a divergência persistir por mais de 1 ciclo.
  - Cobertura de testes em `tests/test_node_sync_divergence.py`:
    - `test_classify_checkout_ahead_behind_diverged`
    - `test_safe_repair_divergent_pushes_backup_branch_and_resets`
    - `test_safe_repair_divergent_aborts_reset_if_backup_push_fails`
    - `test_record_divergence_cycle_alerts_telegram_on_second_cycle`.

---

### B. USR-119: Diagnóstico e Remediação Segura de Worktree Suja no node_sync
- **Problema**: Se o nó estivesse com arquivos não commitados, o `node_sync` abortava com `dirty worktree; no git operation attempted`, sem informar quais caminhos estavam sujos, e o `dokploy_redeploy.py` saía com código 1 indiferenciado de erro de deploy na nuvem.
- **Solução Implementada**:
  - `classify_checkout`: mapeia caminhos sujos por categoria (`tracked_modified`, `untracked`, `ignored`) listando apenas os nomes dos caminhos, sem vazamento de conteúdo de arquivos nos logs.
  - `remediate_dirty_checkout`: higieniza automaticamente apenas arquivos e diretórios sabidamente efêmeros/gerados (`__pycache__`, `.pytest_cache`, `.factory/test_logs`). Se houver qualquer modificação rastreada ou arquivo não efêmero, os arquivos são mantidos intactos (*zero perda de dados do operador*), registrando um ticket formal de investigação no `DemandsStore` e disparando notificação.
  - `scripts/dokploy_redeploy.py`: introduz o código de saída dedicado `EXIT_NODE_DIRTY = 3` quando o deploy Dokploy convergiu mas a sincronização do nó foi bloqueada por worktree suja.
  - Cobertura de testes em `tests/test_node_sync_divergence.py` e `tests/test_dokploy_redeploy.py`:
    - `test_classify_checkout_dirty_classes_without_content_leak`
    - `test_remediate_dirty_checkout_cleans_ephemeral_and_leaves_untracked`
    - `test_main_returns_exit_node_dirty_when_worktree_is_dirty`.

---

### C. USR-73: Prova ao Vivo Obrigatória de Convergência para Tickets de Deploy
- **Problema**: Tickets de deploy e infraestrutura eram marcados como `completed` apenas com testes sintéticos/fakes, enquanto em produção o serviço continuava sem convergir.
- **Solução Implementada**:
  - `is_live_deploy` em `core/demands/models.py`: propriedade que identifica tickets marcados com tag `deploy-live` ou critérios de aceite `live`.
  - `complete_ticket` em `core/git/autonomy.py`: aceita `live_verifier`. Para tickets `is_live_deploy`, executa a verificação ao vivo antes de consolidar o status. No sucesso, carimba `:live_converged`.
  - Se a prova falhar ou lançar exceção: o ticket é revertido para `DeliveryStatus.PLANNED`, a evidência de falha `[LIVE_CONVERGENCE_FAILURE]` é anexada ao `problem_statement`, e um ticket de defeito dependente é aberto automaticamente no `DemandsStore`.
  - `DemandsStore.save_ticket`: valida e bloqueia qualquer tentativa de persistir um ticket `is_live_deploy` como `completed` sem evidência de prova viva (`live_converged`, `live_verified` ou `live_provisional`).
  - Cobertura de testes em `tests/test_git_autonomy_live_proof.py`:
    - `test_complete_ticket_live_deploy_success`
    - `test_complete_ticket_live_deploy_failure_reverts_and_opens_defect`
    - `test_complete_ticket_live_verifier_exception_reverts_and_opens_defect`
    - `test_save_ticket_enforces_live_proof_for_deploy_tickets`.

---

### D. USR-127: Descoberta de Checkout Canônico do Main e Convergência Resumível
- **Problema**: Quando o nó executava tarefas a partir de worktrees gerenciadas de tickets, o `node_sync` não localizava o checkout canônico na branch `main` e pulava a sincronização. Da mesma forma, nós em estado `busy` abortavam sem persistência para retomada posterior.
- **Solução Implementada**:
  - `find_canonical_main_checkout` em `core/infra/node_sync.py`: descobre a raiz canônica ou worktree vinculada a `refs/heads/main` via `git rev-parse --git-common-dir` e `git worktree list --porcelain`, sem alterar a branch de trabalho atual nem interferir em configurações locais não rastreadas.
  - Guarda `busy`: respeita tarefas em execução no worker remoto e salva o estado pendente em `state_root() / "node_sync_pending.json"`.
  - Ação `retry`: `node_sync.py retry` e `node_sync.retry()` para reexecução idempotente e resumível assim que o nó remoto ficar ocioso.
  - Cobertura de testes em `tests/test_node_sync_divergence.py`:
    - `test_pending_convergence_persistence_and_clear`
    - `test_find_canonical_main_checkout_discovers_main_worktree`.

---

## 2. Validação Determinística Focada

Execução dos testes focados das entregas:
```powershell
python -m pytest tests/test_dokploy_redeploy.py tests/test_git_autonomy_live_proof.py tests/test_node_sync_divergence.py -q
```
Resultado: **90 passed, 1 skipped** (skip condicional esperado para plataforma non-Windows).
Tree hygiene: 100% limpa (nenhum resíduo temporário deixado em disco).
