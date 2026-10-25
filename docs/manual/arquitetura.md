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
