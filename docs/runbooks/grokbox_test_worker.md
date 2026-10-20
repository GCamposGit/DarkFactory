# Runbook: grokbox como worker oficial de testes do portao

O grokbox e uma VM Linux (host `grok-bot-vm-418374532`, Tailscale node `grokbox`, `100.77.130.5`,
Python 3.13, project_root `/workspace/darkfac/DarkFactory`) que executa o worker de testes
(`core/harness/remote_worker.py`, porta 8080). Em 14/10/2026 rodou o portao (`runner.py --quick`) com
4157 testes passando em 143 s; o Notebook leva 23-30 min. Por isso ele e o PRIMEIRO da lista de
workers e o Desktop (`http://100.78.181.90:8080`) e o segundo.

## 1. Variaveis

| Variavel | Valor recomendado | Efeito |
| --- | --- | --- |
| `DARKFAC_TEST_WORKERS` | `http://100.77.130.5:8080,http://100.78.181.90:8080` | lista ordenada; o primeiro alcancavel e livre vence |
| `DARKFAC_WORKER_HEALTH_TIMEOUT_SEC` | `10` | timeout da sonda `/health` por worker |
| `DARKFAC_REMOTE_HARNESS` | `auto` (padrao) | `required` falha em vez de cair para local |
| `DARKFAC_WORKER_TOKEN` | (se o worker exigir) | bearer token |

No Dokploy da VPS (compose `darkfac-cloud`), `DARKFAC_TEST_WORKERS` ja esta definido com grokbox
primeiro e Desktop depois. `DARKFAC_WORKER_HEALTH_TIMEOUT_SEC` NAO esta definido la e vale o padrao do
compose (`10`, a partir de `deploy/dokploy/docker-compose.cloud.yml`).

Nas maquinas de dev (Notebook, Desktop) o padrao do codigo ainda e so o Desktop ate o owner aplicar o
patch `docs/proposals/GROKBOX-default-workers.patch`; ate la, exporte as variaveis na sessao:

```powershell
$env:DARKFAC_TEST_WORKERS = "http://100.77.130.5:8080,http://100.78.181.90:8080"
$env:DARKFAC_WORKER_HEALTH_TIMEOUT_SEC = "10"
```

## 2. Por que a sonda de 10 s

A rota Notebook/VPS -> grokbox passa por um relay DERP do Tailscale (sem UDP direto; a porta 41641
nao pode ser liberada). O `GET /health` leva mais que os 2 s do padrao
(`HEALTH_PROBE_TIMEOUT_SEC` em `core/harness/remote_dispatch.py`) de vez em quando, o worker era dado
como inalcancavel e o portao caia para o Notebook. Com 10 s funcionou nas execucoes de 14/10.

## 3. Checar a saude

```powershell
curl.exe -s -m 15 http://100.77.130.5:8080/health
tailscale status
tailscale ping grokbox
```

Esperado: JSON com `status` ok, `busy` false, `platform_family` linux. `tailscale ping` mostrando
`via DERP(...)` e normal (e o motivo do timeout de 10 s).

## 4. Quem mantem o worker de pe (scripts/grokbox_supervisor.py)

> **Atencao (20/10/2026):** NAO substitua o `grokbox-supervisor.sh` que roda hoje na VM por este script.
> O supervisor da VM tambem recupera o Tailscale apos cada congelamento (`tailscale up` + restart do tailscaled),
> mantem o watchdog e confere venv/requirements, e o bootstrap o relanca se ele sumir (dois supervisores
> brigariam pelo worker). O caminho e levar a regra de recarga deste script (`decide()`: comparar o HEAD com
> o sha em que o PROPRIO supervisor lancou o worker; o `git_sha` do `/health` e calculado do HEAD na hora e
> nao prova que o processo recarregou) para dentro do `grokbox-supervisor.sh`, e versionar esse script aqui.

A VM NAO tem `systemctl` nem `journalctl` (verificado em 18/10/2026, OA-035) e nao vamos instalar systemd.
O supervisor esta versionado em `scripts/grokbox_supervisor.py` (Python 3 stdlib, USR-315). Ele lanca
`core/harness/remote_worker.py --host 0.0.0.0 --port 8080 --node-id grokbox --project-root <root>`,
relanca o worker se morrer, e a cada ciclo (padrao 10 s) compara `git rev-parse HEAD` de `project_root`
com o `git_sha` do `/health`: se mudou e `busy=false`, envia SIGTERM ao worker e o relanca (em ate ~60 s);
com `busy=true` (ou `/health` inalcancavel) espera, nunca reinicia com job rodando.

Subir (na VM, uma vez; o `git pull --ff-only` seguinte dispara o reload sozinho):

```bash
cd /workspace/darkfac/DarkFactory && mkdir -p /workspace/darkfac/run
nohup python3 scripts/grokbox_supervisor.py >> /workspace/darkfac/run/supervisor.log 2>&1 &
```

Configuracao (argumentos ou variaveis): `--project-root`/`GROKBOX_PROJECT_ROOT`
(`/workspace/darkfac/DarkFactory`), `--python`/`GROKBOX_PYTHON` (`<root>/.venv/bin/python`),
`--port`/`GROKBOX_PORT` (8080), `--node-id`/`GROKBOX_NODE_ID` (`grokbox`), `--host`/`GROKBOX_HOST`
(`0.0.0.0`), `--interval`/`GROKBOX_POLL_SEC` (10), `--stop-file`/`GROKBOX_STOP_FILE`
(`/workspace/darkfac/run/supervisor.stop`), `--pid-file`/`GROKBOX_PID_FILE`
(`/workspace/darkfac/run/supervisor.pid`).

Verificar:

```bash
cat /workspace/darkfac/run/supervisor.pid; ps -eo pid,ppid,etime,cmd | grep -v grep | grep -E "grokbox_supervisor|remote_worker.py"
tail -n 20 /workspace/darkfac/run/supervisor.log
curl -s http://127.0.0.1:8080/health          # git_sha == git rev-parse HEAD, busy=false
```

Parar o supervisor: `touch /workspace/darkfac/run/supervisor.stop` (ele sai no proximo ciclo e deixa o
worker de pe; remova o arquivo antes de subir de novo) ou `kill $(cat /workspace/darkfac/run/supervisor.pid)`.
Para parar tambem o worker, so com `busy=false`: `kill -TERM <PID do remote_worker.py>`.

Atualizar a VM:

```bash
curl -s http://127.0.0.1:8080/health          # so siga com "busy": false (ou aceite que o reload espera o job)
cd /workspace/darkfac/DarkFactory && git status --short && git fetch origin && git checkout main && git pull --ff-only
# o supervisor recarrega o worker ocioso em ate ~60 s; confira o git_sha novo no /health
```

Se o supervisor tambem estiver parado, suba-o pelo comando acima. Logs do kernel: `dmesg` (nao ha journal).
Congelamentos ficam em `/workspace/darkfac/run/vm-events.log`.

O Vigia do Tailscale no grokbox roda a cada 5 min e cuida SOMENTE do Tailscale; ele nao reinicia o worker.
O supervisor nao sobrevive a reboot da VM: depois de um reboot, suba-o de novo com o `nohup` acima.

## 5. Quando o Tailscale perde o servidor de controle

Sintomas: `tailscale status` mostra o grokbox como `offline`/`idle`, ou `health` expira mesmo com 10 s,
e o runner imprime `[REMOTE] http://100.77.130.5:8080 unreachable ...` e segue para o Desktop e depois
para o local. O portao nunca trava por isso, apenas fica lento. Para recuperar:

1. No Notebook, `tailscale status` e `tailscale netcheck`; se a conta aparece desconectada, reabra o
   cliente Tailscale e entre de novo.
2. No grokbox (console da VM), `sudo tailscale status`; se disser "Logged out" ou "can't reach
   control", rode `sudo tailscale up` e confira se o no volta a `100.77.130.5` (o Vigia tenta isso a
   cada 5 min).
3. Confirme que o worker segue de pe (`ps -eo pid,etime,cmd | grep remote_worker.py`) e repita o `curl.exe`
   do item 3.
4. Se a VM nao responder, registre/atualize uma OA em `.factory/owner_actions/owner_actions.json`.

## 6. Portao so remoto, para teste

Prova de que o grokbox sozinho fecha o portao, sem cache e sem fallback local:

```powershell
$env:DARKFAC_TEST_WORKERS = "http://100.77.130.5:8080"
$env:DARKFAC_WORKER_HEALTH_TIMEOUT_SEC = "10"
python C:\dev\DarkFac\core\harness\runner.py --quick --no-cache --remote-required
```

Esperado: `[HARNESS_PASS]` com `executed_on` apontando o grokbox em ~2-3 min. Se o grokbox nao
responder, `--remote-required` falha com `RemoteRequiredError` em vez de rodar local.

## 7. Isolamento dos testes

O job do worker exporta `DARKFAC_HARNESS_WORKER_JOB=1` (`remote_dispatch.ENV_WORKER_JOB`); com ele
`maybe_dispatch_remote` retorna `None` em silencio. O teste
`test_lost_job_is_announced_and_its_seconds_are_recorded_for_the_local_fallback` passa no Notebook e no
CI e so falhava dentro do job do worker: a fixture autouse limpava um nome errado
(`DARKFAC_WORKER_JOB`). Testes que simulam o despacho devem remover `DARKFAC_HARNESS_WORKER_JOB`,
`CI`, `DARKFAC_REMOTE_HARNESS` e `DARKFAC_TEST_WORKERS`; ver `tests/test_worker_silence_resume.py`.

## 8. VM congelada/restaurada pela plataforma

Evidencia (15/10/2026): o kernel do grokbox registrou "virtual machine fork"; as 18:12:29 IP e gateway
mudaram e o Tailscale ficou fora ate 18:13. No portao do USR-192 o processo do worker (pid 303996) terminou
no meio do job, outra instancia recuperou o estado e marcou o job `worker_restarted`, e essa recuperacao
apagou o worktree do job: 432 testes falharam por arquivo ausente (falhas falsas, nao veredito). O Notebook
viu `/health` sumir (3 sondas), `[REMOTE_JOB_LOST]` e caiu para o proximo worker / local.

Regras:

- Isso e infraestrutura: `worker_restarted`/job perdido nunca e `[HARNESS_FAIL]` do candidato. Com o patch
  `docs/proposals/WORKER-RESTART-resilience.md` aplicado, o dispatch espera o mesmo worker voltar
  (`DARKFAC_WORKER_RESTART_GRACE_SEC`, 180 s) e reenvia ate `DARKFAC_WORKER_RESTART_RETRIES` (2) vezes
  (linhas `[REMOTE_RETRY]`), e a recuperacao do worker nao apaga o worktree de job ativo.
- Testes destrutivos no worker (reiniciar o servico, `kill`, `tailscale down`, mexer em `worktrees/`,
  `systemctl restart`) so com a fila parada: confirme `curl.exe http://100.77.130.5:8080/health` com
  `busy=false` e nenhum portao em andamento no Notebook/VPS antes de comecar.
- Ao ver `worker_restarted` num log, olhe `dmesg | grep -i fork` e `tailscale status` na VM antes de
  culpar o candidato; se o job voltou em outra tentativa (`[REMOTE_RETRY]`), nada a fazer.

## 9. Congelamento de VM = infraestrutura (USR-298)

Sintoma: o pytest do job morre com exit `-15` (SIGTERM) em 1-2 s, com 0 testes. O suporte da plataforma
(T-H65476) confirma que a VM e congelada/restaurada; na restauracao o Tailscale fica offline e os processos
recebem SIGTERM. Nunca e veredito do candidato.

Regras (codigo em `core/harness/remote_worker.py` e `core/harness/remote_dispatch.py`):

- O worker fecha o job como `failed` com `error_code=worker_signal_killed` (sem `result_json` de veredito)
  quando o runner termina com returncode negativo, quando algum passo do harness tem exit code negativo, ou
  quando o job dura menos de 15 s sem executar nenhum teste e sem nenhum passo com exit code positivo. O
  exit code e o nome do sinal ficam na linha `[WORKER worker_signal_killed]` do log do job.
- O despachante trata `worker_signal_killed` como infraestrutura: imprime `[REMOTE_RETRY]` (nunca
  `[HARNESS_FAIL]`), espera o mesmo worker voltar (`DARKFAC_WORKER_RESTART_GRACE_SEC`, 180 s) e reenvia ate
  `DARKFAC_WORKER_RESTART_RETRIES` (2) vezes antes de cair para o proximo worker (Desktop).
- Portao de estabilidade: antes de submeter, o despachante exige 3 respostas seguidas de `/health` (ocioso,
  mesmo `instance_id`/`started_at`) com 20 s de intervalo (`DARKFAC_WORKER_STABLE_CHECKS`,
  `DARKFAC_WORKER_STABLE_INTERVAL_SEC`). Com `DARKFAC_WORKER_STABLE_CHECKS=1` a espera some. Worker que nao
  reporta identidade, e execucao so-local, nao esperam.
- Diagnostico do emissor: no Linux o runner roda sob `python -m core.harness.sigterm_probe -- ...`, que
  registra no log do job a linha `[SIGTERM_PROBE] received SIGTERM ... from pid=<PID> uid=<UID>` e depois
  encerra por SIGTERM normalmente (o sinal nao e engolido). `DARKFAC_WORKER_SIGTERM_PROBE=off` desliga.
  Para fechar a causa, correlacione esse PID/horario com `/workspace/darkfac/run/vm-events.log` e `dmesg`.
