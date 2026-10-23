# USR-207 - Hub sem queda a cada merge

Estado: fase 2 de codigo entregue. A validacao sem queda ainda depende de `composeType` Stack/Swarm ou da decisao do Owner para blue/green (OA-057), e de evidencia coletada durante deploy real.

## 1. Entregue neste ticket

| Item | Onde |
| --- | --- |
| Todo `fetch` do frontend passa por um unico helper com retry/backoff e banner "Hub atualizando, reconectando..." | `hub/frontend/hub_net.js` (`hubNetFetch`), carregado primeiro em `index.html`; teste estatico `tests/test_hub_deploy_usr207.py::test_no_raw_fetch_outside_the_helper` |
| Classificador de caminhos alterados (ledger/docs/testes nao reconstroem a imagem) | `core/infra/deploy_classifier.py` |
| Um merge => no maximo um deploy: pula se o sha ja esta no ar, pula merge so de dados, junta-se a um deploy `running` iniciado pelo autoDeploy do Dokploy; `--force` ignora | `scripts/dokploy_redeploy.py` (`plan_darkhub_deploy`) |
| `node_sync` nao trata VPS em sha antigo como divergente quando a imagem nao mudou (evita redeploy em loop) | `core/infra/node_sync.py` (`verify`) |
| Sonda externa de `/health` com alerta no Telegram ops apos 120 s e aviso de recuperacao | `core/infra/availability_probe.py` |
| `healthcheck` + `stop_grace_period` no compose do Hub | `deploy/dokploy/docker-compose.hub.yml` |
| GHCR como fonte padrao, imagem imutavel por SHA e espera compartilhada da tag | `core/orchestrator/compose_render.py`, `core/infra/ghcr.py`, `scripts/dokploy_redeploy.py`, `core/orchestrator/deployment_adapter.py` |
| `start-first` sem `container_name`; aviso quando `composeType` nao indica Stack/Swarm | renderer, CLI e adapter de deploy |
| Sonda externa de 1 s durante o deploy, com evidencia JSON e verificacao de 100% HTTP 200 + convergencia do SHA | `core/infra/switchover_probe.py`, chamada pelo redeploy com `--wait` |

Notas:

- `live.js` (pagina `/live`, USR-208) mantem o proprio polling e fica fora do helper por enquanto; a lista `RAW_FETCH_ALLOWED` do teste registra a excecao.
- O helper repete GET/HEAD em erro de rede e qualquer metodo em 502/503/504 (o proxy nao entregou o pedido). POST com erro de rede nao e reenviado. Janela total ~85 s (cobre o ~1 min de recriacao). Esgotada, um vigia consulta `/health` e recarrega a pagina quando o Hub volta.
- O autoDeploy do Dokploy dispara no push; o redeploy manual agora detecta o deploy `running` (idade maxima 20 min) e apenas espera por ele. Para eliminar de vez o duplo disparo, desligar o autoDeploy do compose Darkhub (acao do owner, abaixo).

### Agendar a sonda (Notebook ou Desktop, NUNCA a VPS)

```powershell
$py = "C:\Users\guigc\AppData\Local\Programs\Python\Python312\pythonw.exe"
$action = New-ScheduledTaskAction -Execute $py -Argument "-m core.infra.availability_probe" -WorkingDirectory "C:\dev\DarkFac"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "DarkFac-Hub-Availability" -Action $action -Trigger $trigger -Force
```

(`pythonw.exe` nao abre janela. Nao executar a partir de agentes; o owner cola no PowerShell ja feito na OA-044.)

## 2. Fase 2: imagem GHCR e troca sem downtime

Causa raiz: `docker compose up` recria o container parando o antigo antes de subir o novo, e o build roda na VPS (2 vCPU, 3,7 GB).

### 2.1 Imagem construida fora da VPS

Aplicar `docs/proposals/USR-207-ghcr-build.patch` (cria `.github/workflows/hub-image.yml`, protegido pelo guard):

```powershell
git -C C:\dev\DarkFac apply docs/proposals/USR-207-ghcr-build.patch
```

O workflow protegido foi publicado pela OA-052; a imagem GHCR e agora a fonte padrao do CLI e do adapter, fixada no SHA completo. O compose versionado continua declarando `build:` como base para o escape hatch `DARKHUB_IMAGE_SOURCE=build`; o renderer converte essa base para `image: ghcr.io/gcamposgit/darkhub:<sha>`, `pull_policy: always`, `start-first` e `failure_action: rollback` no modo padrao. A credencial privada e a tag inicial foram verificadas nas OA-052/OA-053.

O modo de rollback manual e explicito:

```diff
-    build:
-      context: https://${GITHUB_PAT:-}@github.com/GCamposGit/DarkFactory.git#${DARKFAC_GIT_SHA}
-      dockerfile: deploy/dokploy/Dockerfile.hub
-      no_cache: true
-      args:
-        DARKFAC_GIT_SHA: ${DARKFAC_GIT_SHA}
-    image: darkhub:v2.1
-    pull_policy: build
-    container_name: darkhub-cloud-247
+    image: ghcr.io/gcamposgit/darkhub:${DARKFAC_GIT_SHA}
+    pull_policy: always
     hostname: darkhub-cloud-247
```

Efeitos em codigo que hoje exige `build:`: `core/orchestrator/compose_render.py` (`render_darkhub_compose` valida o contexto Git fixado) e o teste `test_darkhub_build_pinned_before_deploy_and_health_converges` precisam ser adaptados na mesma entrega; o redeploy deve esperar o workflow publicar a tag do sha antes de acionar o Dokploy. O pacote GHCR precisa ficar privado com credencial de pull no Dokploy (registry).

### 2.1b Contrato atual de imagem (USR-339 / USR-207)

O codigo ja suporta as duas fontes de imagem, controladas por `DARKHUB_IMAGE_SOURCE` (ambiente de quem roda `scripts/dokploy_redeploy.py` e do `DokployDeploymentAdapter`):

- `ghcr` (padrao): transforma a base `build:` em imagem GHCR imutavel com SHA completo, remove `container_name` e injeta `pull_policy: always`, `update_config.order: start-first` e rollback.
- `build` (escape hatch explicito): mantém contexto Git pinado, `no_cache` e `pull_policy: build`.
- Valor desconhecido falha fechado. CLI e adapter compartilham `core.infra.ghcr.wait_for_ghcr_tag`; 401/403 sao erros de credencial, 404/timeout nao acionam fallback para build. Configure `GHCR_TOKEN` ou `GITHUB_PAT` com `read:packages`.
- `start-first` so vale em Stack/Swarm. CLI e adapter consultam `composeType`; Compose simples ou tipo desconhecido gera aviso e referencia OA-057, sem prometer ausencia de queda.
- Com `--wait`, o redeploy mede externamente `/health` a cada segundo durante a implantacao e grava `.factory/evidence/hub-switchover-<sha>.json`. O aceite exige que todas as amostras sejam HTTP 200 e que pelo menos uma reporte o SHA esperado.

### 2.1c Publicacao do workflow (concluida via OA-052)

O Owner ja publicou `hub-image.yml`, confirmou a primeira imagem e gravou `GHCR_TOKEN` conforme OA-052/OA-053. Nao reaplique o patch protegido. Para diagnostico e rollback manual, os comandos historicos abaixo permanecem como referencia:

1. Abra o PowerShell e crie uma worktree limpa (somente se uma futura alteracao protegida for autorizada):
   `git -C C:\dev\DarkFac fetch origin` e `git -C C:\dev\DarkFac worktree add C:\dev\DarkFac\.worktrees\hub-image-publish -b chore/hub-image-workflow origin/main`
2. Aplique o patch: `git -C C:\dev\DarkFac\.worktrees\hub-image-publish apply C:\dev\DarkFac\docs\proposals\USR-207-ghcr-build.patch`
3. Rode o portao: `python C:\dev\DarkFac\core\harness\runner.py --quick` (dentro da worktree, ajustando o caminho).
4. Commit e push: `git -C C:\dev\DarkFac\.worktrees\hub-image-publish add .github/workflows/hub-image.yml`, `git -C ... commit -m "ci: publica imagem do Hub no GHCR [USR-207]"`, `git -C ... push -u origin chore/hub-image-workflow` e abra/mescle o PR.
5. Verifique: aba Actions do GitHub, workflow "Hub image (GHCR)" verde; em github.com/GCamposGit > Packages deve existir `darkhub` com a tag igual ao sha do commit em `main`.
6. So depois de a primeira imagem existir: defina `DARKHUB_IMAGE_SOURCE=ghcr` (Variaveis de Ambiente de Usuario do Windows, ou no ambiente do runner/orquestrador) e rode `python C:\dev\DarkFac\scripts\dokploy_redeploy.py --only Darkhub --wait`.
7. Volte para `build` removendo a variavel se algo falhar.

### 2.2 Start-first gated por healthcheck e tipo do servico

`container_name` fixo impede dois containers simultaneos; remover (o `hostname` ja preserva a identidade de no, USR-212). O Docker Compose simples nao tem `start-first`; ha duas rotas:

1. Dokploy em modo Stack (Swarm) para o compose do Hub, com:

```diff
     deploy:
+      update_config:
+        order: start-first
+        parallelism: 1
+        monitor: 20s
+        failure_action: rollback
+      rollback_config:
+        order: start-first
       resources:
```

   (o `healthcheck` ja aplicado no compose e o que libera o corte do trafego.)

2. Sem Swarm: o Dokploy ignora `update_config`; CLI e adapter avisam e a sonda registra qualquer queda. A rota alternativa e dois servicos `darkhub-blue` / `darkhub-green` com rotulos Traefik distintos. Ela exige decisao do Owner e ticket proprio; OA-057 oferece as opcoes.

Criterio de aceite operacional: sonda externa de 1 s durante deploy real com 100% HTTP 200 em `/health` e convergencia de `git_sha`. O teste local cobre amostragem e gravacao da evidencia com HTTP injetado; nao substitui a medicao externa real.

### 2.3 Acoes do owner (registradas no DarkHub)

- Desligar o autoDeploy do compose Darkhub no Dokploy (o deploy passa a ser so o do script).
- OA-044: autoDeploy desligado, credencial GHCR criada e sonda externa agendada (concluida).
- OA-052/OA-053: workflow publicado, token configurado e primeira imagem validada (concluidas).
- OA-057: escolher Stack/Swarm ou aprovar ticket de blue/green se o servico reportar Compose simples.

## 3. Memoria: como medir antes de mexer nos 512 MB

Nao alterar o limite agora. Evidencia necessaria para mudar:

1. Coletar na VPS, durante pelo menos uma semana util e incluindo ao menos 3 deploys e uma rajada de telemetria:

```bash
docker stats --no-stream --format "{{.Name}} {{.MemUsage}} {{.MemPerc}}" darkhub-cloud-247   # a cada 30 s via cron
docker inspect -f "{{.State.OOMKilled}} {{.RestartCount}}" darkhub-cloud-247
dmesg -T | grep -i "out of memory"
free -m
```

2. Carga sintetica contra o container (do Notebook): 20 requisicoes simultaneas em `/api/cloud/status`, `/api/usage/accounts` e `/api/telemetry/tickets` por 5 min, anotando o pico de `MemUsage` e a latencia p95.
3. Observar o pico durante o build local atual (RAM da VPS ~79%): a memoria que falta e do build, nao do runtime; com a imagem vinda do GHCR esse pico some.
4. Criterio para subir o limite: pico RSS do container >= 80% de 512 MB em operacao normal OU `OOMKilled=true`/`RestartCount` crescendo. Subir para o proximo degrau (768 MB) e remedir; aumentar a VPS so se `free -m` mostrar a VPS sem folga com o limite novo (non-goal do ticket: nao aumentar a VPS sem medicao).
5. Registrar os numeros em `docs/` (ou no ticket) antes da mudanca de `deploy.resources.limits.memory`.
