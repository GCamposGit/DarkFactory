<!-- covers: core/line/stages.py, core/line/ticket_engine.py, core/line/ticket_stages.py, core/line/route_wait.py -->
# Motor da esteira

## Sequencia canonica de estagios (USR-375 S2)

`core/line/stages.py` define a ordem: INTAKE, GRILL, PLAN, RESEARCH, BUILD, VALIDATE, REVIEW, MERGE, DEPLOY_SMOKE, DOCS, LEARNING.

Cada estagio deixa um artefato em `.darkfac/runs/<run_id>/`, validado por `require_artifact(stage, ws)`:

| Estagio | Artefato | Modelo |
|---|---|---|
| INTAKE | `DEMAND.md` | texto nao vazio |
| GRILL | `GRILL.md` | texto nao vazio |
| PLAN | `SPEC.md` + `tickets.json` | `PlanningPlan` |
| BUILD | `progress.json` | `BuildReport` |
| VALIDATE | `validation.json` | `ValidationReport` (`passed` true) |
| REVIEW | `review_verdict.json` | `ReviewVerdict` (`approve`, sem bloqueios) |
| MERGE | `merge.json` | `MergeReceipt` |
| DEPLOY_SMOKE | `deploy_report.json` | `DeployReport` (`smoke_ok` true) |
| RESEARCH, DOCS, LEARNING | opcionais | retornam `None` (USR-376) |

Artefato ausente, vazio ou invalido levanta `StageArtifactMissing` (com `stage` e `reason`). O contrato esta ligado ao `run_ticket`, ao `fleet_dispatcher` e a linha (slice S4, abaixo).

## run_ticket executa a sequencia canonica (USR-375 S4)

Nao existe mais caminho direto agente -> portao -> merge. O `run_ticket` (e o `fleet_dispatcher`, que so
lanca `run_ticket.py <ID> --fleet`) executa os estagios em ordem e chama `require_artifact` antes de avancar:

1. **INTAKE** grava `DEMAND.md`; **GRILL** grava `GRILL.md` com as decisoes do intake (G1 semantico, S6).
   Correcao pequena (`estimate_ticket_size == small`) pula SOMENTE o GRILL.
2. **PLAN** chama `stage_planning.run_planning` (modelo de nivel alto) e grava `SPEC.md` + `tickets.json`;
   correcao pequena grava um plano minimo (um sub-ticket igual a demanda), sem agente.
3. **BUILD** roda o agente de desenvolvimento uma vez por sub-ticket do plano, em sequencia, na mesma
   worktree, e grava `progress.json` (`harness` = familia do implementador).
4. Na entrega (`--resume-delivery`, processo novo): exige PLAN e BUILD; **VALIDATE** grava `validation.json`
   depois do portao; **REVIEW** roda `stage_review.ReviewStage` com familia diferente do implementador e grava
   `review_verdict.json` so na aprovacao (`changes_required` reinvoca o agente, comita e repete o portao);
   **MERGE** grava `merge.json` (PR e SHA); **DEPLOY_SMOKE** grava `deploy_report.json`. Na DarkFac o deploy
   pos-merge continua unico, pelo `fleet_dispatcher`/`scripts/dokploy_redeploy.py`, entao o relatorio sai com
   `deployed=false` e `smoke_ok=null`: `require_artifact` aceita isso SO como "deploy pendente" e o ticket e
   reportado como `merged_awaiting_deploy` (`[MERGE] ... aguardando deploy`), nunca como entregue. Um
   `smoke_ok=null` com `deployed=true` e invalido. Sem plano ou veredito nao ha merge; sem relatorio de deploy
   a execucao falha.
5. **Sem rota elegivel** (nenhum modelo de nivel alto para PLAN, nenhum revisor de outra familia para REVIEW):
   o resultado do `RouteWaiter` (`no_route_available not_before=...`) vira espera, nao falha. O `run_ticket`
   sai com o codigo 75 (`EXIT_ROUTE_WAIT`, de `fleet_dispatcher.INFRA_EXIT_CODES`), com o marcador
   `harness_infra_error: route_wait`; o despachante reenfileira com cooldown e o ticket continua `planned`
   (no REVIEW a worktree e preservada para o `--resume-delivery`).

Os artefatos do ticket ficam fora da worktree (que o merge remove), em
`<state_root>/artifacts/ticket_runs/<ID>/.darkfac/runs/<ID>/` (`core/line/ticket_engine.py`). Uma execucao
nova do launcher apaga os artefatos anteriores; a entrega apaga validacao, merge e deploy anteriores.
Um `--resume-delivery` digitado pelo operador numa worktree anterior ao S4 (sem nenhum artefato) registra
plano/build minimos e segue por VALIDATE, REVIEW e MERGE.

Na linha, `bindings.IntegrationStageAdapter` recusa a integracao (`stage_artifact_missing:<ESTAGIO>`) sem
`SPEC.md`/`tickets.json` e sem `review_verdict.json`, que o `ReviewStage` passou a gravar ao aprovar.


## Planejamento

Rotas de planejamento: Claude (role `planning.claude`) ou Codex (role `planning.codex`), ambos com modelo tier `high` no registro.
O Codex roda com esforco `high` (pin do estagio) e o slug do CLI vem do `gateway_ids` do modelo no registro.

O estagio `planning` so roteia para modelos de nivel alto (`tier` `high` ou `frontier` em
`.factory/config/model_registry.json`, lido por `core.models.registry`). Um harness sem modelo
explicito (padrao do CLI) nao tem nivel declarado e nao e elegivel. O fallback do OpenRouter
(modelo barato) tambem e desligado para este estagio (`routing.HIGH_TIER_ONLY_STAGES`).

Sem modelo alto disponivel (cota, cooldown ou capacidade do host), `pick()` devolve `None` e o estagio
espera pela semantica de `core/line/route_wait.py` (`retry` com `not_before`, e `waiting_human` somente
apos o orcamento de relogio). Nunca ha queda para modelo barato e nenhum agente e invocado.

## Estrutura do run_ticket (USR-375 S3)

`run_ticket.py` e o launcher headless; `_run` agora so orquestra estagios, cada um com no maximo 150 linhas:
`_stage_line_lock`, `_stage_check_harness`, `_stage_intake`, `_stage_preflight` (sonda do grokbox, tamanho,
`_stage_route`, dry run), `_stage_workspace`, `_stage_build` (agente + retry), `_stage_deliver` (subprocesso
`--resume-delivery`). O portao de autocorrecao e dividido em `_gate_focal_step`, `_gate_official_step`
(`_gate_run_official`, `_gate_classify_failure`, `_gate_reinvoke_for_gate`) e `_gate_exhausted_result`, e a
entrega em `_deliver_sync`, `_deliver_external_project` e `_deliver_worktree`.

`core/line/ticket_stages.py` guarda os helpers sem dependencia de globais substituidas pelos testes
(construtores de prompt, comando/ambiente da entrega, payloads e relatorios de falha, `GateState`).
O fluxo que chama `pick`, `inspect_quotas`, `_run_bounded` etc. fica em `run_ticket.py` para que os
`monkeypatch` existentes continuem valendo. Refatoracao pura: CLI, codigos de saida e mensagens inalterados.
