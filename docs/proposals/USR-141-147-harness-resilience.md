# Proposta: resiliencia do portao de testes (USR-141, USR-147, USR-135)

`core/harness/*` e `harness.config.json` sao protegidos por `core/orchestrator/guard.py`: o job `trusted-pr-policy` do CI reprova qualquer PR que os altere, e so o owner aplica por commit direto. Por isso a fabrica NAO altera esses arquivos: este documento e o patch ao lado (`docs/proposals/USR-141-147-harness-resilience.patch`) trazem a mudanca ja testada para o owner aplicar.

O patch toca 4 arquivos protegidos e cria 2 arquivos de teste novos (`tests/` nao e protegido, mas os testes vao junto para o commit do owner para nada ficar sem cobertura):

| Arquivo | Ticket |
|---|---|
| `core/harness/remote_worker.py` | USR-141 (prioridade do filho) |
| `core/harness/remote_dispatch.py` | USR-141 (lento vs morto), USR-147 (timeout de probe configuravel) |
| `core/harness/suite_lock.py` | USR-147 (dono stalled) |
| `core/harness/runner.py` | USR-135/147 (lock antes do `execute()` no caminho `--no-cache`) |
| `tests/test_remote_dispatch_resilience.py` (novo) | USR-141 |
| `tests/test_suite_lock_stalled.py` (novo) | USR-147, USR-135 |

`harness.config.json` NAO esta no patch (ver secao USR-135: decisao do owner).

## 1. Passo exato do owner

Em `C:\dev\DarkFac`, no `main` atualizado (PowerShell):

```powershell
git -C C:\dev\DarkFac pull --ff-only origin main
git -C C:\dev\DarkFac apply --check C:\dev\DarkFac\docs\proposals\USR-141-147-harness-resilience.patch
git -C C:\dev\DarkFac apply C:\dev\DarkFac\docs\proposals\USR-141-147-harness-resilience.patch
python -m pytest C:\dev\DarkFac\tests\test_remote_dispatch_resilience.py C:\dev\DarkFac\tests\test_suite_lock_stalled.py C:\dev\DarkFac\tests\test_suite_lock.py C:\dev\DarkFac\tests\test_remote_dispatch.py C:\dev\DarkFac\tests\test_harness_runner_acceleration.py -q
git -C C:\dev\DarkFac add core/harness/remote_dispatch.py core/harness/remote_worker.py core/harness/runner.py core/harness/suite_lock.py tests/test_remote_dispatch_resilience.py tests/test_suite_lock_stalled.py
git -C C:\dev\DarkFac commit -m "fix(harness): resiliencia do portao - worker lento vs morto, prioridade do filho, dono stalled do suite lock [USR-141][USR-147][USR-135]"
git -C C:\dev\DarkFac push origin main
```

Notas:

- Commite SOMENTE os 6 caminhos acima (nunca `git add -A`: o checkout compartilhado tem alteracoes locais soltas).
- Direto no `main`, sem PR (um PR seria reprovado pelo `trusted-pr-policy`). Antes do push, `python C:\dev\DarkFac\core\orchestrator\guard.py origin/main` acusa `GUARD VIOLATION` (esperado: o guard compara caminhos, nao autoria); depois do push o diff some e fica `[GUARD PASS]`.
- A parte do worker (prioridade do filho) so passa a valer depois que o checkout do worker no Desktop (`DESKTOP-G45IPEM`) puxar o `main` e o processo do worker for reiniciado. A parte do cliente (`remote_dispatch.py`, `runner.py`, `suite_lock.py`) vale assim que o Notebook/Desktop que despacha puxar o `main`.
- Depois do merge siga o passo normal de deploy (`python C:\dev\DarkFac\scripts\dokploy_redeploy.py`); nenhuma mudanca de produto vai junto.
- O patch foi verificado com `git apply --check` + `git apply` numa arvore limpa de `origin/main` (checkout Windows com `autocrlf`), seguido dos testes focais (secao 6).

## 2. Problema e evidencia

### USR-141: o worker do Desktop "para de responder" quando o passo paralelo inicia

Evidencia do ticket (04/10/2026, candidato 39cee2c, Notebook -> Desktop `http://100.78.181.90:8080`): `syntax_and_types` passou e, ao iniciar `unit_and_integration_tests_parallel`, o runner registrou `worker stopped responding for >60s mid-run` nas duas tentativas; logo depois `/health` respondia `ok`/`busy=false`.

Diagnostico confirmado no codigo:

1. `remote_worker.py::JobManager._run_job` iniciava o filho `runner.py --quick --local` com `subprocess.Popen` em prioridade normal. O filho dispara `pytest -n auto`, que satura todos os nucleos, e o servidor HTTP do proprio worker (uvicorn, no mesmo host) compete com ele por CPU e deixa de atender os polls.
2. `remote_dispatch.py::_stream_job` abandonava o worker incondicionalmente apos `WORKER_SILENCE_FALLBACK_SEC = 60.0` sem poll bem-sucedido (cada poll tem `POLL_HTTP_TIMEOUT_SEC = 10`), sem verificar se o worker estava apenas lento ou realmente morto. O abandono jogava a execucao no fallback local, que no Notebook estourou 906 s (USR-135).

### USR-147: dono vivo mas travado segura o suite lock por horas

`suite_lock.py` usa lock de arquivo do SO, que e liberado quando o processo morre (entao um dono MORTO nunca deixa lock orfao: o criterio "pid morto e liberado" ja vale por construcao). O defeito real e um dono VIVO mas parado (~0% CPU) que segura o lock por horas: `[SUITE_LOCK] waiting: held by pid 13092 on DESKTOP-G45IPEM ... since 13:52:15`. Alem disso `HEALTH_PROBE_TIMEOUT_SEC = 2.0` em `remote_dispatch.py` nao era configuravel.

### Achado adicional (USR-135/147): a espera pelo lock era contada dentro do timeout do passo

`runner.py::run_with_cache` so tomava o `suite_lock` no ramo com cache. Com `--no-cache` (ou sem arvore limpa) o `execute()` rodava sem lock e quem tomava o lock era o PROPRIO pytest filho (`tests/conftest.py::_acquire_session_lock`). A espera por outra sessao rodando a suite no mesmo host corria dentro do `timeout_sec` do passo e terminava em falso timeout com `count=0`. Esse e um dos caminhos que produziu o `908,5s ... count=0` do USR-135 (outras sessoes rodavam `runner --local` no Notebook).

## 3. O que muda

### 3.1 `remote_worker.py` (USR-141)

- `apply_child_priority(cmd)`: o filho do job e criado com prioridade abaixo do normal. Windows: `creationflags=BELOW_NORMAL_PRIORITY_CLASS` (0x4000). POSIX: o comando ganha o prefixo `nice -n 5` (nao se usa `preexec_fn`, inseguro em processo com threads como este worker). A prioridade e herdada pelos netos (workers do pytest-xdist); verificado empiricamente no Windows (filho e neto reportam 16384). O servidor HTTP do worker continua em prioridade normal e passa a ganhar a disputa por CPU.
- `child_priority_mode()` le `DARKFAC_WORKER_CHILD_PRIORITY` (`normal` | `below_normal`, case-insensitive, default `below_normal`). Valor invalido gera `logger.warning` e cai no default.

### 3.2 `remote_dispatch.py` (USR-141 e USR-147)

- `_stream_job` agora distingue worker LENTO de worker MORTO. Passados `WORKER_SILENCE_FALLBACK_SEC` (60 s) sem poll valido, sonda `GET /health` (timeout `max(configurado, 10 s)`, no maximo a cada 10 s). Se `/health` responde (mesmo `busy=true`), imprime `[REMOTE <url>] job polls silent for Ns but /health answers (busy=...); worker is slow, not dead - waiting up to Ns` e continua esperando. Se `/health` falha, declara `_WorkerUnavailable` imediatamente. Mesmo com `/health` vivo, desiste quando o silencio passa do teto `DARKFAC_WORKER_SILENCE_FALLBACK_SEC` (default 180 s, nunca menor que os 60 s). O deadline do cliente (`soma dos timeouts + 120 s`) continua valendo por cima de tudo.
- `probe_health(base_url, timeout=None)`: sem argumento usa `health_probe_timeout_sec()`, que le `DARKFAC_WORKER_HEALTH_TIMEOUT_SEC` (default 2.0 mantido).
- Mensagem de abandono deixa de ter o "60s" fixo no texto.

### 3.3 `suite_lock.py` (USR-147)

- Nova classe `_StallWatch`, chamada dentro do loop de espera de `SuiteLock.acquire()` (qualquer excecao do detector e engolida com uma linha de log e nunca quebra a aquisicao do lock).
- Depois de esperar `DARKFAC_SUITE_LOCK_STALL_AFTER_SEC` (default 600 s), amostra o CPU acumulado da ARVORE de processos do dono (pid do sidecar + todos os descendentes, via `psutil`). Se, numa janela de `DARKFAC_SUITE_LOCK_STALL_WINDOW_SEC` (default 10 s), o CPU somado avancou no maximo 0,25 s E nenhum processo entrou ou saiu da arvore, imprime uma vez por pid:
  `[SUITE_LOCK] holder appears stalled: pid <pid> on <host> held the lock for <idade> and used <x>s of CPU in <janela>s (<n> process(es) in its tree); not terminating it; set DARKFAC_SUITE_LOCK_REAP_STALLED=1 to reap stalled holders automatically`.
- Somente com `DARKFAC_SUITE_LOCK_REAP_STALLED=1` (default desligado) encerra o dono (filhos primeiro, `terminate` e depois `kill` apos 3 s) e imprime `terminated stalled holder pid <pid>`; o SO libera o lock e o waiter assume o slot na iteracao seguinte.
- Salvaguardas: so inspeciona sidecar do MESMO host; nunca inspeciona nem encerra o proprio pid nem seus ancestrais; protege contra reuso de pid (processo criado depois do sidecar nao e o dono); sem `psutil` ou com acesso negado o detector fica mudo (`psutil>=6.0,<8` ja esta em `requirements.txt`).

### 3.4 `runner.py` (USR-135 e USR-147)

- No ramo `cache_key is None` de `run_with_cache`, o `execute()` agora roda dentro de `harness_suite_lock.suite_lock()`. A espera pelo lock acontece fora do subprocesso do passo, nao consome o `timeout_sec` do passo, e o `DARKFAC_SUITE_LOCK_HELD=1` exportado pelo contexto impede o pytest filho de tentar pegar o lock de novo. E a mesma mudanca que o patch antigo do USR-143 (`.factory/patches/usr-143-harness-core.patch`) fazia em `runner.py`.

## 4. Variaveis de ambiente

| Variavel | Onde | Default | Efeito |
|---|---|---|---|
| `DARKFAC_WORKER_CHILD_PRIORITY` | worker | `below_normal` | `normal` desliga a reducao de prioridade; invalido -> default |
| `DARKFAC_WORKER_SILENCE_FALLBACK_SEC` | runner (cliente) | `180` | teto de espera de worker silencioso mas com `/health` vivo; piso efetivo 60; invalido/<=0 -> 180 |
| `DARKFAC_WORKER_HEALTH_TIMEOUT_SEC` | runner (cliente) | `2.0` | timeout do probe `/health` inicial; invalido/<=0 -> 2.0 |
| `DARKFAC_SUITE_LOCK_STALL_AFTER_SEC` | qualquer waiter | `600` | espera minima antes de checar dono stalled; invalido/negativo -> 600 |
| `DARKFAC_SUITE_LOCK_STALL_WINDOW_SEC` | qualquer waiter | `10` | janela de amostragem de CPU; invalido/negativo -> 10 |
| `DARKFAC_SUITE_LOCK_REAP_STALLED` | qualquer waiter | desligado | `1` encerra o dono stalled; qualquer outro valor = so aviso |

## 5. Riscos e limites

- Worker lento por ate 180 s a mais antes do fallback local: o custo e esperar mais um pouco quando `/health` responde mas o job nao avanca (e o caso que antes abandonava um worker saudavel). Reduza com `DARKFAC_WORKER_SILENCE_FALLBACK_SEC=60` para voltar ao comportamento antigo. Um worker com `/health` vivo mas polls de job quebrados de forma permanente e abandonado no teto.
- Prioridade reduzida: o filho pode terminar um pouco mais devagar se o Desktop tiver outra carga em prioridade normal; e o efeito desejado (o servidor HTTP e o uso interativo vencem). `DARKFAC_WORKER_CHILD_PRIORITY=normal` reverte. Em POSIX o `argv` do filho passa a comecar por `nice`; o `process.kill()` do watchdog continua funcionando porque `nice` faz `exec`.
- Falso positivo do detector de stalled: um dono legitimamente quieto por mais que a janela de 10 s E sem nenhum processo ativo na arvore durante a janela, depois de 600 s de espera do waiter. O runner dono sempre tem o pytest como filho consumindo CPU, entao uma suite viva nao e marcada. Por isso o encerramento automatico fica desligado por padrao; recomenda-se rodar primeiro so com o aviso. O patch antigo do USR-143 olhava apenas o CPU do processo raiz (que fica ocioso enquanto o pytest filho trabalha) e encerrava por padrao, o que poderia matar uma suite saudavel; este patch o substitui nesse ponto.
- `runner.py`: no ramo `--no-cache` o lock agora e mantido durante todo o `execute()`; com `DARKFAC_SUITE_LOCK=off` ou `DARKFAC_SUITE_LOCK_HELD=1` nada muda.
- O criterio "lock orfao com pid morto e liberado com registro": o SO ja libera o lock quando o processo morre; so nao existe linha de log disso (nao ha evento a registrar do lado do waiter, ele simplesmente adquire). Se o owner quiser a linha de log, e um follow-up trivial.

### Relacao com `.factory/patches/usr-143-harness-core.patch`

Esse patch (do USR-143) mexe nos mesmos trechos de `suite_lock.py`, `remote_dispatch.py` e `runner.py` e NAO deve ser aplicado junto com este: os dois conflitam. Este patch o substitui em tres pontos: (a) o detector considera a arvore de processos e fica desligado para encerrar por padrao; (b) o nome do env do probe e `DARKFAC_WORKER_HEALTH_TIMEOUT_SEC` (no de USR-143 era `DARKFAC_REMOTE_PROBE_TIMEOUT_SEC`) e o env do stall e `DARKFAC_SUITE_LOCK_STALL_AFTER_SEC`; (c) acrescenta lento vs morto e a prioridade do filho. Nao foram portados do USR-143: o motivo textual da falha do probe (`last_probe_failure_reason`) e a remocao do sidecar de pid morto (o sidecar e apagado no `release()` e o lock em si e liberado pelo SO).

## 6. Testes

Novos (rodam em Windows e Linux, sem rede externa, `tmp_path` para todo estado):

- `tests/test_remote_dispatch_resilience.py` (29 testes): worker falso HTTP em thread (`127.0.0.1`, porta livre). Worker lento mas vivo nao e abandonado; worker vivo cujos polls nunca voltam e abandonado so no teto; worker morto (porta fechada) e abandonado bem antes do teto; `/health` com erro conta como morto; deadline do cliente prevalece; envs invalidas (`""`, `abc`, `-1`, `0`, `nan`, `inf`) caem no default; teto nunca abaixo de 60 s; `probe_health` honra o timeout do env; prioridade do filho aplicada, verificada interceptando `Popen` (`creationflags` no Windows, prefixo `nice -n 5` no POSIX), desligavel por env e com valor invalido caindo em `below_normal`. Observacao: `tests/conftest.py` troca `urllib.request.urlopen` por um stub offline; o arquivo religa um opener real so para loopback.
- `tests/test_suite_lock_stalled.py` (18 testes): dono ocioso real (subprocesso vivo com o lock e ~0% CPU) gera `[SUITE_LOCK] holder appears stalled` com pid/host/idade e NAO e encerrado por padrao; so amostra depois da espera configurada; com `DARKFAC_SUITE_LOCK_REAP_STALLED=1` o dono e encerrado e o waiter assume o slot; testes deterministicos do detector (CPU avancando na arvore, processos entrando na arvore, host diferente, proprio pid, pid reciclado, falha do detector nao quebra o lock); `run_with_cache(no_cache=True)` toma o lock ANTES do `execute()`.

Resultado em arvore limpa de `origin/main` + `git apply` do patch (Windows 11, Python 3.12):

```
tests/test_remote_dispatch_resilience.py tests/test_suite_lock_stalled.py tests/test_suite_lock.py tests/test_remote_dispatch.py
tests/test_remote_worker_multi_harness.py tests/test_remote_worker_restart.py tests/test_remote_codex_harness.py
tests/test_remote_provider_failover.py tests/test_harness_runner_acceleration.py tests/test_runner_candidate_binding.py
tests/test_hf15_runner.py tests/test_harness_contract.py
150 passed, 1 skipped in 316.88s   (o skip e o ensaio live de ponta a ponta)
tests/test_ci_policy.py tests/test_governance_guard.py  ->  9 passed
tests/test_remote_dispatch.py -k end_to_end --run-live-audio --allow-network  ->  1 passed  (worker uvicorn real com o filho em BELOW_NORMAL)
```

Nao foi rodado o portao completo (`runner.py`) nem a suite inteira, por instrucao.

## 7. USR-135: limite de tempo do portao (decisao do owner)

O patch NAO altera `harness.config.json`. O que a evidencia mostra e o que falta:

| Medida | Valor | Fonte |
|---|---|---|
| Notebook, passo paralelo, 04/10, limite 900 s | interrompido em 908,5 s, `count=0` | ticket USR-135 |
| Notebook, mesma suite, limite diagnostico 2400 s | terminou em 916,1 s; 2971 testes (2959 passaram, 6 falharam, 6 pulados) | ticket USR-135 |
| CI Windows (`main-validation (windows-latest)`, run 37680522709, push em `main`, 07/10), passo "Run deterministic harness" (3 passos juntos) | 731 s (12 min 11 s) | `gh run view` |
| CI Ubuntu (mesmo run) | 140 s (2 min 20 s) | `gh run view` |
| Desktop (worker), veredito de 2869 testes passando | duracao nao gravada em `verdicts.json` | `%LOCALAPPDATA%\DarkFac\harness\verdicts.json` |
| Logs antigos em `.factory/test_logs` (1458 testes, setembro) | 415 s a 502 s | logs `pytest` |

Leitura: a suite dobrou de tamanho desde setembro (1458 -> ~2970 testes) e a medida do Notebook (916 s, 2% acima do limite) mostra que 900 s nao tem folga nenhuma nessa maquina, ainda mais com outras sessoes disputando CPU. Mas a medicao pedida no ticket ("medir novamente com as falhas corrigidas") ainda nao foi feita, e parte dos falsos timeouts vinha da espera pelo lock dentro do passo (corrigida pelo item 3.4 deste patch). Por isso nao ha evidencia suficiente para a fabrica fixar um numero.

Recomendacao ao owner, em ordem:

1. Aplique este patch primeiro (remove a espera do lock do orcamento do passo).
2. Meca de novo no Notebook, com a arvore limpa e sem outra sessao rodando: `python C:\dev\DarkFac\core\harness\runner.py --quick --local --no-cache` com o limite diagnostico atual de 900 s ou, para ter o numero mesmo se estourar, temporariamente 2400 s, e anote o `[CHILD_STEP_TIME] unit_and_integration_tests_parallel <s>s`.
3. Fixe `timeout_sec` do passo `unit_and_integration_tests_parallel` em cerca de 1,5x o pior valor medido. Com o dado atual (916 s) isso da **1500**. Mudanca de uma linha em `C:\dev\DarkFac\harness.config.json`: `"timeout_sec": 900` -> `"timeout_sec": 1500` (so no passo paralelo; o serial continua 300). O orcamento remoto acompanha sozinho: o cliente usa `soma dos timeouts + 120 s` e o worker `soma + 120 s` (`_job_timeout_sec`).
4. Alternativa mais conservadora: manter 900 e depender do worker do Desktop como caminho primario, aceitando que o fallback local no Notebook pode estourar. Nao recomendado: o fallback local do ticket USR-141 e justamente o caminho que falhou.

Nenhum teste existente fixa o valor 900 (verificado em `tests/test_harness_contract.py`, `tests/test_ci_policy.py`, `tests/test_governance_guard.py`, `tests/test_hf15_runner.py`).

Criterios de aceite do USR-135 e como fecha-los: (a) medicao nova no Notebook (passo 2); (b) o owner aplica o `timeout_sec` escolhido em commit direto; (c) `trusted-pr-policy` fica verde porque nenhum PR toca o arquivo protegido; o portao oficial passa com contagem positiva no Notebook depois de (a) e (b).
