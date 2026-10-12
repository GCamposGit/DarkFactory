# Proposta: o portao nao reprova por concorrencia e o worker silencioso cai limpo para o local (USR-178)

`core/harness/*` e protegido por `core/orchestrator/guard.py`: o job `trusted-pr-policy` do CI reprova qualquer PR que o altere, e so o owner aplica por commit direto. Por isso a fabrica NAO altera esses arquivos: este documento e o patch ao lado (`docs/proposals/USR-178-gate-resilience.patch`) trazem a mudanca ja testada. `harness.config.json` e `core/harness/remote_worker.py` NAO estao no patch (ver secoes 3 e 6).

O patch toca 3 arquivos protegidos e os testes novos vao como `.txt` (se estivessem em `tests/` na branch eles reprovariam o portao, porque so passam depois do patch); o owner os copia para `tests/` no mesmo commit:

| Arquivo | Papel |
|---|---|
| `core/harness/runner.py` | telemetria por execucao (`[RUN_TIMING]` + `run_timings.jsonl`), lock cronometrado, contabilidade do tempo remoto perdido |
| `core/harness/remote_dispatch.py` | worker silencioso: confirmacao de morte por varias sondas (retomada), diagnostico estruturado ampliado, cancelamento do job abandonado, `[REMOTE_FALLBACK]` |
| `core/harness/suite_lock.py` | `SuiteLock.waited_sec` e `SuiteLockTimeout.waited_sec`; linha `[SUITE_LOCK] acquired slot N after waiting Xs` |
| `docs/proposals/USR-178-test_gate_lock_timing.py.txt` -> `tests/test_gate_lock_timing.py` (novo, 8 testes) | criterios 1 e 3 |
| `docs/proposals/USR-178-test_worker_silence_resume.py.txt` -> `tests/test_worker_silence_resume.py` (novo, 15 testes) | criterio 2 |

## 1. Problema e evidencia

Ticket USR-178 (`.factory/demands/demands.json`): o portao local estoura o limite do passo paralelo sob concorrencia e o worker do Desktop (`http://100.78.181.90:8080`) silencia no passo paralelo.

- 08-09/10/2026 (USR-168): `runner.py --quick` falhou duas vezes com `STEP_FAIL unit_and_integration_tests_parallel` (timeout 1502 s e 1506 s, `TEST_COUNT 0`, exit 124).
- 11-12/10/2026 (USR-225 e USR-245), ja com o limite em 3000 s (OA-011): o portao perdeu o worker no meio do passo (`worker stopped responding for >75s mid-run and /health is unreachable`, `REMOTE_JOB_LOST worker_unreachable`) e caiu para a execucao local, que levou 2971 s e 3002 s e estourou o limite.

Diagnostico no codigo atual de `origin/main` (leitura de `runner.py`, `remote_dispatch.py`, `suite_lock.py`, `remote_worker.py`):

1. A espera no `suite_lock` NAO conta contra o `timeout_sec` do passo no caminho local: o patch USR-141/147 (ja aplicado) toma o lock em `run_with_cache` antes de `execute()` nos dois ramos (com e sem cache), e `run_step` so cronometra o subprocesso. Prova (secao 5): com o codigo atual, fila de 3 s e timeout de passo de 2 s, o portao passa. O que falta e o REGISTRO desse tempo e um teste de regressao.
2. `_stream_job` ja distingue worker lento (`/health` responde: espera ate o teto de 180 s) de worker morto, e ja registra `[REMOTE_JOB_LOST]` + `<estado>/remote_jobs/<job>.json`. Falta: (a) UMA unica sonda `/health` perdida (a 75 s de silencio, com timeout de 10 s) declarava o worker morto e jogava uma suite de ~50 min no fallback local, enquanto o job remoto seguia vivo (custo assimetrico: esperar mais 2-3 sondas custa segundos; abandonar errado custa uma suite inteira); (b) o diagnostico nao dizia em que passo o job estava, quanto tempo ja tinha gasto remotamente, quantas sondas falharam, nem se o job foi cancelado; (c) o job abandonado ficava rodando no Desktop (o proximo portao ainda esperava ate 300 s por `busy`); (d) o tempo gasto remotamente antes do fallback nao era contabilizado em lugar nenhum.
3. Nao existe nenhum registro por execucao do tempo de fila no lock separado do tempo de execucao (so a linha `[SUITE_LOCK] waiting`).

## 2. O que muda por criterio

### Criterio 1: espera no `suite_lock` nao conta contra o timeout do passo

Ja satisfeito pelo codigo atual (nao reimplementado). O patch acrescenta:

- `runner._timed_suite_lock`: cronometra a aquisicao do lock (preferindo `SuiteLock.waited_sec`) e a soma em `RunTimings.lock_wait_sec`; `SuiteLockTimeout` (falha fechada apos `DARKFAC_SUITE_LOCK_TIMEOUT_SEC`, default 3600 s) tambem registra o tempo esperado.
- `suite_lock.py`: `SuiteLock.waited_sec` e, ao adquirir depois de esperar, a linha `[SUITE_LOCK] acquired slot N after waiting Xs (queue time is not counted against any step timeout)`. Essa linha tambem aparece no log do worker, entao o cliente enxerga a fila do Desktop.
- Teste de regressao `test_waiting_for_the_suite_lock_is_not_charged_to_the_step_timeout` (fila de 3 s > timeout do passo de 2 s: passa) e `test_step_timeout_still_bounds_the_execution_itself` (o timeout continua valendo para o que roda de verdade).

Limite conhecido, fora deste patch: o watchdog do JOB no worker (`remote_worker.py::_run_job`, deadline = soma dos timeouts + 120 s contado desde o inicio do `runner.py --local`) ainda inclui a fila do lock do Desktop. Ver secao 6 (trecho pronto para quem tem o patch do `remote_worker.py` em maos).

### Criterio 2: worker silencioso por mais de 180 s

Comportamento resultante (teto de silencio continua 180 s, `DARKFAC_WORKER_SILENCE_FALLBACK_SEC`; piso 60 s):

- Silencio de ate 60 s: nada muda, segue o poll normal.
- Silencio > 60 s com `/health` respondendo: worker lento, espera ate 180 s (como antes) e entao `RemoteJobLost worker_silent`.
- Silencio > 60 s e a primeira sonda `/health` falha: NOVO, o cliente refaz ate `DARKFAC_WORKER_DEAD_PROBES` sondas (default 3, pausa de 0,5 s). Se qualquer uma responder, o worker so teve um solavanco e o job e RETOMADO no mesmo offset do log (sem reenviar a suite, sem duplicar linhas): `[REMOTE <url>] /health failed N probe(s) but answered again; resuming job <id> from offset <off>`. So se todas falharem declara `worker_unreachable`. `DARKFAC_WORKER_DEAD_PROBES=1` restaura o veredito imediato anterior.
- Queda limpa (`worker_silent` ou `worker_unreachable`): o `[REMOTE_JOB_LOST]` e o JSON de evidencia (`<estado>/remote_jobs/<job>.json`) agora trazem tambem `remote_elapsed_sec`, `silence_ceiling_sec`, `failed_health_probes`, `last_step_started`, `step_times` e `cancel_requested`; o job e cancelado no worker (`DELETE`, melhor esforco) para nao ficar rodando uma suite que ninguem espera; `maybe_dispatch_remote` imprime `[REMOTE_FALLBACK] worker=<url> job=<id> code=<codigo> remote_spent=<s>s; ...` e guarda a tentativa em `remote_dispatch.DISPATCH_ATTEMPTS` (`dispatch_attempts()`), que o runner copia para o registro de telemetria. Tentativas de worker ocupado (`busy_timeout`) e de submissao que nao saiu (`not_dispatched`) tambem entram.
- "Sem perder o tempo ja gasto": o fallback local comeca com relogio de passo novo (o tempo remoto e a fila do lock nunca descontam do `timeout_sec` do passo local) e o tempo remoto perdido fica registrado em `remote_lost_sec`, separado de `lock_wait_sec` e `exec_sec`. Decisao deliberada: NAO se aproveita passos "aprovados" lidos do log do worker abortado. O veredito do portao exige `HarnessResult` completo (contagens e `candidate_sha`/`config_hash` verificados), e um log parcial nao e evidencia confiavel (`sanitize_child_output` existe justamente para o cliente nao confiar em marcadores vindos do log remoto).
- Teste de regressao com os limites REAIS (relogio virtual, milissegundos): `test_default_ceiling_is_180s_and_a_responsive_but_silent_worker_is_lost_just_after_it` (silencio 180-200 s, diagnostico completo, evidencia em disco, cancelamento), `test_dead_worker_needs_three_failed_probes_and_is_abandoned_before_the_ceiling`, `test_a_single_lost_health_probe_is_a_blip_not_a_dead_worker`, `test_worker_that_blips_mid_run_is_resumed_over_http_without_duplicating_the_log` (HTTP real em loopback), mais `[REMOTE_FALLBACK]` e contabilidade pelo `maybe_dispatch_remote`.

### Criterio 3: telemetria por execucao

Cada chamada de `run_with_cache` imprime UMA linha informativa depois do veredito (nao e marcador do supervisor, nao altera `parse_harness_output`) e acrescenta o mesmo JSON a `run_timings.jsonl`, no diretorio de estado global do harness (`%LOCALAPPDATA%\DarkFac\harness\run_timings.jsonl` no Windows; truncado para as 1000 ultimas linhas acima de 512 KB):

```
[RUN_TIMING] {"schema":1,"ts":"...","host":"...","path":"local|local_after_remote|remote|cache_hit","success":true,
  "quick":true,"no_cache":false,"local_only":false,"worker_job":false,"tree_hash":"...",
  "lock_wait_sec":812.4,"lock_timed_out":false,"exec_sec":1439.0,"total_sec":2251.9,
  "steps_timeout_sum_sec":3300.0,"remote_lost_sec":75.0,"remote_attempts":[{...}]}
```

`lock_wait_sec` (fila no lock) fica separado de `exec_sec` (suites rodando) e de `remote_lost_sec` (tempo remoto que terminou em fallback). No caminho remoto, `exec_sec` e o tempo do job remoto; a fila do lock do Desktop aparece no `[RUN_TIMING]` do proprio worker, que chega ao cliente prefixado com `[REMOTE <url>]`. Falha ao gravar a telemetria nunca muda veredito nem codigo de saida (`test_a_broken_timing_log_never_changes_the_verdict`). `HarnessResult` NAO foi alterado (`models.py` fora do patch), entao nenhum consumidor do `[HARNESS_RESULT]` muda.

## 3. Passo exato do owner

Em `C:\dev\DarkFac`, no `main` atualizado (PowerShell, um comando por vez):

```powershell
git -C C:\dev\DarkFac pull --ff-only origin main
git -C C:\dev\DarkFac apply --check C:\dev\DarkFac\docs\proposals\USR-178-gate-resilience.patch
git -C C:\dev\DarkFac apply C:\dev\DarkFac\docs\proposals\USR-178-gate-resilience.patch
Copy-Item C:\dev\DarkFac\docs\proposals\USR-178-test_gate_lock_timing.py.txt C:\dev\DarkFac\tests\test_gate_lock_timing.py
Copy-Item C:\dev\DarkFac\docs\proposals\USR-178-test_worker_silence_resume.py.txt C:\dev\DarkFac\tests\test_worker_silence_resume.py
python -m pytest C:\dev\DarkFac\tests\test_gate_lock_timing.py C:\dev\DarkFac\tests\test_worker_silence_resume.py C:\dev\DarkFac\tests\test_remote_dispatch_resilience.py C:\dev\DarkFac\tests\test_remote_job_recovery.py C:\dev\DarkFac\tests\test_suite_lock.py C:\dev\DarkFac\tests\test_suite_lock_stalled.py C:\dev\DarkFac\tests\test_harness_runner_acceleration.py -q -p no:cacheprovider
git -C C:\dev\DarkFac add core/harness/remote_dispatch.py core/harness/runner.py core/harness/suite_lock.py tests/test_gate_lock_timing.py tests/test_worker_silence_resume.py
git -C C:\dev\DarkFac commit -m "fix(harness): portao registra fila do lock separada da execucao e worker silencioso e retomado ou cai limpo para o local [USR-178]"
git -C C:\dev\DarkFac fetch origin
git -C C:\dev\DarkFac rebase origin/main
git -C C:\dev\DarkFac push origin HEAD:main
```

Notas:

- Commite SOMENTE os 5 caminhos acima (nunca `git add -A`: o checkout compartilhado pode ter alteracoes locais soltas). Se o `apply --check` falhar porque outro patch mexeu em `runner.py`, `remote_dispatch.py` ou `suite_lock.py` depois de `a954f83`, avise a fabrica para regenerar o patch (os testes nao dependem da ordem).
- Direto no `main`, sem PR (um PR seria reprovado pelo `trusted-pr-policy`). Antes do push, `python C:\dev\DarkFac\core\orchestrator\guard.py origin/main` acusa `GUARD VIOLATION` (esperado: o guard compara caminhos, nao autoria); depois do push o diff some e fica `[GUARD PASS]`.
- A parte do cliente (os 3 arquivos) vale assim que o Notebook/Desktop que despacha puxar o `main`. O patch NAO exige reiniciar o worker. A linha `[SUITE_LOCK] acquired ...` e o `[RUN_TIMING]` do lado do worker so passam a existir depois que o checkout do worker no Desktop (`DESKTOP-G45IPEM`) puxar o `main` e o worker reiniciar.
- Depois do merge siga o passo normal de deploy (`python C:\dev\DarkFac\scripts\dokploy_redeploy.py`); nenhuma mudanca de produto vai junto. Para ver a telemetria: `Get-Content "$env:LOCALAPPDATA\DarkFac\harness\run_timings.jsonl" -Tail 5`.

## 4. Variavel de ambiente nova

| Variavel | Default | Efeito |
|---|---|---|
| `DARKFAC_WORKER_DEAD_PROBES` | `3` | sondas `/health` consecutivas (pausa de 0,5 s) que precisam falhar antes de declarar o worker `worker_unreachable`; invalido -> 3; limitado a 1..10; `1` = veredito imediato (comportamento anterior) |

As demais (`DARKFAC_WORKER_SILENCE_FALLBACK_SEC`, `DARKFAC_WORKER_HEALTH_TIMEOUT_SEC`, `DARKFAC_SUITE_LOCK_TIMEOUT_SEC`) seguem como estao.

## 5. Testes e verificacao

Verificado com `git apply --check` + `git apply` numa arvore detached limpa de `origin/main` (`2ef168c`, worktree temporaria removida depois), seguido de pytest focal (um por vez):

- `tests/test_gate_lock_timing.py` + `tests/test_worker_silence_resume.py` (novos): `23 passed` (11,9 s). Sem o patch (controle negativo, `git checkout -- core/harness` na mesma arvore) os 23 falham/erram.
- `tests/test_remote_dispatch_resilience.py` + `tests/test_remote_job_recovery.py` + `tests/test_remote_dispatch.py`: `75 passed, 1 skipped` (o skip e o ensaio live de audio).
- `tests/test_suite_lock.py` + `tests/test_suite_lock_stalled.py` + `tests/test_harness_runner_acceleration.py`: `52 passed`.
- `tests/test_harness_contract.py` + `tests/test_gate_isolation.py` + `tests/test_run_ticket_gate_self_correction.py`: `28 passed`.
- Prova de que o criterio 1 ja vale no codigo atual (sem patch): script de fila de 3 s com timeout de passo de 2 s imprimiu `[HARNESS_PASS]`.

O portao completo (`runner.py --quick`) NAO foi rodado aqui; e do orquestrador.

## 6. Riscos e limites

- **Watchdog do job no worker (`remote_worker.py`, fora deste patch, outro agente/patch).** O deadline do job (`_job_timeout_sec`, soma dos timeouts + 120 s) comeca antes do `runner.py --local` e portanto inclui a fila do `suite_lock` do Desktop. Com este patch o filho passa a imprimir `[SUITE_LOCK] acquired slot N after waiting Xs`; um follow-up pequeno em `JobManager._run_job` estende o deadline por essa espera:

  ```python
  deadline_box = [time.monotonic() + timeout_sec]          # no lugar de `deadline = ...`
  # no _watchdog: `time.monotonic() > deadline_box[0]`
  _LOCK_WAIT_RE = re.compile(r"^\[SUITE_LOCK\] acquired slot \d+ after waiting ([0-9]+(?:\.[0-9]+)?)s")
  # no laco `for line in process.stdout:` apos `self._append_log(job, line)`:
  m = _LOCK_WAIT_RE.match(line)
  if m:
      deadline_box[0] += min(float(m.group(1)), 3600.0)    # teto: um teste nao pode inflar o deadline
  ```

  Enquanto isso nao for aplicado, uma fila longa NO DESKTOP ainda pode matar o job por deadline (o cliente ve o job terminar sem veredito e cai para o local). Registrar como seguimento do USR-183/186.
- **Cancelamento do job abandonado.** Em `worker_silent`/`worker_unreachable` o cliente envia `DELETE` ao worker (ate 10 s extras num host que nao responde). Se o worker voltar depois e o job estiver quase pronto, esse trabalho e descartado; a alternativa (deixar rodando) mantinha o Desktop ocupado e o proximo portao esperava ate 300 s por `busy`.
- **Esperar mais 2-3 sondas.** Um worker realmente morto e declarado ~1 s depois (conexao recusada) ou ate ~20 s depois (sondas que estouram o timeout de 10 s), alem dos ~60 s de silencio. Aceitavel frente ao custo de reexecutar a suite local; `DARKFAC_WORKER_DEAD_PROBES=1` reverte.
- **Nao ha retomada do job apos reinicio do worker nem reenvio automatico.** O job de um worker que reiniciou ja e marcado `worker_restarted`/`job_lost_on_worker` (USR-162) e cai para o local; reenviar a suite ao mesmo worker dobraria o pior caso (ate 2 x 3000 s) e nao foi feito.
- **A suite local ainda pode demorar ~50 min** sob concorrencia no Notebook (2971 s e 3002 s no USR-225/245) e o limite do passo e 3000 s (maximo aceito pelo modelo: `timeout_sec <= 3600`). Este patch tira a FILA e o tempo remoto perdido da conta e os torna visiveis; nao acelera a suite. Se a execucao pura passar de 3000 s, a decisao (subir `timeout_sec` ate 3600 em `harness.config.json`, ou reduzir a suite) e do owner e depende dos numeros de `run_timings.jsonl`.
- **Limite externo do launcher.** `run_ticket.py` aplica `GATE_TIMEOUT_S = 7200` ao processo do portao inteiro (fila no lock de ate 3600 s + remoto + local); em fila longa esse teto pode matar o portao antes do teto do passo. Nao e protegido; fica como ajuste proprio do orquestrador se `lock_wait_sec` mostrar filas grandes.
- `runner.py`: `run_with_cache` virou um wrapper fino sobre `_run_with_cache` (corpo e docstring originais); assinatura publica, marcadores, `execute()` e o contrato do `[HARNESS_RESULT]` ficam como estavam. O `[RUN_TIMING]` sai depois de `[HARNESS_PASS]/[HARNESS_FAIL]`.
