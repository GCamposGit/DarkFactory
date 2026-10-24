# USR-207 - Hub sem queda a cada merge

Estado: caminho GHCR e blue-green entregues no repositorio; a prova operacional de zero downtime ainda depende de acesso de pull da VPS ao pacote privado e da sonda OA-058.

## 1. Entregue neste ticket

| Item | Onde |
| --- | --- |
| Todo `fetch` do frontend passa por um unico helper com retry/backoff e banner "Hub atualizando, reconectando..." | `hub/frontend/hub_net.js` (`hubNetFetch`), carregado primeiro em `index.html`; teste estatico `tests/test_hub_deploy_usr207.py::test_no_raw_fetch_outside_the_helper` |
| Classificador de caminhos alterados (ledger/docs/testes nao reconstroem a imagem) | `core/infra/deploy_classifier.py` |
| Um merge => no maximo um deploy: pula se o sha ja esta no ar, pula merge so de dados, junta-se a um deploy `running` iniciado pelo autoDeploy do Dokploy; `--force` ignora | `scripts/dokploy_redeploy.py` (`plan_darkhub_deploy`) |
| `node_sync` nao trata VPS em sha antigo como divergente quando a imagem nao mudou (evita redeploy em loop) | `core/infra/node_sync.py` (`verify`) |
| Sonda externa de `/health` com alerta no Telegram ops apos 120 s e aviso de recuperacao | `core/infra/availability_probe.py` |
| `healthcheck` + `stop_grace_period` no compose do Hub | `deploy/dokploy/docker-compose.hub.yml` |

Notas:

- `live.js` (pagina `/live`, USR-208) mantem o proprio polling e fica fora do helper por enquanto; a lista `RAW_FETCH_ALLOWED` do teste registra a excecao.
- O helper repete GET/HEAD em erro de rede e qualquer metodo em 502/503/504 (o proxy nao entregou o pedido). POST com erro de rede nao e reenviado. Janela total ~85 s (cobre o ~1 min de recriacao). Esgotada, um vigia consulta `/health` e recarrega a pagina quando o Hub volta.
- O autoDeploy do Dokploy dispara no push; o redeploy manual agora detecta o deploy `running` (idade maxima 20 min) e apenas espera por ele. Para eliminar de vez o duplo disparo, desligar o autoDeploy do compose Darkhub (acao do owner, abaixo).

### Agendar a sonda (Notebook ou Desktop, NUNCA a VPS)

```powershell
$py = "C:\Users\guigc\AppData\Local\Programs\Python\Python312\pythonw.exe"
if (-not (Test-Path $py)) { throw "pythonw.exe nao encontrado em $py (confira com: (Get-Command python).Source)" }
$action = New-ScheduledTaskAction -Execute $py -Argument "-m core.infra.availability_probe" -WorkingDirectory "C:\dev\DarkFac"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "DarkFac-Hub-Availability" -Action $action -Trigger $trigger -Force
```

(`pythonw.exe` nao abre janela. Nao executar a partir de agentes; o owner cola no PowerShell ja feito na OA-044.)

## 2. Entregue: imagem GHCR e troca blue-green

Causa raiz: `docker compose up` recria o container parando o antigo antes de subir o novo, e o build roda na VPS (2 vCPU, 3,7 GB).

### 2.1 Imagem construida fora da VPS

Entregue: `.github/workflows/hub-image.yml` publica `ghcr.io/gcamposgit/darkhub:<sha>` para os merges cobertos pelo workflow. O compose pode ser renderizado em modo `ghcr` sem `build:`; o classificador de deploy segue as fontes `COPY` de `deploy/dokploy/Dockerfile.hub`. O teste `test_classifier_rebuild_paths_are_covered_by_hub_image_workflow_trigger` trava a cobertura do workflow; como `.github/workflows/*` e protegido pelo guard, divergencias devem virar acao do owner, sem editar o workflow neste ticket.

### 2.1b Chave de ativacao (USR-339)

O codigo ja suporta as duas fontes de imagem, controladas por `DARKHUB_IMAGE_SOURCE` (ambiente de quem roda `scripts/dokploy_redeploy.py` e do `DokployDeploymentAdapter`):

- `build` (padrao): comportamento de hoje, build do contexto Git na VPS.
- `ghcr`: `render_darkhub_compose` troca `build:`/`pull_policy: build` por `image: ghcr.io/gcamposgit/darkhub:<sha>` + `pull_policy: always`, remove `container_name` e injeta `deploy.update_config.order: start-first` (+ `rollback_config`). O CLI preserva timeout de 900 s e o adapter da linha usa 600 s; ambos chamam `compose_render.wait_for_ghcr_image` (poll de 15 s). Sem a imagem no prazo, a escrita no Dokploy e recusada, sem fallback para build.
- O HEAD do manifesto usa `GHCR_TOKEN` ou `GITHUB_PAT` com `read:packages` (OA-053). Resposta 401/403 sem token falha imediatamente e orienta configurar `GHCR_TOKEN`; com token, 401/403/404 continuam em poll e o ultimo status HTTP aparece no timeout.
- Valor desconhecido falha fechado. `start-first` so vale em modo Stack/Swarm; o modo escolhido pelo owner foi blue-green no Traefik, descrito na secao 2.2.

### 2.1c Estado de ativacao

O workflow e o caminho de renderizacao GHCR ja estao entregues e ativados (`DARKHUB_IMAGE_SOURCE=ghcr`). A primeira imagem e publicada pelo workflow para o SHA do merge. A VPS ainda precisa conseguir fazer pull do pacote privado; a falha operacional observada em 23/10/2026 esta registrada na secao 2.2 e pertence ao USR-363.

### 2.2 Blue-green gated por healthcheck

**Estado entregue (USR-339): o owner escolheu blue-green no Traefik em OA-057; nao migrar para Stack/Swarm.** O bloco `start-first` continua sendo emitido no render GHCR, mas sozinho nao prova troca sem downtime no Docker Compose simples.

O `container_name` fixo foi removido no render GHCR para permitir dois containers simultaneos (o `hostname` preserva a identidade de no, USR-212). O Compose simples ignora `update_config.order: start-first`; a estrategia escolhida e implementada pelo owner (OA-057, 23/10/2026) e **blue-green no Traefik, sem Swarm**, em `core/orchestrator/bluegreen.py` e `scripts/dokploy_bluegreen.py`.

Desenho:
- O compose raw tem `darkhub-blue` e `darkhub-green`, com os mesmos rotulos Traefik (mesmo router/service `darkhub`). O provider Docker encaminha para containers saudaveis. Os dois usam os mesmos volumes nomeados e ambiente; so a imagem/sha por cor muda.
- A cor inativa fica com `deploy.replicas: 0`. O estado fica no compose armazenado no Dokploy como `# darkfac-bluegreen: active=<cor> blue=<sha> green=<sha>`; o proximo deploy le por `compose.one`.
- Fase 1 (overlap): sobe a cor inativa com o sha novo e `replicas: 1`, mantendo a ativa. Aguarda o `/health` publico devolver o sha novo 3 vezes seguidas.
- Fase 2 (cutover): ativa a cor nova e define a antiga com `replicas: 0`.
- Se a cor nova nao servir o sha na janela (30 x 5 s), reinstala o compose anterior; o script encerra com codigo 1.
- A migracao inicial do servico legado ainda precisa de rollback seguro e limpeza do container antigo, escopo do USR-363.
- O caminho exige `ghcr` e aguarda a tag antes de qualquer escrita no Dokploy.

**Resultado da prova em 23/10/2026:** a tentativa de `compose up` terminou em `error` porque a VPS nao conseguiu fazer pull da imagem GHCR privada. Portanto, essa execucao nao demonstra o cutover blue-green nem satisfaz o criterio de zero downtime. Corrigir o acesso privado e o rollback de compose/migracao legada pertence ao USR-363.

**Pendente:** executar a sonda real de 1 s durante deploy, com `non200 = 0` e ambos os SHAs observados. OA-058 segue aberta; depende do USR-363 e de uma nova janela operacional. Nao considerar a prova simulada nem a falha de pull como aceite.

### 2.2b Roteiro de prova (owner)

Terminal 1 (PowerShell): sonda de 1 s por 10 min.

```powershell
python C:\dev\DarkFac\scripts\hub_downtime_probe.py --url https://darkhub.ggcampos.com/health --seconds 600 --out C:\Users\guigc\hub_probe.jsonl --expect-switch
```

Terminal 2, durante a sonda (blue-green; o ultimo merge precisa ter a imagem GHCR publicada):

```powershell
python C:\dev\DarkFac\scripts\dokploy_bluegreen.py
```

Para provar a alternancia, rode uma segunda vez com outro sha (novo merge) e confira no Dokploy/Docker que a cor ativa mudou (`docker ps --filter name=darkhub`).

O comando imprime o resumo ao terminar e retorna codigo 0 somente quando `non200 = 0` e, com `--expect-switch`, pelo menos dois SHAs foram observados. Cada amostra fica em JSONL com timestamp, status, SHA, latencia e erro. A sonda pode ser reproduzida sem acesso ao script externo; OA-058 continua pendente ate a execucao real durante um deploy.

Se `non200 > 0`: confira a ordem das fases nos logs do script (overlap antes de cutover) e se o container legado `darkhub-cloud-247` ainda existia.

### 2.3 Estado das acoes do owner

- OA-044: agendamento da sonda no Notebook/Desktop registrado e cumprido; o comando PowerShell da secao 1 e protegido por `test_availability_probe_schedule_uses_absolute_pythonw_and_safe_powershell_arguments`.
- OA-052/OA-053: configuracao de ativacao e credencial de leitura GHCR cumpridas para o runner que consulta o manifesto. A credencial de pull usada pela VPS/Dokploy ainda falhou na prova de 23/10 e esta no escopo do USR-363.
- OA-057: blue-green no Traefik escolhido e implementado.
- OA-058: prova de sonda de 1 s durante deploy real continua aberta; executar depois da correcao do pull privado pelo USR-363.
- O autoDeploy e a integracao Dokploy permanecem como registrados nas respectivas acoes do owner; esta proposta nao instrui a repetir configuracoes ja concluidas.

## 3. Memoria: como medir antes de mexer nos 512 MB

O limite do container permanece em **512 MB** ate que exista evidencia coletada. O Hub agora amostra seu cgroup a cada 30 s, mantém o maior uso desde o boot e envia alerta ao Telegram de ops quando atingir 80% do limite ou quando o contador `oom_kill` aumentar. A leitura detalhada fica opt-in em `/health?memory=1`; `/health` sem parametro continua leve.

Execute no Notebook ou Desktop, contra a URL publica do Hub. A carga dura 5 minutos, com 20 requisicoes concorrentes distribuidas entre os tres endpoints; o JSON inclui pico percentual/MB, p95, erros e `oom_kills`:

```powershell
python C:\dev\DarkFac\scripts\hub_memory_load.py --base-url https://darkhub.ggcampos.com --concurrency 20 --seconds 300 --out C:\Users\guigc\hub_memory_load.json
```

Durante a coleta e no fim dela, registre tambem `OOMKilled` e `RestartCount` do container ativo no host Docker (ajuste o nome se a cor ativa for `darkhub-blue` ou `darkhub-green`):

```powershell
docker inspect -f "{{.Name}} OOMKilled={{.State.OOMKilled}} RestartCount={{.RestartCount}}" darkhub-cloud-247
```

Repita a medicao em operacao normal por pelo menos uma semana util, incluindo tres deploys e uma rajada de telemetria, e guarde os JSONs e os valores de `docker inspect` junto ao ticket. Se o pico atingir **80% de 512 MB** ou `OOMKilled`/`RestartCount` crescer, suba o limite para **768 MB** e remeca. O limite atual fica em 512 MB ate essa evidencia existir; aumentar a VPS exige evidencia separada de falta de folga no host.
