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
