# SPEC

## Objetivo
Fazer o worker de teste remoto detectar por conta propria um job ativo sem progresso (CPU agregada da arvore do runner < 5% com 100% = 1 nucleo, e sem nova saida de log do job) por 10 minutos continuos, fechar esse job como perdido com error_code dedicado `worker_stalled`, matar a arvore de processos, reiniciar o proprio daemon preservando host/porta/node_id, registrar o evento em telemetria/DarkHub e alertar no Telegram ops; e fazer o dispatcher reenfileirar o job perdido pelo caminho existente de [REMOTE_RETRY], sem duplicidade e sem veredito de teste.

## Fora de escopo
- Matar ou reciclar job com progresso real (CPU acima do limiar ou nova saida de log do job)
- Recuperar esperas legitimas verificaveis (fila do suite_lock): a janela de estagnacao fica suspensa enquanto o job esta comprovadamente na fila do lock
- Reiniciar a maquina/VM ou executar atualizacao de codigo (POST /system/update) como parte da auto-recuperacao
- Alterar o cascade da esteira, o portao oficial, politica de cota ou o fluxo de fallback local do runner
- Novo canal de notificacao: reutiliza NotificationService (DarkHub + Telegram ops) ja existente
- Auto-recuperacao de workers de harness de IA (/harness/execute, codex/grok/claude) - o alvo e o job de teste do JobManager

## Desenho
1) Novo modulo `core/harness/job_stall.py` com `JobStallWatch`, puro e testavel: recebe clock, amostrador de CPU da arvore e um contador de progresso de log; mantem o inicio da janela e devolve `StallVerdict` (estagnado + evidencia: cpu_percent medido, segundos sem progresso, tamanho da arvore). Limiares como constantes com override por env: `DARKFAC_JOB_STALL_AFTER_SEC` (default 600.0), `DARKFAC_JOB_STALL_CPU_PCT` (default 5.0, normalizado em 1 nucleo = 100% via `cpu_delta_sec / wall_delta_sec * 100`) e `DARKFAC_JOB_STALL_WATCH=off` como kill switch. Qualquer amostra indisponivel (psutil ausente, processo sumiu, acesso negado) e tratada como 'nao ha evidencia de estagnacao' (fail-open) e reinicia a janela.
2) Reuso de amostragem: a logica de CPU acumulada por arvore ja existe em `suite_lock._sample_holder_tree` (USR-147). Extrair o amostrador para `core/harness/process_identity.py` (`sample_tree_cpu_seconds`) e transformar `_sample_holder_tree` em wrapper fino, mantendo o comportamento e os testes atuais do suite_lock verdes.
3) Progresso do job: `JobManager._append_log` passa a incrementar um contador `job.progress_seq` SOMENTE para saida real do runner; notas do proprio worker (prefixo `[WORKER]`) e consultas de saude nao contam (`progress=False`). Espera legitima: um regex de `[SUITE_LOCK] waiting` (sem `acquired` posterior) suspende a janela, reutilizando os marcadores que `parse_suite_lock_wait_sec`/`_JobDeadline` ja interpretam.
4) O `_watchdog` de `JobManager._run_job` passa a consultar o `JobStallWatch` a cada `JOB_TREE_SNAPSHOT_SEC`; ao veredito estagnado: anexa linha de evidencia `[WORKER worker_stalled] ...`, `tree.kill(force=True)`, marca `job.interrupted = True` e `_set_status(job, 'failed', error_code=ERROR_CODE_WORKER_STALLED)` (sem veredito de teste, log e record persistidos como hoje), dispara telemetria/alerta e chama o hook de restart.
5) Restart: `create_worker_app` injeta em `job_manager.restart_hook` um callable que chama `trigger_daemon_restart(root_path, host, port, node_id)` (mesmo mecanismo do `POST /system/restart`, que ja usa `mark_interrupted`), preservando host, porta e node_id; o hook e injetavel nos testes. Nada de `os.execv` novo nem de reinicio de maquina.
6) Dispatcher: `ERROR_CODE_WORKER_STALLED = 'worker_stalled'` entra em `INFRASTRUCTURE_ERROR_CODES` e `RESTART_RETRY_ERROR_CODES` de `remote_dispatch.py`; assim `_finalize_remote_result` converte o job em `RemoteJobLost` (nunca `[HARNESS_FAIL]`) e `_dispatch_with_restart_retry` imprime `[REMOTE_RETRY]`, espera o worker voltar (`_wait_for_worker_back` + `_await_worker_stable`) e resubmete no mesmo worker ou cai para o proximo/local, reaproveitando limites, dedup de submit e idempotencia atuais.
7) Telemetria/alerta: helper `record_worker_stall(...)` usando `NotificationService(telegram_role='ops').notify(category=AlertCategory.SYSTEM_HEALTH, severity=CRITICAL, provider_id=f'worker:{node_id}', details={...})`, que ja persiste no store do DarkHub e envia Telegram com cooldown anti-spam; toda a chamada envolvida em try/except + log, para que falha de Telegram/store nunca bloqueie a recuperacao.

## Arquivos a tocar
- core/harness/job_stall.py
- core/harness/remote_worker.py
- core/harness/process_identity.py
- core/harness/suite_lock.py
- core/harness/remote_dispatch.py
- tests/test_worker_stall_recovery.py
- tests/test_remote_dispatch_stalled_retry.py

## Riscos
- Falso positivo matando job com progresso real: pytest-xdist pode ficar minutos sem imprimir em suite longa; mitigado pelo criterio conjuntivo (CPU da arvore E ausencia de saida), janela continua de 10 min e fail-open quando a amostra nao e confiavel
- Espera legitima no suite_lock (ou em I/O) parecer estagnacao: mitigado suspendendo a janela enquanto a fila do lock esta comprovada no log e mantendo o limiar sobre a arvore inteira, nao sobre a CPU global
- Diferenca Windows/Linux na identidade de processo e em cpu_times: mitigado reutilizando `same_process`/`proc_start_ticks` ja endurecidos (USR-317) em vez de nova deteccao
- Loop de reinicio: um worker que estagna sempre poderia reiniciar sem parar; mitigado por limiar de janela continua, pelo cooldown anti-spam do alerta e pelos limites de retry do dispatcher (`worker_restart_retries`)
- Refatorar o amostrador compartilhado pode regredir a deteccao de holder estagnado do suite_lock; mitigado mantendo `_sample_holder_tree` como wrapper e nao mexendo nos testes existentes
- Testes com processos e threads reais tendem a flakiness; mitigado por clock/amostrador/hook injetados nos testes de unidade e um unico teste de integracao curto com processo real
