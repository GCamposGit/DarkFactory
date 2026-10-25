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

Artefato ausente, vazio ou invalido levanta `StageArtifactMissing` (com `stage` e `reason`). O contrato ainda nao esta ligado ao `run_ticket` (slice S4).


## Planejamento

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
