# Relatório de Execução: HF-13 — Trilha DarkHub: Visibilidade, Filas e Ingestão HF/INFRA

- **Ticket**: `HF-13`
- **Título**: DarkHub central: fila real, HF/INFRA, diagnóstico do erro 404 e observabilidade
- **Status**: `COMPLETED`
- **Módulo**: `hub`, `core.roadmap`
- **Data**: 2026-09-11
- **Escopo**:
  1. Diagnóstico e resolução da incompatibilidade de rotas e erro 404 em `/api/tasks/dashboard`.
  2. Implementação dos adaptadores de ingestão canônica para as fontes HF (`HybridWorkflowPlanSource`) e INFRA (`InfraRoadmapJsonSource`).
  3. Integração na projeção *read-only* do DarkHub com preservação estrita da retrocompatibilidade.

## 1. Diagnóstico e Resolução do Erro 404 em `/api/tasks/dashboard`

### Causa Raiz Observada
1. **Falta de Aliases na Raiz**: O `api_router` em `hub/backend/api.py` usava `prefix="/api"`. Requisições diretas de proxies reversos, clientes ou probes a `/tasks/dashboard` ou `/tasks` (sem `/api`) retornavam `HTTP 404 Not Found`.
2. **Trailing Slash**: Chamadas com barra no final (`/api/tasks/dashboard/` ou `/tasks/dashboard/`) não possuíam rota mapeada.
3. **Bloqueio Operacional em HF-01**: Na sessão de 08/09/2026, o DarkHub em execução retornou 404 porque o processo havia sido iniciado antes da introdução dos endpoints do DF-21 e a regra de governança de HF-01 proibia explicitamente reiniciar o Hub ("Não reiniciar o Hub").
4. **Resiliência do Frontend**: O script `hub/frontend/tasks.js` não possuía mecanismo de fallback caso o proxy estivesse roteando na raiz.

### Correções Aplicadas
- Adição de rotas e aliases em `hub/backend/api.py` para `/tasks/dashboard`, `/tasks/dashboard/`, `/tasks` e `/tasks/`.
- Mapeamento de rotas de nível raiz em `hub/backend/main.py` para `/tasks/dashboard`, `/tasks/dashboard/`, `/tasks` e `/tasks/`.
- Inclusão de fallback resiliente no frontend `hub/frontend/tasks.js` que tenta `/tasks/dashboard` caso `/api/tasks/dashboard` retorne 404.

## 2. Ingestão das Fontes HF e INFRA

### Adaptadores Implementados
1. **`HybridWorkflowPlanSource` (`core/roadmap/sources.py`)**:
   - Lê `docs/HYBRID_WORKFLOW_PLAN_2026-09-08.md`.
   - Faz o parsing determinístico da Onda 1 (`HF-01` a `HF-15`) e Onda 2 (`HF-20` a `HF-25`).
   - Mapeia dependências causais explícitas (ex: `HF-02` depende de `HF-01`; `HF-13` depende de `HF-05`; Onda 2 depende de `HF-15`).
   - Vincula relatórios de evidência em `.factory/reports/` (`hf-*-report.md`), marcando status `COMPLETED` quando há evidência de conclusão e `PLANNED` caso contrário.
2. **`InfraRoadmapJsonSource` (`core/roadmap/sources.py`)**:
   - Lê `.factory/infra/roadmap.json` e mapeia pré-requisitos de `.factory/infra/roadmap.md`.
   - Projeta os itens `INFRA-01` a `INFRA-11` com `item_type=RoadmapItemType.INFRASTRUCTURE` e tags no escopo do projeto `darkfac`.
   - Mapeia status `delivered` para `COMPLETED` e `planned` para `PLANNED`.

### Integração no DarkHub
- `core/roadmap/service.py`: `build_repository_roadmap_service` agora suporta `include_hf: bool = False` e `include_infra: bool = False`, preservando retrocompatibilidade total com as suítes de teste existentes.
- `hub/backend/service.py`: `HubService.__init__` instancia a projeção do roadmap com `include_hf=True` e `include_infra=True`, integrando automaticamente todo o grafo ao cockpit.
- `core/roadmap/cli.py`: Suporta `--include-hf`, `--include-infra`, `--include-demands` e `--all`.

## 3. Evidências de Validação

- `tests/test_hub_hf13.py`: 5 testes cobrindo parsing de ondas HF, itens INFRA, compilação de snapshot unificado, endpoints HTTP do DarkHub (com e sem `/api`, com e sem `/`) e CLI.
- `tests/test_task_dashboard.py`: 3 testes cobrindo projeção da fila e variantes de rota.
- `tests/test_roadmap.py`: 14 testes cobrindo estabilidade da baseline e compilador.
- `tests/test_hub.py`, `tests/test_hub_access.py`, `tests/test_darkhub_cloud_gateway.py`: 37 testes cobrindo integridade do Hub e gateway.
