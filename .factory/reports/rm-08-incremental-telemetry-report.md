# RM-08 — Atualização Incremental, Telemetria e Testes de Escala

## Resultado

O ticket **RM-08** foi implementado com sucesso em worktree dedicada (`codex/rm-08-incremental-telemetry`), tornando a infraestrutura do roadmap operacionalmente confiável sob carga densa, introduzindo rastreamento de mudanças de fontes, métricas de observabilidade de latência/cache e validando formalmente o orçamento de latência para grafos de 500 itens e 1.500 relações.

## Entregas

1. **Observabilidade e Telemetria (`core/roadmap/store.py`)**:
   - Estrutura `StoreTelemetry` capturando: requisições totais (`requests_total`), acertos de cache (`cache_hits`), falhas (`cache_misses`), taxa de acerto (`hit_ratio`), latência da última compilação (`last_compile_latency_ms`), média móvel de latência (`average_compile_latency_ms`), contagem de itens/relações e alterações detectadas nas fontes (`source_changes_detected`).
   - Método `RoadmapSnapshotStore.get_telemetry(project_id)` com suporte a agregação multiprojeto.

2. **Detecção de Mudanças e Invalidação Incremental (`core/roadmap/store.py`)**:
   - Rastreamento por impressão digital (`source_fingerprint`) para identificar mutações incrementais nas fontes canônicas.
   - Preservação do caminho aquecido (< 10ms) para consultas repetidas com invariância de fingerprint.

3. **Integração na Camada de Serviço e Health (`core/roadmap/service.py`, `core/roadmap/models.py`)**:
   - Adicionado campo opcional e retrocompatível `telemetry` ao contrato `RoadmapHealth`.
   - Método `RoadmapQueryService.get_telemetry(project_id)` exposto para a API do Hub e CLI.

4. **Suíte Focal de Escala e Estresse (`tests/test_roadmap_scale.py`)**:
   - `test_dense_graph_500_items_1500_relations_latency_budget`: Gera DAG denso sintético com 500 itens e 1.500 relações de dependência entre 4 estágios do ciclo de vida. Valida que a compilação completa ocorre em < 500ms e consulta em cache em < 20ms.
   - `test_store_telemetry_source_change_detection`: Comprova a detecção de mudança de revisão de fonte e contadores de hit/miss.
   - `test_service_health_exposes_telemetry`: Comprova que a sondagem de saúde expõe telemetria em tempo real.

## Validação Executada

- `python -m pytest tests/test_roadmap_scale.py -v` — **3 passed** (0.14s).
- `python -m pytest tests/test_roadmap.py -v` — **14 passed** (0.39s) — regressão zero.
