# Handoff operacional

Versao: 18
Atualizado em: 2026-11-26 tarde (Antigravity, coordenação do plano EC-2026-11)

## Estado do plano EC-2026-11 em 26/11 tarde (ler primeiro)

| Bloco | Estado | Evidência |
|---|---|---|
| `EC-01` | parcial | checkout `C:\dev\DarkFac` limpo no `main`; worktrees 108 → 36; holds de 9 tickets cancelados liberados; OA-099/100/101 `done`; Desktop saudável em `100.78.181.90:8080` e convergido. Pausa do despachante (`Stop/Disable-ScheduledTask DarkFac-Fleet-Dispatcher`) negada pelo classificador e limpeza de `.factory/backups/trash` / `.factory/tmp` seguem pendentes. Holds `gate_timeout_repeated` de `USR-520`, `USR-537`, `USR-545`, `USR-568`, `USR-569` e `USR-577` agendados para liberação após este PR, mantendo `USR-446` em hold `review_changes_required`. |
| `EC-02` | concluído | `#954` → `0d7fba51` |
| `EC-03` | parcial | `#956` → `2f15816c` (overlay `.factory/learning/learning_ledger.live.json`, gitignored); `#958` → `d12422b3` (`Docs-Unchanged` automático conservador, superfície por AST); `#961` → `9dac33f9` (isolamento de `demands.json` em projeto externo). O item interno "não mexer em `demands.json` na worktree" conflita com o contrato de entrega e segue em redesenho no `USR-577`. |
| `EC-04` | concluído | `#953` → `a20de752` (OA-101 resolvida e sonda de cota ativa); `#962` / `USR-578` → `2bf0b839` (unificação de `find_claude_binary()` e `/health` sem sonda). |
| `EC-05` | aguardando aceite comercial | `#957` → `4cf09fd8` (`v0_proof` com recibos reais e ancestralidade remota); `#959` → `3134fd5f` (learning pack externo e smoke honesto); `#963` / `USR-579` → `ade26fb1` (`_stage_review` preserva `review_rounds` em falhas de rota e invoca `importlinter` via python); `#964` / `USR-580` → `96ffa4c0` (`DEPLOY_SMOKE` aceita `smoke_applicable=False`); `#965` / `USR-581` → `61073c46` (`stage_review` popula `output_refs` com `publish=False`). Entregas `mansaodolago` abertas contra `darkfac/m1-seo` (`origin/master` intocado em `04c87a24`): `MDL-01` → `Site_Mansaodolago#1`, `MDL-02` → `#2`, `MDL-03` → `#3`, Learning Packs `lp-mdl-01..03` gerados e tickets em `waiting_human`. |

- `core.line.v0_proof`: `streak=0/5`, `criterion_met=False` (`MDL-01..03` aguardam aceite/merge do owner em `darkfac/m1-seo` e não carregam tag `channel:` — decisão aberta para o owner).
- Ticket agrupado dos residuais de 25/11: `USR-577` (`demands.json` na entrega interna, superfície `*_ENV`, `affected --run` lento, evidência externa na frota, pausa do despachante).
- Follow-ups de defeitos encontrados na sessão de 26/11 (enfileirados como `USR-*` após este PR): revisão CLI headless do Antigravity (`ANTIGRAVITY_CSRF_TOKEN` / limite de linha de comando / espera síncrona ou exclusão explícita), auto-reserva do `fleet_dispatcher` no Desktop empurrando `runner.py --profile pr --remote-required` para execução local, e transição automática para `waiting_human` em projeto externo com `requires_commercial_acceptance: true` + guarda pós-rodada final/novo commit no `stage_review` + endurecimento de saúde MCP `segundocerebro`.
- `USR-279` segue como `accepted` (placar real `0/5`; não vai para a frota).
- `OA-102` ignorada por instrução explícita do owner (execução conduzida diretamente na sessão interativa).

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
2. Desde o `#956`, `learning.cli record-turn` grava o runtime em `.factory/learning/learning_ledger.live.json` (gitignored), mantendo `C:\dev\DarkFac` limpo no `main`.
3. `dokploy_redeploy.py` sai com código 3 quando só o Notebook não converge por árvore suja. Confira as linhas `[NODE SYNC]` e `[RUNTIME IMAGE]`.
4. Identificador literal de modelo neste arquivo ou no roadmap quebra `tests/test_model_id_ratchet_usr185.py`. Cite apenas aliases (`opus`, `sonnet`, `haiku`, `sol`, `luna`, `neon`, `flash`).
5. `MDL-01..03` (`mansaodolago`, cliente real) miram exclusivamente a branch `darkfac/m1-seo` (`requires_commercial_acceptance: true`). Nada vai para `master` ou FTP sem aceite expresso do owner.
