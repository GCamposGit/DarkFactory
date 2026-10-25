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
