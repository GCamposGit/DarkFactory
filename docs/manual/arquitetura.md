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

## Roteamento (USR-378)

O unico roteador de producao e `core/line/routing.pick`. Os roteadores
paralelos `core/router/model_router.py` e `core/router/harness_router.py` foram
removidos (sem chamador fora de testes). `core/router/token_budget.py` fica
porque `pick` e o monitor de tokens usam `_quota_headroom`;
`core/benchmarks/router.py`, `core/portfolio/router_optimizer.py` e
`core/workflow/qualified_routes.py` permanecem por terem chamadores vivos.
## Backend do Hub (USR-377 S7b)

`hub/backend/service.py` e uma fachada fina: define `HubService` (construtor, cache da
linha ao vivo) e reexporta a API publica (`HubService`, `DarkHubService`, guarda SSRF).
A logica vive em mixins por dominio, um modulo irmao cada: `service_line.py` (linha,
dashboard, demandas/grill, test workers), `service_interventions.py` (intervencoes e
owner actions), `service_progress.py` (roadmap, progresso, portfolio), `service_catalog.py`
(catalogo de servicos, sessao, probes, launch), `service_usage.py` (telemetria, cotas,
infra), `service_ai.py` (Ollama/OpenRouter, benchmarks, learning packs, content, visual),
`service_integrations.py` (webhooks, Telegram, n8n, HF15, notificacoes) e `service_net.py`
(guarda SSRF). Testes que fazem monkeypatch no modulo `service` (ex.: `read_line_live`)
continuam validos porque `get_line_live` permanece em `service.py`.
