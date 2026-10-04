# Relatório de Conclusão: USR-105 e USR-106 (Onda 1 — Line & Autonomia)

**Data**: 2026-10-04  
**Tickets**: USR-105, USR-106  
**Status**: Completed  
**Branch**: `feat/wave1-line-route-waiter-and-human-probe`  

---

## 1. Contexto e Motivação

Durante os reviews de estabilidade da Onda 1 (planos HF-28 e ADRs associados), foram identificadas duas lacunas críticas na cadeia de autonomia e tolerância a falhas da fábrica:

1. **USR-105 (Retomada Autônoma de `waiting_human` Não-Grill)**:
   - *Problema*: `core/line/human.py` definia `probe_and_resume` e `probe_cmd` (ex.: `python -m core.git.autonomy check-main`), mas nenhum serviço ou daemon agendado os executava periodicamente. HumanRequests de `kind="infra"` (criados por esgotamento de quota ou falha de base_red) prometiam retomada automática ao restabelecer o ambiente, porém ficavam travados aguardando intervenção manual do operador.
   - *Solução*: Implementado varredor periódico `sweep_waiting_human_requests` no `CloudWorker` (com cadência configurável `human_probe_interval_s`, padrão 600s / 10 min), que consulta `list_waiting_jobs(statuses=("waiting_human",))` via `ControlStore`, filtra requisições automáticas (`kind != "grill"` e `!= "commercial_acceptance"`), executa a sonda determinística com timeout curto (15s) e retoma via `resume_blocked_job` de modo transacional e protegido por concorrência.

2. **USR-106 (Substituição de Retries Quentes e Loop Cap por `RouteWaiter`)**:
   - *Problema*: O `RouteWaiter` (entregue no USR-87) havia sido adotado nas etapas centrais de IA (`development`, `independent_review`, `grill`, `planning`), mas dois pontos críticos continuavam com comportamento legado:
     a) `cloud_worker.dispatch_claimed_job`: quando nenhum `harness:*` estava disponível, adiava por +10 min fixos, o que em sucessores estourava o relógio e virava `failed(loop_cap)` terminal (sem acionar suporte humano nem esperar o reset).
     b) `stage_integration._resolve_conflict`: sem rota, devolvia `AgentResult(not_installed)` gerando retries quentes imediatos (até 30 iterações em loop) e gravando a marca de tentativa durável, queimando a cota única de resolução de conflito.
   - *Solução*: Ambos os componentes migrados para o `core.line.route_wait.RouteWaiter`, retornando retry com `not_before` determinístico até o teto do relógio do run, e apenas depois estacionando em `waiting_human(kind="infra")`. O marcador de tentativa de merge no git agora só é gravado e enviado se houver rota de agente eleita.

---

## 2. Modificações de Código

1. **`core/workflow/control_store.py` & `core/orchestrator/adapters/control_postgres.py`**:
   - Adicionado o método `list_waiting_jobs(statuses=("waiting_human", "waiting_dependency"))` ao protocolo `ControlStore`, `SQLiteControlStore` e `PostgresControlStore`.

2. **`core/orchestrator/cloud_worker.py`**:
   - Adicionados `human_probe_interval_s`, `request_loader`, `human_probe_runner` e `route_waiter` aos parâmetros injetáveis de `CloudWorker.__init__`.
   - Adicionado `sweep_waiting_human_requests()` e integração no `poll_and_execute_once()`, disparando o varredor no início do ciclo caso decorrido o intervalo.
   - Atualizado `dispatch_claimed_job()` para rotear através de `RouteWaiter.no_route_result()`.

3. **`core/line/stage_integration.py` & `core/line/bindings.py`**:
   - `IntegrationStageHandler` agora recebe `route_waiter` opcional (injetado via `IntegrationStageAdapter` e `build_line_registry`).
   - `_resolve_conflict()` verifica `pick("development", ...)`; caso nulo, chama `self.route_waiter.no_route_result()`. A marca de tentativa (`_ATTEMPT_JOB_SUFFIX`) só é gravada e commitada se a rota for encontrada.
   - `_rebase_onto_default()` trata o retorno de `StageResult` sem queimar tentativas.

4. **`tests/line/test_no_route_contract.py`**:
   - `stage_integration.py` adicionado a `AGENT_STAGE_MODULES` e removido de `NOT_A_ROUTE_PARK`.
   - `cloud_worker.py` incluído na varredura AST determinística `_violations()`.
   - Adicionado `test_cloud_worker_routes_missing_harness_through_waiter()`.

5. **`tests/test_cloud_worker_active_loop.py` & `tests/line/test_stage_integration.py`**:
   - Cobertura completa de varredura: teste com sonda verde (retomada com sucesso para pending), sonda vermelha (permanece em espera), filtro de grill/aceitação comercial, blindagem contra exceções de probe, controle de intervalo e dispatch sem rota via RouteWaiter.
   - Teste de integração de conflito sem rota validando que nenhuma marca de tentativa é commitada no git.

---

## 3. Evidências de Teste

- `python -m pytest tests/line/test_no_route_contract.py`: 15 passed in 2.37s.
- `python -m pytest tests/test_canary_first_run_fixes.py`: 29 passed in 11.67s.
- `python -m pytest tests/test_cloud_worker_active_loop.py`: 10 passed in 10.25s.
- `python -m pytest tests/line/test_stage_integration.py`: 18 passed in 258.07s.
- `python core/orchestrator/guard.py origin/main`: `[GUARD PASS] No protected governance paths modified.`
