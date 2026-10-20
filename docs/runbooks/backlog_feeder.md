# Alimentador de backlog (USR-304, absorve USR-221)

O alimentador (`core/line/backlog_feeder.py`) e acionado por `core/line/feeder_cycle.py`
dentro do loop de supervisao do `CloudCoordinator` (`scan_and_recover_pending`, passo 4b).
Ele e o despachante de 15 min descrito em USR-221: nao existe cron separado. USR-221 foi
marcado `cancelled` no ledger, coberto por este ciclo.

## Comportamento por ciclo

1. Le o ledger (`.factory/demands/demands.json` na raiz do repo, ou `DARKFAC_DEMANDS_PATH`).
2. Avalia tickets abertos (projeto registrado, G1, dependencias, tentativas, runs em andamento).
3. Admite `max_ativos - em_andamento` tickets elegiveis pelo mesmo `submit_ticket_to_line`
   (idempotente por `ticket:<id>`; replay e dois coordenadores nao duplicam).
4. Registra uma linha de log `feeder_cycle {...}` com contagens, `skipped` por motivo
   (`g1_ambiguity`, `unmet_dependencies`, `in_flight`, `quota_floor`, ...) e `idle_reason`
   (`no_eligible_ticket`, `no_free_slot`, `quota_floor...`, `store_error`).

## Configuracao

| Variavel | Padrao | Efeito |
| --- | --- | --- |
| `DARKFAC_FEEDER_ENABLED` | ligado (so com `DARKFAC_HF02_DATABASE_URL`) | `0` desliga |
| `DARKFAC_FEEDER_INTERVAL_SEC` | `900` | intervalo entre ciclos |
| `DARKFAC_FEEDER_MAX_ACTIVE` | `DARKFAC_MAX_CONCURRENT_SLOTS` (3) | teto de runs simultaneos |
| `DARKFAC_FEEDER_QUOTA_GATE` | desligado | `1` nao admite nada se nenhum harness estiver acima do piso de 15% |
| `DARKFAC_DEMANDS_PATH` | ledger do repo | caminho do ledger como o servico o enxerga |

O piso de cota de 15% continua aplicado tambem pelo roteador de cada estagio do worker.

## Verificar em producao

Procure nos logs do coordenador por `feeder_cycle`; duas linhas sucessivas com `submitted > 0`
(ou `idle_reason` explicando a fila parada) comprovam a reposicao.
