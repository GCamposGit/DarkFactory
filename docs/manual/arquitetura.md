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
- Apoio: `readiness.py`, `reconciliation.py`, `verification.py`, `manual_resolution.py`, `line_live.py`. `safe_export.py` e `cli.py` foram removidos em USR-377 S2.

Nota: `materialize` aparece em `ControlStore` (declaracao do Protocol) e em
`SQLiteControlStore` (implementacao); nao e duplicata.

## ReadinessGate (USR-385)

`ReadinessGate.evaluate` avalia o handoff com o `VerificationContext` recebido, sem rede e sem arquivo. `require_delivery` e `mark_delivered` passam por ele. USR-385 partiu `evaluate` nos passos abaixo; o `ReadinessReport` (elegibilidade, razoes, evidencia faltante e dependencias bloqueantes) permanece o contrato publico.

1. Estado e Grill (`_check_state_and_grill`): transicao pedida via `validate_transition`; workflow `cancelled` ou `failed`; contexto ausente (`CONTEXT_REQUIRED`); Grill sem `ready_for_spec`; decisao material sem resposta ou sem origem.
2. Dependencias manuais (`_check_manual_dependencies`): ignora dependencia de outro ticket ou de estagio que o estado pedido nao bloqueia. Dependencia nao `resolved`, sem contexto, sem `resolution_receipt_ref` ou com recibo ausente entra em `blocking_dependency_ids`. O recibo precisa estar `passed`, bater probe/sujeito/ambiente/config/papel e estar dentro da idade maxima (politica ou 86400 s).
3. Aprovacao do plano (`_check_plan_approval`): o digest do plano precisa coincidir; tier `economy` no handoff ou na aprovacao reprova; quando a politica lista papeis, o papel do aprovador precisa estar habilitado.
4. Evidencia (`_check_evidence`): une os ids da politica do estagio com os do handoff aplicaveis. Com contexto, vale o recibo do supervisor (isencao expirada ou operacional na entrega reprova). Sem contexto, vale a evidencia carregada no handoff (ambiente, resultado, identidade, frescor e referencia de auditoria). Entrega sem nenhum id e sem evidencia operacional reprova.
5. Entrega (`_check_delivery`), somente com estado pedido `delivered`: o handoff precisa estar em `independent_review` ou `delivered`, o ambiente precisa ser `target_environment` (`mock_only` nao autoriza) e, com contexto, precisa haver recibo de revisao de um sujeito distinto do implementador e prova `passed` em `target_environment`.

`require_delivery` chama `evaluate` com alvo `delivered` e levanta `ReadinessError` quando `eligible` e falso. O estado de prontidao sai de `_readiness_state`: `operationally_verified` (entrega elegivel), `blocked` (entrega inelegivel, ou workflow cancelado/falho), `waiting_human` (ha dependencia bloqueante), `not_ready`, `validated_in_simulation` (ambiente `mock_only`), `ready_for_spec`, `ready_for_handoff` ou `ready_for_release`.

# Arquitetura

## Modulos removidos (USR-377 S1)

Sem uso em producao (nenhum import fora de testes), removidos em 2026-10-24:
`core/planning/`, `core/pilots/`, `core/security/`, `core/marketing/` e
`scripts/run_practical_activations.py`, junto com seus testes dedicados.
`core/knowledge` (CLI documentada na skill 18 e ligacao com o Segundo Cerebro) e `core/workflow` permanecem.

## Modulos removidos (USR-377 S2)

Sem chamador em producao (apenas testes ou documentos historicos), removidos junto com seus testes dedicados:

- `spikes/` inteiro (laboratorio HF-02 `runtime_choice` e `check_local_usage.py`).
- `core/game/` (motor do Echo Garden; nenhum importador fora de testes).
- `core/models/` do autopilot de modelos: `autopilot`, `discovery`, `discovery_sources`, `grill_policy`,
  `lifecycle`, `openrouter_qualifier`, `pending`, `pr`, `qualification`, `telemetry`; e os scripts
  `discover_models.py`, `model_lifecycle.py`, `qualify_model.py`, `test_openrouter.py`.
- CLIs sem uso: `core/{archetypes,catalog,evolution,infra,notifications,projects,telemetry,workflow,execution,enterprise,portfolio}/cli.py`,
  `core/integrations/telegram_n8n_cli.py`, `core/harness/live_preflight.py`, `core/git/worktree_gc.py`,
  `core/infra/availability_probe.py`, `core/workflow/safe_export.py`.
- Scripts: `backfill_catalog.py`, `download_artworks.py`, `download_expanded_artworks.py`, `probe_production.py`.

## Orquestrador

O pacote `core/orchestrator` guarda a infraestrutura de execução e entrega que a esteira
(`core/line`) e o Hub usam. Ele não decide o fluxo de um ticket: quem decide é `core/line`.

Módulos que permanecem, todos com chamador em produção:

| Módulo | Para que serve | Quem usa |
| --- | --- | --- |
| `guard.py` | Lista de arquivos de governança protegidos | `core/line/protected_edit.py`, `dogfood.py` |
| `delivery.py`, `delivery_executor.py` | Entrega e integração | `core/workflow/integration_handler.py`, `hub/backend/webhooks.py` |
| `build_artifacts.py`, `deployment_adapter.py`, `compose_render.py`, `bluegreen.py` | Artefatos imutáveis, deploy e blue-green | `core/line/stage_release.py`, `scripts/dokploy_*.py` |
| `release_pipeline.py` | Pipeline de release (build, staging, produção, backup) | `core/workflow/release_handlers.py`, `hub/backend/service.py` |
| `cloud_coordinator.py`, `cloud_worker.py`, `cloud_db.py`, `cloud_artifacts.py`, `artifact_retention.py`, `adapters/control_postgres.py` | Coordenação de workers na nuvem e banco de controle | `scripts/start_fleet_worker.py`, `core/line/*` |
| `context.py` | Contexto de aprendizado | `core/learning/service.py` |

Removidos em USR-377 (S3), por não terem nenhum importador em produção, apenas testes:
`cli.py` (CLI vertical DF-15), `release_cli.py`, `state.py` e `models.py` (máquina de estados DF-01,
substituída pelo estado da esteira em `core/line`), `target_journey.py` (a etapa foi fundida em
`build_deploy`) e o arquivo `factory_vertical.config.json`.

Removidos em USR-377 (S2): `runtime.py` e `store.py` (runtime SQLite do experimento HF-02, só usado por
`spikes/runtime_choice`), junto com todo o diretório `spikes/`.

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

## DarkHub remoto: login pelo rodape (USR-406)

Sem sessao, o Hub remoto mostra "Modo leitura" no rodape. Tocar nele ja chama `POST /api/auth/telegram-link`, que cria um pedido pendente (token de consulta secreto, guardado so no navegador que pediu; vale 5 min) e envia ao Telegram do owner a mensagem com o botao "Aprovar" (callback `hublogin:<id>`, aceito so de chat/usuario autorizado) e um link. O navegador consulta `GET /api/auth/telegram-poll` a cada 2 s; ao aprovar, recebe a sessao uma unica vez, guarda e recarrega em modo edicao. Abrir o link no celular tambem libera o celular e o navegador que pediu. `/api/session` agora confirma (200) quem ja envia o token valido; antes devolvia 401 e o frontend apagava o token recem-obtido, deixando o Hub em somente leitura.
