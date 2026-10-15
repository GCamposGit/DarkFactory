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

## 4. Subir no boot (systemd)

O worker foi iniciado a mao e cai se a VM reiniciar. A causa do SIGTERM de 14/10 foi o worker rodando
sem o Python certo no PATH. Entrypoint (o mesmo que `scripts/install_test_worker.ps1` registra no
Windows): `python core/harness/remote_worker.py --host 0.0.0.0 --port 8080 --node-id <id>`.

Na VM, como root, crie `/etc/systemd/system/darkfac-test-worker.service` (ajuste `VENV` se o Python
do repo estiver em outro diretorio e `User` para o usuario dono de `/workspace`):

```ini
[Unit]
Description=DarkFac remote test worker (core/harness/remote_worker.py, port 8080)
After=network-online.target tailscaled.service
Wants=network-online.target tailscaled.service

[Service]
Type=simple
User=root
WorkingDirectory=/workspace/darkfac/DarkFactory
# Python do repo ANTES do sistema: sem isso o worker usava outro python e recebia SIGTERM (14/10/2026).
Environment=PATH=/workspace/darkfac/DarkFactory/.venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUTF8=1
ExecStart=/workspace/darkfac/DarkFactory/.venv/bin/python core/harness/remote_worker.py --host 0.0.0.0 --port 8080 --node-id grokbox-test-worker --project-root /workspace/darkfac/DarkFactory
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Se o repo nao usa `.venv`, troque os dois caminhos `.venv/bin` pelo diretorio do Python 3.13 correto
(`command -v python3.13`).

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now darkfac-test-worker
sudo systemctl status darkfac-test-worker --no-pager
sudo journalctl -u darkfac-test-worker -f
curl -s -m 5 http://127.0.0.1:8080/health
```

O Vigia do Tailscale no grokbox roda a cada 5 min e cuida SOMENTE do Tailscale; ele nao reinicia o
worker. Quem mantem o worker de pe e o systemd (`Restart=always`).

## 5. Quando o Tailscale perde o servidor de controle

Sintomas: `tailscale status` mostra o grokbox como `offline`/`idle`, ou `health` expira mesmo com 10 s,
e o runner imprime `[REMOTE] http://100.77.130.5:8080 unreachable ...` e segue para o Desktop e depois
para o local. O portao nunca trava por isso, apenas fica lento. Para recuperar:

1. No Notebook, `tailscale status` e `tailscale netcheck`; se a conta aparece desconectada, reabra o
   cliente Tailscale e entre de novo.
2. No grokbox (console da VM), `sudo tailscale status`; se disser "Logged out" ou "can't reach
   control", rode `sudo tailscale up` e confira se o no volta a `100.77.130.5` (o Vigia tenta isso a
   cada 5 min).
3. Confirme que o worker segue de pe (`systemctl status darkfac-test-worker`) e repita o `curl.exe`
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
