# Proposta: worker remoto encerra a arvore de processos e o restart espera a porta (USR-183 + USR-186 item a)

`core/harness/*` e protegido por `core/orchestrator/guard.py`: o job `trusted-pr-policy` do CI reprova qualquer PR que o altere, e so o owner aplica por commit direto. Por isso a fabrica NAO altera `core/harness/remote_worker.py`: este documento e o patch ao lado (`docs/proposals/USR-183-186-worker-process-tree.patch`) trazem a mudanca ja testada. O teste novo vai como `docs/proposals/USR-183-186-test_remote_worker_process_tree.py.txt` (se estivesse em `tests/` reprovaria o portao da branch, porque so passa depois do patch); o owner o copia para `tests/` no mesmo commit.

A unica mudanca que NAO depende do patch ja esta na branch da fabrica: `tests/test_remote_dispatch.py` (teste live do uvicorn) passou a usar `log_config=None`.

## ORDEM OBRIGATORIA: USR-196 primeiro, depois este

O patch do USR-196 (`docs/proposals/USR-196-worker-update.patch`) tambem edita `core/harness/remote_worker.py` (inclui uma funcao nova logo antes de `create_worker_app` e uma chamada em `update_system`). Este patch foi gerado SOBRE `origin/main` COM o USR-196 JA APLICADO (o `index` do cabecalho, `2500741..141d98e`, parte do arquivo resultante do USR-196). Portanto:

1. Se o USR-196 ainda nao esta no `main`, aplique o USR-196 primeiro, depois este (os comandos da secao 4 fazem isso).
2. Se o USR-196 ja foi aplicado e commitado, pule os passos dele; o `git apply --check` deste patch continua passando.
3. Aplicar este patch SEM o USR-196 falha no `--check` (contexto diferente): nao force.

## 1. Problema e evidencia

**USR-183 (arvore de processos).** O job do worker roda `runner.py --quick --local` com `stdout=PIPE` e le o pipe linha a linha na propria thread do job. Dois defeitos:

- O watchdog (`_run_job`, estouro de tempo ou cancelamento) chamava apenas `process.kill()`: mata o processo de topo e deixa os workers do pytest-xdist (netos do worker) vivos, ainda segurando o pipe de stdout e o `suite_lock`.
- Se o runner morre (ou sai) mas um neto mantem o pipe aberto, `for line in process.stdout` nunca termina: o job fica `running` para sempre e o loop de leitura prende a thread unica de jobs do worker. `POST /system/restart` so encerrava a arvore se `process.poll() is None`, ou seja, nao alcancava os netos de um runner que ja tinha morrido.
- Teste live: `uvicorn.Config` com `log_config` padrao troca os handlers de logging do pytest e, no encerramento, escreve em stream fechado ("I/O operation on closed file").

Reproduzido de forma hermetica: com o codigo atual, os dois testes de `_run_job` do arquivo novo (runner que sai deixando um neto com o pipe aberto; runner pendurado que estoura o tempo) falham porque `_run_job` fica bloqueado ate o neto terminar (120 s no teste; o teste desiste e falha apos 40 s).

**USR-186 item (a) (porta).** `_wait_for_released_port` tentava 40 vezes a cada 0,25 s (10 s) e `_restart_after_exit` desistia ("remained occupied; refusing duplicate launch"), deixando o worker DESLIGADO (`[Errno 10048]` na proxima subida). A recuperacao pela tarefa agendada e os 5 testes ja foram entregues no PR #237; falta esperar a porta de verdade.

## 2. O que o patch faz (1 arquivo protegido: `core/harness/remote_worker.py`)

**Criterio 1 do USR-183 (arvore).**

- `_ProcessTree` (nova): guarda o runner e todos os descendentes vistos enquanto ele vivia (`psutil`, que ja e dependencia: `requirements.txt`). Orfaos reparentados deixam de ser alcancaveis a partir do runner; os snapshots os mantem. A identidade do `psutil.Process` inclui a hora de criacao, entao um pid reciclado nunca e morto por engano (teste cobre).
- Watchdog: faz snapshot da arvore a cada 5 s (`JOB_TREE_SNAPSHOT_SEC`) e, em timeout ou cancelamento, mata a ARVORE inteira (`kill`, sem graca) e ainda `process.kill()` como reserva.
- Leitura do pipe numa thread propria; a thread do job espera o runner (`process.wait()`) e depois da ate 10 s (`JOB_PIPE_DRAIN_GRACE_SEC`) para o pipe fechar. Se um neto ainda o segura, mata os remanescentes da arvore, registra `[WORKER] output pipe still open ...` no log do job e espera mais 10 s; se mesmo assim nao fechar, para de ler (thread daemon) e o job segue para o veredito. Nada trava a fila de jobs.
- `mark_interrupted` (`/system/restart`): passa a encerrar a arvore (terminate, depois kill apos 3 s) mesmo quando o runner de topo ja morreu.
- `_terminate_process_tree` ganhou `force` (mesmo comportamento por padrao).

**Criterio 3 do USR-183 (teste live).** `log_config=None` em `tests/test_remote_dispatch.py` (ja na branch da fabrica; padrao igual ao de `tests/test_remote_job_recovery.py`).

**USR-186 (a).**

- `_wait_for_released_port(host, port)` (assinatura compativel; novos kwargs opcionais so para teste) espera ate `DARKFAC_WORKER_PORT_WAIT_SEC` segundos (padrao 60) com backoff exponencial 0,25 s -> 2 s. Valor invalido ou <= 0 volta ao padrao.
- Se passar o limite: (i) se algum worker responde `GET /health` em `127.0.0.1:<porta>`, ele ja esta servindo, nada a relancar (retorna False, o comportamento atual de nao lancar duplicata); (ii) senao, o processo remanescente que escuta a porta e que roda `remote_worker.py` (predecessor pendurado) e encerrado com sua arvore e a porta e aguardada por mais 10 s; (iii) se nada disso resolver, retorna False como hoje (a tarefa agendada do PR #237 continua sendo a rede de seguranca).
- A sonda de bind usa `SO_REUSEADDR` so em POSIX (como o uvicorn; um TIME_WAIT nao conta como ocupada); no Windows nao se usa, para a sonda nunca "passar por cima" de um listener vivo.
- Heranca do socket de escuta: `_detached_popen_kwargs()` (nova) centraliza os argumentos dos filhos destacados (`trigger_daemon_restart` e `_restart_after_exit`) e agora declara `close_fds=True` de forma explicita. O padrao do Python 3.12 ja nao herdaria (sockets nao sao herdaveis; no Windows so os handles padrao vao na lista), mas o contrato fica explicito e testado: mesmo com o socket marcado como herdavel o filho nao o segura, e a porta fica livre quando o daemon antigo sai. Nao foi preciso reusar o socket.

## 3. Testes (`tests/test_remote_worker_process_tree.py`, novo, 13 testes)

Processos reais (runner com neto que herda o pipe), sockets reais e relogio virtual:

- runner que sai deixando neto com o pipe aberto: `_run_job` termina, o neto e morto, o log registra os remanescentes;
- runner pendurado que estoura o tempo do watchdog: runner e neto morrem;
- `_ProcessTree` alcanca o neto depois que o runner de topo morreu; nao toca pid reciclado;
- `/system/restart` (`mark_interrupted`) mata remanescentes de um runner que ja saiu;
- espera da porta: padrao de 60 s e leitura do ambiente; espera real de 30 s virtuais com backoff limitado (>10 s do codigo antigo); porta realmente ocupada por um socket em escuta (so desiste no limite e tem sucesso quando liberada); worker saudavel nunca e encerrado; predecessor pendurado e encerrado e a subida acontece; `_restart_after_exit` lanca exatamente uma vez depois de uma liberacao lenta (25 s virtuais);
- filho destacado nao herda o socket de escuta (mesmo herdavel) e todos os lancamentos usam `_detached_popen_kwargs`.

Resultado na arvore detached limpa de `origin/main` + USR-196 + este patch (`git apply --check` + `git apply` em sequencia; o arquivo final e identico, byte a byte, ao gerado): `tests/test_remote_worker_process_tree.py` + `tests/test_worker_update_removed_files.py` + `tests/test_remote_worker_restart.py` + `tests/test_remote_job_recovery.py` + `tests/test_worker_onprem_test_subagent.py`: `57 passed`. `tests/test_remote_dispatch.py`: `29 passed, 1 skipped` (o live passa com `DARKFAC_ALLOW_NETWORK=1 ... --run-live-audio`). Contra o codigo antigo, os 2 testes de `_run_job` falham (job bloqueado no pipe) e os de porta nem existem (funcoes novas).

## 4. Passo exato do owner

Em `C:\dev\DarkFac`, no `main` atualizado (PowerShell, um comando por vez):

```powershell
git -C C:\dev\DarkFac pull --ff-only origin main
git -C C:\dev\DarkFac apply --check C:\dev\DarkFac\docs\proposals\USR-196-worker-update.patch
git -C C:\dev\DarkFac apply C:\dev\DarkFac\docs\proposals\USR-196-worker-update.patch
git -C C:\dev\DarkFac apply --check C:\dev\DarkFac\docs\proposals\USR-183-186-worker-process-tree.patch
git -C C:\dev\DarkFac apply C:\dev\DarkFac\docs\proposals\USR-183-186-worker-process-tree.patch
Copy-Item C:\dev\DarkFac\docs\proposals\USR-196-test_worker_update_removed_files.py.txt C:\dev\DarkFac\tests\test_worker_update_removed_files.py
Copy-Item C:\dev\DarkFac\docs\proposals\USR-183-186-test_remote_worker_process_tree.py.txt C:\dev\DarkFac\tests\test_remote_worker_process_tree.py
python -m pytest C:\dev\DarkFac\tests\test_remote_worker_process_tree.py C:\dev\DarkFac\tests\test_worker_update_removed_files.py C:\dev\DarkFac\tests\test_remote_worker_restart.py C:\dev\DarkFac\tests\test_remote_job_recovery.py C:\dev\DarkFac\tests\test_worker_onprem_test_subagent.py -q
git -C C:\dev\DarkFac add core/harness/remote_worker.py tests/test_worker_update_removed_files.py tests/test_remote_worker_process_tree.py
git -C C:\dev\DarkFac commit -m "fix(harness): worker encerra a arvore de processos do runner e o restart espera a porta [USR-183, USR-186]"
git -C C:\dev\DarkFac fetch origin
git -C C:\dev\DarkFac rebase origin/main
git -C C:\dev\DarkFac push origin HEAD:main
```

Se o USR-196 ja estiver no `main`, omita os tres primeiros comandos de `apply` e o `Copy-Item` do teste dele (e o respectivo caminho no `git add`).

Notas:

- Commite SOMENTE os caminhos do `git add` acima (nunca `git add -A`: o checkout compartilhado pode ter alteracoes locais soltas).
- Direto no `main`, sem PR (um PR seria reprovado pelo `trusted-pr-policy`). Antes do push, `python C:\dev\DarkFac\core\orchestrator\guard.py origin/main` acusa `GUARD VIOLATION` (esperado: o guard compara caminhos, nao autoria); depois do push o diff some e fica `[GUARD PASS]`.
- O patch so passa a valer no Desktop depois que o checkout do worker puxar o `main` (`/system/update`) e o processo do worker for reiniciado (`/system/restart`); o restart ja usa o codigo novo na ida seguinte. Depois do merge siga o passo normal de deploy (`python C:\dev\DarkFac\scripts\dokploy_redeploy.py`).
- Apos o push marque o `OA` correspondente como `done` (com `resolved_at`) em `.factory/owner_actions/owner_actions.json` e feche os tickets USR-183 e USR-186 (item a).

## 5. Riscos e limites

- O watchdog agora mata a ARVORE do runner (so processos descendentes do runner, nunca o proprio worker; o pid do worker e excluido explicitamente).
- Remanescentes que o runner criou e que escaparam antes do primeiro snapshot (intervalo de 5 s; o primeiro ocorre no primeiro tick de 1 s) podem nao ser conhecidos; o `kill` sempre refaz o snapshot enquanto o runner ainda vive, entao o risco so existe para processos criados e orfaos em menos de 1 s.
- O `net_connections` do psutil pode exigir privilegio em alguns sistemas; se falhar, a etapa (ii) e ignorada e o comportamento e o atual (False, sem lancar duplicata).
- Quem escuta a porta e roda `remote_worker.py` sem responder `/health` por 60 s e tratado como predecessor pendurado e encerrado; um worker saudavel (responde `/health`) nunca e tocado.
- A espera maxima do helper `--restart-wait` sobe de 10 s para 60 s (+10 s apos encerrar um predecessor); configuravel por `DARKFAC_WORKER_PORT_WAIT_SEC`. O helper e um processo destacado, entao nao bloqueia nada.
- Se a thread de leitura nao fechar nem apos matar os remanescentes, ela fica abandonada (daemon) e o job segue; no pior caso um handle de log fica aberto ate o fim do processo.
