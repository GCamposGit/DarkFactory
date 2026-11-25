# Handoff operacional

Versao: 16
Atualizado em: 2026-11-25 (Antigravity, planejamento cirúrgico aprovado pelo owner)

## Prioridade imediata da coordenação: Plano `EC-2026-11` (`EC-01` → `EC-05`)

O owner aprovou e priorizou o **Plano de Estabilização Cirúrgica** salvo em [`docs/plans/PLANO_ESTABILIZACAO_CIRURGICA_2026-11-25.md`](../plans/PLANO_ESTABILIZACAO_CIRURGICA_2026-11-25.md) e registrado no roadmap operacional (`.factory/roadmap/darkfac.json`, épico `EC-2026-11`, itens `EC-01` a `EC-05` em horizonte `now` e `EC-06` em horizonte `next`).

**Diretriz obrigatória ao assumir a fábrica (`coordenar-fabrica`):**

1. **Zero abertura de meta-tickets fragmentados.** Não abra novos tickets `USR-*` por sintoma nem redespache a fila antiga antes de concluir `EC-01` e `EC-02`.
2. **Inicie imediatamente por `EC-01` (higiene operacional do coordenador):**
   - Pausar o processo `core.line.fleet_dispatcher --loop` durante a cirurgia `EC-01..04` para cessar a criação de `fleet-dispatch-*` duplos e novos holds no Notebook.
   - Podar as ~75 worktrees órfãs/`fleet-dispatch-*` e limpar `.factory/backups/trash/` e `.factory/tmp/`, **preservando obrigatoriamente** as 3 worktrees com código pronto em `C:\dev\Site_Mansaodolago\.worktrees\`: `mdl-01-20261124T181425Z`, `mdl-02-20261124T181931Z` e `mdl-03-20261124T183555Z` (15+15+12 arquivos e 71 testes passando).
   - Limpar os holds espúrios em `.factory/admission.sqlite3` (`MDL-01..03` por `build_evidence_mismatch` e tickets trancados em `fix_agent_no_changes` por `[STEP_TIMEOUT]`), absorver/cancelar os meta-tickets cobertos pelo plano (`USR-572`, `USR-554`, `USR-557`, `USR-563`) e encerrar/arquivar o PR `#912` (`USR-483`, segurado por fluxo paralelo) para destravar `detect_stalls()` em `core.ops.coordination`.
3. **Executar na sequência estrita (1 PR limpo por bloco):**
   - **`EC-02`:** Reuso universal de worktrees (`project_checkout(project)`) em `run_ticket.py` e `fleet_dispatcher.py` + reset limpo de `progress.json` órfão em `ticket_engine.py`; fim do checkout efêmero `fleet-dispatch-*` com `DARKFAC_STATE_ROOT` explícito; classificação de `[STEP_TIMEOUT]` e `phase=collection` em `_gate_failure_is_timeout()` como código `75` sem chamar `_invoke_fix_agent` (absorve `USR-572`).
   - **`EC-03`:** Eventos de turno de `learning_ledger` fora do Git (`STATE_ROOT`), zero mutação de `demands.json` em branches de código e `Docs-Unchanged:` automático em ajustes estritamente internos.
   - **`EC-04`:** Descoberta do binário mais recente do Claude Code em `adapters.py`, sonda resiliente por alias curto (`haiku` / fallback) e reuso de snapshot fresco (< 5 min) entre fatias (absorve `USR-554`).
   - **`EC-05`:** Habilitar Learning Pack para `external_project` em `run_ticket.py`, alinhar `core/line/v0_proof.py` aos artefatos reais do motor único (absorve `USR-557`), religar a frota saudável e resgatar/entregar `MDL-01`, `MDL-02` e `MDL-03` na branch `darkfac/m1-seo` (jamais em `master`/FTP).
4. **Atualizar `.factory/roadmap/darkfac.json`** a cada bloco `EC-01..05` entregue (marcando `delivery_status: "completed"` com `commit:<ID>:<sha>` e `"verified": true`).

## Estado observado na manhã de 25/11

- Fase: V0 da Dark Factory (`docs/FASE_ATUAL.md`), `core.line.v0_proof` em `0/5`.
- `origin/main` verde após `#944`, `#946`, `#948`, `#949`, `#950` e `#951`; serviços `darkfac-cloud` (`coordinator` e `worker`) saudáveis.
- Ocorrências da madrugada (24/11 23h → 25/11 10h45 UTC) que confirmaram o diagnóstico de `EC-02` e `EC-04`:
  - `MDL-03` entrou em hold `build_evidence_mismatch` (`01:42Z`) pelo mesmo bug de nova worktree por tentativa que já havia travado `MDL-01` e `MDL-02`.
  - `USR-572` (`02:10Z`), `USR-554` (`09:49Z`) e `USR-563` (`10:45Z`) caíram em hold `fix_agent_no_changes` após `[STEP_TIMEOUT]` sob contenção local do `fleet_dispatcher --loop` no Notebook (`18` holds totais na tabela `ticket_holds`).
- PR aberto remanescente: `#912` (`USR-483`, segurado desde 20/11 após revisão cruzada apontar segundo fluxo paralelo a `core.ops.coordination`).

## Histórico recente entregue em 24/11

- `#921` (`USR-538`): `completed` exige prova `<sha>:merged`.
- `#944` (`USR-574`): testes de higiene alinhados ao `USR-561`.
- `#946` (`USR-570`/`571`/`575`): identidade git de fallback em repo de produto, PEM sem corpo base64 e testes citados por mudança não-Python no fallback do PR.
- `#949` (`USR-548`): rebase por cherry-pick para tickets com 1 ou 2 commits.
- `#950` (`USR-547`): reset de artefato só com DEMAND/GRILL em worktree limpa.
- `#948` (`USR-550`): grill órfão, dependência terminal e sessão sem perguntas.

## Armadilhas ativas (até conclusão de `EC-01..05`)

1. Para fechar entrega feita fora do launcher, use `Set-Location C:\dev\DarkFac; python -m core.demands.cli complete <TICKET> --sha <sha completo do origin/main> --evidence "..."`.
2. Até `EC-03` entrar, `learning.cli record-turn` altera `.factory/learning/learning_ledger.json` rastreado no checkout compartilhado (`C:\dev\DarkFac`). Antes de avançar o checkout, rode `git -C C:\dev\DarkFac stash push -m <tag> -- .factory/learning/learning_ledger.json`, depois `git -C C:\dev\DarkFac merge --ff-only` e `git -C C:\dev\DarkFac stash pop`.
3. `dokploy_redeploy.py` sai com código 3 quando só o Notebook não converge por árvore suja. Confira as linhas `[NODE SYNC]` e `[RUNTIME IMAGE]`.
4. Identificador literal de modelo neste arquivo ou no roadmap quebra `tests/test_model_id_ratchet_usr185.py`. Cite apenas aliases (`opus`, `sonnet`, `haiku`, `sol`, `luna`, `neon`, `flash`).
5. `MDL-01..03` (`mansaodolago`, cliente real) miram exclusivamente a branch `darkfac/m1-seo` (`requires_commercial_acceptance: true`). Nada vai para `master` ou FTP sem aceite expresso do owner.
