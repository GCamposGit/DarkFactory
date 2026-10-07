# Relatório de Entrega - USR-123

## Resumo da Demanda
- **Ticket**: USR-123
- **Título**: Linha: run que estourou o wall-clock fica em waiting_human para sempre e /linha nao abre nova tentativa
- **Status**: Completed

## Contexto & Causa Raiz
Em 01/10 o `run-83bd3712924d` (USR-62 tentativa 4) ficou repetidamente sem rota de escrita disponível e, ao exceder o teto de `run_caps.wall_clock_hours` (6 horas a partir de `created_at`), o estágio de desenvolvimento transicionou para `waiting_human(no_route_available)`.
Após a resolução da infraestrutura (USR-121), retomar o job não permitia avanço porque qualquer re-tentativa com `not_before` estourava o wall-clock e falhava imediatamente. Além disso, `owner_intake.run_state` tratava `waiting_human` indefinidamente como em voo (`in_flight`), impedindo o comando `/linha USR-62` de abrir uma nova tentativa. Não existia também mecanismo operacional para o Owner cancelar runs travados/mortos na linha de produção (nem no store Postgres nem no Telegram nem no DarkHub).

## Solução Implementada

1. **Método Canônico `cancel_run` nos Stores de Controle (`core/workflow/control_store.py` e `core/orchestrator/adapters/control_postgres.py`)**:
   - Adicionado ao protocolo `ControlStore` e implementado em `SQLiteControlStore` e `PostgresControlStore`:
     - Transiciona atomicamente todos os jobs abertos (`pending`, `running`, `waiting_human`, `waiting_dependency`) para `cancelled`, registrando `cause_code = reason`, `finished_at` e limpando leases.
     - Libera claims ativos (`status = 'released'`).
     - Transiciona o run para `cancelled` com `completed_at` e `updated_at`.
     - Registra evento de auditoria no outbox (`event_type = 'run_cancelled'`) com actor, motivo e contagem de jobs cancelados.

2. **Cancelamento e Nova Tentativa na Camada de Intake (`core/line/owner_intake.py`)**:
   - `cancel_line_run(target, ...)`: Resolve se o alvo é `ticket_id` (ex.: `USR-62`) ou `run_id` (ex.: `run-xxx`), localiza a tentativa mais recente daquele ticket no store e cancela o run.
   - `run_state`: Atualizado para tratar `status == 'cancelled'` como terminal (`failed`), permitindo que chamadas subsequentes a `submit_ticket_to_line` abram uma nova tentativa (`attempt + 1`).
   - `_expired_no_route_wait`: Trata como terminado (`failed`) um run cujo wall-clock expirou e cujo único job aberto é `waiting_human(no_route_available)`, destravando o ticket para nova tentativa.

3. **Comando do Owner no Telegram (`core/integrations/telegram.py` e `hub/backend/service.py`)**:
   - Implementado suporte aos comandos `/cancelar <ticket_id|run_id>` e `/cancel <ticket_id|run_id>`.
   - Incluído no `/help` tanto para o bot `@darkfac_bot` quanto para `@darkfac_ops_bot`.
   - Wire em `HubService._setup_telegram_gateway` via `cancel_handler`.

4. **Endpoints de Cancelamento no DarkHub API (`hub/backend/api.py`)**:
   - `POST /api/demands/tickets/{ticket_id}/line/cancel`
   - `POST /api/line/runs/{run_id}/cancel`

5. **Suíte de Testes Dedicada (`tests/test_line_cancel.py`)**:
   - 7 testes automatizados cobrindo cancelamento em SQLite, tentativa de cancelar run inexistente, cancelamento por ticket ID abrindo tentativa 2, cancelamento por run ID, comando do Telegram `/cancelar`, endpoints da API HTTP do Hub e resolução de wall-clock expirado.

## Validações Executadas
- `tests/test_line_cancel.py`: 7 passed (100% de sucesso).
- `tests/test_line_owner_intake.py` + `tests/test_line_owner_intake_expired.py`: 33 passed.
- `tests/test_telegram_dual_bot.py`: 8 passed.
- `tests/test_dependencies_contract.py`: 5 passed.
