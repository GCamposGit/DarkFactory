# HF-27 — Linha de produção autônoma · índice de handoffs

Plano mestre: [PRODUCTION_LINE_PLAN_2026-09-22.md](../../PRODUCTION_LINE_PLAN_2026-09-22.md). Todos os tickets estão `planned`, com binding high resolvido neste pacote e prontos para papel econômico.
Pré-requisito: integrar ao `main` a contenção do HF-03-08 (branch `codex/hf-03-08-activation`, com worker fail-closed e sem `deterministic_mock`).

| Ticket | Título | Papel | Depende de | Onda |
| --- | --- | --- | --- | --- |
| [HF-27-01](HF-27-01.md) | Registry de projetos executável | economy | — | 1 |
| [HF-27-02](HF-27-02.md) | Workspace Git por run | economy | 01 | 1 |
| [HF-27-03](HF-27-03.md) | AgentCLI real + roteamento por quota | economy | — | 1 |
| [HF-27-04](HF-27-04.md) | Grill de rodada única + Planning | economy | 02, 03 | 2 |
| [HF-27-05](HF-27-05.md) | Development + validate loop + review cruzada | economy | 02, 03 | 2 |
| [HF-27-06](HF-27-06.md) | Integração GitHub via gh | economy | 02 | 2 |
| [HF-27-07](HF-27-07.md) | Deploy + smoke + rollback por target | economy | 01, 06 | 2 |
| [HF-27-08](HF-27-08.md) | Ligar a linha no worker + DAG enxuto + canal humano | high_architecture (revisão) / economy | 04, 05, 06, 07 | 3 |
| [HF-27-09](HF-27-09.md) | Topologia VPS + on-prem | operations | 03 | 2 |
| [HF-27-10](HF-27-10.md) | Canário E2E diário + dogfood | economy | 08, 09 | 4 |

```mermaid
flowchart TD
  A[HF-27-01] --> B[HF-27-02]
  B --> D[HF-27-04]
  C[HF-27-03] --> D
  B --> E[HF-27-05]
  C --> E
  B --> F[HF-27-06]
  A --> G[HF-27-07]
  F --> G
  D --> H[HF-27-08]
  E --> H
  F --> H
  G --> H
  C --> I[HF-27-09]
  H --> J[HF-27-10]
  I --> J
```

## Ownership de arquivos (disjunto por ticket, permite paralelismo)

| Ticket | Arquivos principais |
| --- | --- |
| 01 | `core/projects/models.py`, `core/projects/registry.py`, `core/projects/detect.py` (novo), `.factory/projects.json` |
| 02 | `core/line/__init__.py` (novo), `core/line/workspace.py` (novo) |
| 03 | `core/line/agent_cli.py` (novo), `core/line/routing.py` (novo), `.factory/config/line_routing.json` (novo), `core/harness/remote_worker.py` (só flags) |
| 04 | `core/line/stage_grill.py` (novo), `core/line/stage_planning.py` (novo), `core/line/prompts/` (novo) |
| 05 | `core/line/stage_build.py` (novo), `core/line/stage_review.py` (novo) |
| 06 | `core/line/stage_integration.py` (novo) |
| 07 | `core/line/stage_release.py` (novo), `core/orchestrator/deployment_adapter.py` (adiciona `FtpMirrorDeploymentAdapter`) |
| 08 | `core/line/bindings.py` (novo), `core/orchestrator/cloud_worker.py`, `core/workflow/successors.py`, `core/line/human.py` (novo) |
| 09 | `deploy/dokploy/Dockerfile.cloud`, `deploy/dokploy/docker-compose.cloud.yml`, `scripts/start_onprem_worker.ps1`, `deploy/dokploy/env.cloud.example` |
| 10 | `core/line/canary.py` (novo), `deploy/dokploy/docker-compose.cloud.yml` (serviço `canary`, serializado após 09) |

## Contrato comum da linha (vale para todos)

- A interface de etapa é a existente: `StageHandler.handle(StageContext) -> StageResult` (`core/workflow/control_contracts.py`). `success` exige `output_refs` reais (SHA de commit, URL de PR, ID de deploy, URL de smoke).
- O estado entre etapas vive **na branch** `df/<run_id>` do repo alvo, em `.darkfac/runs/<run_id>/`. Um handler nunca depende de memória de processo. Toda etapa começa com `workspace.checkout(run)` (fetch + reset para o tip remoto da branch).
- Idempotência: toda etapa verifica primeiro se o efeito já existe (commit com trailer `DarkFac-Job: <job_key>`, PR aberto para a branch, deploy com o mesmo SHA) e o reaproveita.
- Falhas usam `outcome`: `retry` (transitória), `failed` com `cause_code`, `replan` (spec errada) e `waiting_human` (só para os itens da seção 7 do plano).
- Testes focais em `tests/line/` com repositório Git temporário (`tmp_path`, `git init`) e um CLI falso (`scripts` stub no PATH) para as etapas de agente. Nenhum teste depende de rede.
- Validação global inalterada: `python core/harness/runner.py --quick` e `python -m pytest tests -v --ignore=tests/test_canaletto.py`.
