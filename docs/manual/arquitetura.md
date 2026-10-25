<!-- covers: core/workflow/*.py -->
# Arquitetura do modulo workflow

Apos USR-375 S1, `core/workflow/` nao contem mais os handlers por estagio
(`development`, `quality`, `release`, `evolution`, `learning`): eles nao tinham
chamador em codigo de producao. O desenho da esteira vive em `core/line`.

Layout atual:

- Contratos: `contracts.py`, `control_contracts.py`.
- Persistencia: `control_store.py` (`ControlStore` e o Protocol; `SQLiteControlStore` a implementacao).
- Execucao: `runtime.py`, `handlers.py`, `successors.py`, `supervisor.py`, `cycle.py`.
- Jobs e catalogo: `catalog_jobs.py`, `planning_jobs.py`, `job_board.py`, `execution_registry.py`.
- Estagios ainda ativos: `integration_handler.py`, `research_handler.py`.
- Politica e rotas: `capacity_policy.py`, `effective_policy.py`, `policy_binding.py`, `qualified_routes.py`.
- Apoio: `readiness.py`, `reconciliation.py`, `verification.py`, `manual_resolution.py`, `line_live.py`, `safe_export.py`, `cli.py`.

Nota: `materialize` aparece em `ControlStore` (declaracao do Protocol) e em
`SQLiteControlStore` (implementacao); nao e duplicata.

# Arquitetura

## Modulos removidos (USR-377 S1)

Sem uso em producao (nenhum import fora de testes), removidos em 2026-10-24:
`core/planning/`, `core/pilots/`, `core/security/`, `core/marketing/` e
`scripts/run_practical_activations.py`, junto com seus testes dedicados.
`core/knowledge`, `core/game` e `core/workflow` permanecem (em uso).

## Orquestrador

O pacote `core/orchestrator` guarda a infraestrutura de execução e entrega que a esteira
(`core/line`) e o Hub usam. Ele não decide o fluxo de um ticket: quem decide é `core/line`.

Módulos que permanecem, todos com chamador em produção:

| Módulo | Para que serve | Quem usa |
| --- | --- | --- |
| `guard.py` | Lista de arquivos de governança protegidos | `core/line/protected_edit.py`, `dogfood.py`, `subagent_routing.py` |
| `delivery.py`, `delivery_executor.py` | Entrega e integração | `core/workflow/integration_handler.py`, `hub/backend/webhooks.py` |
| `build_artifacts.py`, `deployment_adapter.py`, `compose_render.py`, `bluegreen.py` | Artefatos imutáveis, deploy e blue-green | `core/line/stage_release.py`, `scripts/dokploy_*.py` |
| `release_pipeline.py` | Pipeline de release (build, staging, produção, backup) | `core/workflow/release_handlers.py`, `hub/backend/service.py` |
| `cloud_coordinator.py`, `cloud_worker.py`, `cloud_db.py`, `cloud_artifacts.py`, `artifact_retention.py`, `adapters/control_postgres.py` | Coordenação de workers na nuvem e banco de controle | `scripts/start_fleet_worker.py`, `core/line/*` |
| `context.py` | Contexto de aprendizado | `core/learning/service.py` |
| `runtime.py`, `store.py` | Runtime SQLite do experimento HF-02, hoje só usado por `spikes/runtime_choice` | spike (candidato a remoção junto com `spikes/`) |

Removidos em USR-377 (S3), por não terem nenhum importador em produção, apenas testes:
`cli.py` (CLI vertical DF-15), `release_cli.py`, `state.py` e `models.py` (máquina de estados DF-01,
substituída pelo estado da esteira em `core/line`), `target_journey.py` (a etapa foi fundida em
`build_deploy`) e o arquivo `factory_vertical.config.json`.

Regra para novos módulos: todo módulo em `core/orchestrator` precisa ter um chamador fora de
`tests/`; uma varredura de código morto semanal abre ticket de limpeza quando isso deixa de valer.

## Roteamento (USR-378)

O unico roteador de producao e `core/line/routing.pick`. Os roteadores
paralelos `core/router/model_router.py` e `core/router/harness_router.py` foram
removidos (sem chamador fora de testes). `core/router/token_budget.py` fica
porque `pick` e o monitor de tokens usam `_quota_headroom`;
`core/benchmarks/router.py`, `core/portfolio/router_optimizer.py` e
`core/workflow/qualified_routes.py` permanecem por terem chamadores vivos.

## Backend do Hub (USR-377 S7b)

`hub/backend/service.py` e fachada fina: `HubService` (construtor, `get_line_live`) e
reexports. A logica vive em mixins por dominio: `service_line`, `service_interventions`,
`service_progress`, `service_catalog`, `service_usage`, `service_ai`, `service_integrations`
e a guarda SSRF em `service_net`.
