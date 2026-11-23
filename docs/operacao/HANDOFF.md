# Handoff operacional

Versao: 13
Atualizado em: 2026-11-23 (Claude Code, coordenador)

## Estado

- Fase: V0 da Dark Factory (ver `docs/FASE_ATUAL.md`).
- `main` em `299fe0cb` (#919), CI verde, deploy verificado em `darkfac-cloud` (coordinator e worker no sha).
- Despachante `DarkFac-Fleet-Dispatcher` ativo e reiniciado no sha novo. USR-537 e USR-538 em voo no Notebook e no Desktop.
- Hosts: Notebook e Desktop executam. O Desktop voltou depois que o `/health` passou a publicar capacidade (#920). O Grokbox ainda não publica capacidade (`worker_capacity_missing_or_zero`): precisa subir o worker no sha atual.
- Cotas (23/11 09:46Z): Claude 94%, Codex 56%, Grok 49,5%. Antigravity com leitura vencida (transporte RPC).

## Incidente de 22-23/11: frota parada por PRs em rascunho

Uma sessão de coordenação abriu sete PRs em rascunho fora da esteira e não os promoveu. O despachante relançava USR-542, USR-543 e USR-544 a cada ~6 min. O `run_ticket` recusava com `[RECUSA] ja possui PR ativo` (código 75, conta como espera). Era um laço sem progresso no único host livre. O reparo durável está no USR-545.

Ao assumir a coordenação, cruze `gh pr list --json number,isDraft` com `recent_failures` em `.factory/runs/fleet_dispatcher_state.json`. Um PR em rascunho de ticket com CI verde deve ser revisado, promovido e mesclado.

## Entregue em 23/11

- #920 (USR-542), #924 (USR-543), #925 (USR-544), #923 e #919 (USR-540): revisão cruzada Claude, CI verde, squash.
- USR-447 fechado: já estava no main (#875, #884, #893). O hold era o próprio defeito agindo sobre trabalho mesclado.
- USR-535 reaberto: o #923 só libera artefatos não rastreados. O `learning_ledger.json` rastreado continua deixando o Notebook "divergente" mesmo com HEAD no sha esperado.

## Tickets registrados em 23/11

- USR-545: não relançar ticket com PR ativo; adotar o PR na entrega.
- USR-546: backoff para a varredura de worktrees que falha sempre.
- USR-547: `build_evidence_mismatch` falso sem PLAN (USR-462, USR-476) ou com trabalho já mesclado (USR-447).
- USR-548: rebase por cherry-pick para tickets antigos com 1 ou 2 commits (USR-517, USR-520, USR-522).
- USR-549: alertas de hold no Telegram com `missing_destination`.
- O revisor que grava texto de progresso como veredito (USR-446, USR-535) é o USR-537, em voo.

## Holds restantes e próximo passo

- Retrabalho delimitado: USR-446 (rebase e nova revisão), USR-483 (#912: `core/line/coordination.py:111`, `core/line/operating_harness.py:94`, CI vermelho), USR-519 (`core/line/pending_delivery.py:691`, `core/line/fleet_dispatcher.py:1231` e `:1251`), USR-539 (#918 com conflito; skill e caminho `runs/pending_deliveries.json`), USR-480 (`tests/line/test_agent_failure_visibility.py`).
- Conflito de rebase: USR-517, USR-520 e USR-522. Os dois últimos dividem `docs/manual/learning_pack.md`.
- USR-462 e USR-476 dependem do USR-547.
- PR #921 (USR-538) com conflito; o USR-538 está em voo pela frota.

## Armadilhas

1. Recusa não é falha, mas recusa repetida no mesmo sha é laço: veja o USR-545.
2. Para fechar um ticket entregue fora da esteira: `register_ticket` com `status=completed` e evidência, mais `AdmissionStore(state_root() / "admission.sqlite3").reset_budget("darkfac", id)`.
3. `learning.cli record-turn` altera o `learning_ledger.json` rastreado no checkout compartilhado. Antes de avançar o checkout: `git stash push -- .factory/learning/learning_ledger.json`, depois `merge --ff-only` e `stash pop`.
4. `dokploy_redeploy.py` sai com código 3 quando só o Notebook não converge; os serviços podem estar corretos. Confira as linhas `[RUNTIME IMAGE]`.
5. Id literal de modelo neste arquivo quebra o censo de modelos. Use alias.
