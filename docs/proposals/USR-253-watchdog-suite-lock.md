# USR-253: watchdog do worker nao conta a fila do suite_lock no deadline

Seguimento do USR-178 (secao 6 de `USR-178-gate-resilience.md`). `core/harness/remote_worker.py` e arquivo
protegido: o agente nao commita; o owner publica o patch (OA-041).

## O que muda

`JobManager._run_job` usava `deadline = now + soma(timeout_sec dos passos) + 120 s`, e a espera na fila do
`suite_lock` do host corre dentro desse prazo. Uma fila longa matava o job.

Patch em `docs/proposals/USR-253-watchdog-suite-lock.patch` (so `core/harness/remote_worker.py`):

- `_SUITE_LOCK_ACQUIRED_RE` / `parse_suite_lock_wait_sec(line)`: le o tempo da linha emitida por
  `core/harness/suite_lock.py`: `[SUITE_LOCK] acquired slot N after waiting X.Xs (queue time is not counted ...)`.
  Tolerante a prefixo na linha, espacos extras, inteiros ou decimais; ignora linhas sem o formato.
- `_JobDeadline`: prazo monotonico com lock; `extend_for_line(line)` soma a espera, limitada ao TOTAL
  `DARKFAC_WORKER_SUITE_LOCK_WAIT_CAP_SEC` (padrao 3600; valor invalido/negativo/inf volta ao padrao; `0` desliga).
  `expired()` e usado pelo watchdog no lugar de `time.monotonic() > deadline`.
- `_pump_output` chama `extend_for_line` a cada linha e registra `[WORKER] job deadline extended by Xs ...` no log do job.

Sem a linha (lock livre, `DARKFAC_SUITE_LOCK=off`) o comportamento e identico ao anterior.

## Como aplicar (OA-041)

Worktree a partir de `origin/main`, `git apply docs\proposals\USR-253-watchdog-suite-lock.patch`, commit LOCAL,
portao completo `python C:\dev\DarkFac\core\harness\runner.py --quick` (deve imprimir `[HARNESS_PASS]`), push,
conferir CI da main.

## Como validar

```powershell
python -m pytest tests/test_watchdog_suite_lock_wait.py tests/test_remote_job_recovery.py tests/test_remote_restart_retry.py tests/test_remote_worker_process_tree.py tests/test_suite_lock.py tests/test_gate_lock_timing.py -q -p no:cacheprovider -n0
```

`tests/test_watchdog_suite_lock_wait.py` (13 testes) faz skip limpo ate o patch ser aplicado (detecta `_JobDeadline`).
Cobre parse, relogio falso (fila de 500 s com passos de 100 s nao expira), teto, env, e um `_run_job` real com Popen
falso cuja fila ultrapassa o timeout do job (sobrevive) e controle sem a linha (e morto). Sem sleeps longos; sem
suposicoes de SO.
