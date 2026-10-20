# USR-306 - Disponibilidade da frota: reservas nao renovadas e saude de host presumida

Auditoria de 17/10/2026: o Postgres tinha 155 reservas expiradas (149 grokbox, 6 notebook), nenhuma com
`heartbeat_at > acquired_at`; duas renovadas estavam em outros estados. Este relatorio cobre a parte de codigo.
Nao ha afirmacao de ganho 3x: o que e comprovado por teste e o que falta provar no ambiente real estao separados abaixo.

## 1. O que foi encontrado (evidencia no codigo, antes da correcao)

A reserva de ciclo (`fleet_reservations`) e criada pelo coordenador (`reserve_cycle`, TTL = `lease_duration_sec`,
45 s por padrao) e, antes desta correcao, so era renovada em um unico ponto:

`CloudWorker._start_lease_heartbeat` (`core/orchestrator/cloud_worker.py`), thread de fundo iniciada dentro de
`_execute_stage_in_scope` enquanto um handler de estagio roda.

Lacunas que, somadas, explicam reservas que expiram sem nenhuma renovacao:

1. **Primeira renovacao tardia.** O laco esperava `interval = max(5, lease/3)` (15 s com TTL 45 s) antes do primeiro
   `heartbeat_reservation`. Estagios mais curtos que 15 s (planejamento, roteamento, gate rapido) terminavam sem
   renovar uma unica vez. O `claim` (SQLite e Postgres) tambem nao tocava a reserva.
2. **Nenhuma renovacao entre estagios.** Depois que o job de um estagio termina, o sucessor fica `pending`
   (ou `waiting_dependency`/espera de CI) sem claim ativo. Nao ha thread de heartbeat nesse intervalo e o coordenador
   so varria (`reconcile_fleet_reservations`), nunca renovava. Qualquer intervalo > TTL expirava a reserva.
3. **Falha engolida.** A renovacao ficava dentro de `try/except` com `logger.debug`, e so rodava se `claim.reservation_id`
   resolvia por `get_reservation`. Erros (reserva expirada, fencing) ficavam invisiveis.
4. **Efeito em cascata.** `claim` pula jobs cuja reserva `active` ja passou de `expires_at`; depois da varredura
   a reserva vira `expired` e o job cai no caminho legado (`reservation_id = res-xxxx`, sem reserva real, sem
   capacidade, sem renovacao). Por isso as reservas expiradas nunca tiveram `heartbeat_at > acquired_at`.
5. **Saude presumida.** `CloudCoordinator.admit_eligible_cycles` tratava `host_health_prober is None` como host disponivel,
   e `claim` aceitava jobs sem reserva ativa pelo caminho legado, contornando a capacidade 1 por host / 3 na frota.
6. **Projecao.** `core.workflow.line_live._build_fleet(None, now)` publicava `grokbox/notebook/desktop` como `online`
   sem qualquer fonte de saude.

Nao se atribui a causa a um unico timeout de sonda; a evidencia acima e estrutural (caminho de renovacao incompleto).
A distribuicao 149 grokbox / 6 notebook e coerente com a prioridade de admissao (grokbox primeiro), nao prova causa.

## 2. O que foi corrigido

- **Renovacao no claim** (`SQLiteControlStore.claim`, `PostgresControlStore.claim`): ao casar a reserva do ciclo, o mesmo
  commit atualiza `heartbeat_at`/`expires_at`. Estagios curtos agora renovam pelo menos uma vez.
- **Heartbeat do worker** (`CloudWorker._beat_reservation`): renovacao imediata ao iniciar o handler (usando o relogio do
  claim), resolucao da reserva pelo vinculo do claim e, na falta, pela reserva ativa do run; falha vira `warning`.
  `HEARTBEAT_MIN_INTERVAL_S` passou a atributo de classe (testavel).
- **Renovacao entre estagios / espera** (`renew_open_run_reservations`, SQLite e Postgres, chamado no inicio de
  `CloudCoordinator.admit_eligible_cycles`): renova reservas ativas e nao expiradas de runs com job
  `waiting_dependency` ou `pending` pronto ha ate 300 s (`RESERVATION_PENDING_GRACE_SEC`). Passada a janela sem
  claim (host morto), a reserva deixa de ser renovada e expira. Reserva ja expirada nao ressuscita.
- **Modo gerenciado** (`DARKFAC_FLEET_MANAGED=1`, `fleet_managed_enabled()` em `core/workflow/control_contracts.py`):
  - o coordenador so admite host com saude verificada (`prober` ausente, `None`/stale, `False` ou excecao => nao elegivel);
  - `claim` nao usa o caminho legado: job sem reserva ativa nao e reivindicavel.
  Sem a variavel (modo legado/nao gerenciado) o comportamento anterior e mantido e testado.
- **`_build_fleet`**: sem fonte de saude o host fica `unknown` (padrao do modelo tambem); `online` so com reserva ativa,
  nao expirada, com `heartbeat_at` ate 120 s (`FLEET_HEALTH_FRESH_SEC`); `draining` continua pelo arquivo de drain.
- Teste existente `test_fencing_prevents_old_owner_from_finishing_job` ajustado: o claim agora renova a reserva, entao
  o teste a expira alem do TTL renovado (a semantica de fencing testada e a mesma).

Nao alterado: fencing, leases, exclusao de suite, prioridades de host, slots por maquina.

Testes: `tests/test_usr306_fleet_availability.py` (handler longo com renovacao repetida, handler curto com 1 renovacao,
claim renovando, coordenador renovando entre estagios, modo gerenciado vs legado, `_build_fleet`).

## 3. O que ainda precisa ser provado no ambiente real

1. Ligar `DARKFAC_FLEET_MANAGED=1` nos workers e no coordenador e ligar uma sonda real de saude por host
   (`host_health_prober`) com idade maxima; hoje nenhum processo de producao define o modo nem a sonda por este ticket.
2. Tres tickets simultaneos em tres hosts distintos (grokbox, notebook, desktop), cada um com `heartbeat_at > acquired_at`
   na tabela `fleet_reservations` durante todo o ciclo.
3. Reserva mantida durante um handler longo (>= 10 min) e durante espera de CI real, sem expirar.
4. Duas reposicoes de vaga (ao terminar/estacionar um ciclo, o coordenador admite o proximo na mesma maquina).
5. Contagem de reservas `expired` sem renovacao deve parar de crescer apos o deploy (consulta no Postgres).
6. Texto do estado `unknown` no Hub: a UI do Hub nao esta neste repositorio; o campo `fleet.hosts[].status` agora pode
   valer `unknown` e o Hub precisa exibi-lo (ex.: "sem saude verificada").
