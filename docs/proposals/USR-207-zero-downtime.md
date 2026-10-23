# USR-207 - Hub sem queda a cada merge

Estado: parte entregue no repositorio, parte proposta (exige acao do owner no Dokploy/GitHub).

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

## 2. Proposto: troca sem downtime (fase 2)

Causa raiz: `docker compose up` recria o container parando o antigo antes de subir o novo, e o build roda na VPS (2 vCPU, 3,7 GB).

### 2.1 Imagem construida fora da VPS

Aplicar `docs/proposals/USR-207-ghcr-build.patch` (cria `.github/workflows/hub-image.yml`, protegido pelo guard):

```powershell
git -C C:\dev\DarkFac apply docs/proposals/USR-207-ghcr-build.patch
```

Depois o compose passa a usar a imagem (sem `build:`), diff exato do servico `darkhub`:

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

### 2.1b Chave de ativacao (USR-339)

O codigo ja suporta as duas fontes de imagem, controladas por `DARKHUB_IMAGE_SOURCE` (ambiente de quem roda `scripts/dokploy_redeploy.py` e do `DokployDeploymentAdapter`):

- `build` (padrao): comportamento de hoje, build do contexto Git na VPS.
- `ghcr`: `render_darkhub_compose` troca `build:`/`pull_policy: build` por `image: ghcr.io/gcamposgit/darkhub:<sha>` + `pull_policy: always`, remove `container_name` e injeta `deploy.update_config.order: start-first` (+ `rollback_config`). O redeploy espera a tag do sha no GHCR (HEAD no manifesto; token em `GHCR_TOKEN` ou `GITHUB_PAT` com `read:packages`; timeout `--timeout`, padrao 900 s) e, se nao existir, recusa com erro explicito, sem voltar para build.
- Valor desconhecido falha fechado. `start-first` so vale em modo Stack/Swarm; no Compose simples o Dokploy ignora `update_config` (rota 2 da secao 2.2 cobre isso).
- A espera da tag existe nos dois caminhos: CLI `dokploy_redeploy.py` e `DokployDeploymentAdapter` da linha (`compose_render.wait_for_ghcr_image`, timeout 600 s, poll 15 s). Sem a imagem no prazo o adapter devolve `release_identity_ghcr_image_unpublished` antes de qualquer chamada de escrita ao Dokploy (sem fallback para build).

### 2.1c Ativacao passo a passo (owner, OA registrada no DarkHub)

1. Abra o PowerShell e crie uma worktree limpa:
   `git -C C:\dev\DarkFac fetch origin` e `git -C C:\dev\DarkFac worktree add C:\dev\DarkFac\.worktrees\hub-image-publish -b chore/hub-image-workflow origin/main`
2. Aplique o patch: `git -C C:\dev\DarkFac\.worktrees\hub-image-publish apply C:\dev\DarkFac\docs\proposals\USR-207-ghcr-build.patch`
3. Rode o portao: `python C:\dev\DarkFac\core\harness\runner.py --quick` (dentro da worktree, ajustando o caminho).
4. Commit e push: `git -C C:\dev\DarkFac\.worktrees\hub-image-publish add .github/workflows/hub-image.yml`, `git -C ... commit -m "ci: publica imagem do Hub no GHCR [USR-207]"`, `git -C ... push -u origin chore/hub-image-workflow` e abra/mescle o PR.
5. Verifique: aba Actions do GitHub, workflow "Hub image (GHCR)" verde; em github.com/GCamposGit > Packages deve existir `darkhub` com a tag igual ao sha do commit em `main`.
6. So depois de a primeira imagem existir: defina `DARKHUB_IMAGE_SOURCE=ghcr` (Variaveis de Ambiente de Usuario do Windows, ou no ambiente do runner/orquestrador) e rode `python C:\dev\DarkFac\scripts\dokploy_redeploy.py --only Darkhub --wait`.
7. Volte para `build` removendo a variavel se algo falhar.

### 2.2 Start-first gated por healthcheck

**Veredito (USR-339): `deploy.update_config.order: start-first` so tem efeito se o compose do Hub no Dokploy for do tipo `stack` (Swarm).** Um compose do tipo `docker-compose` roda `docker compose up` e ignora `update_config`: o container antigo e parado antes do novo (queda de ~1 min, mitigada so pelo retry do frontend). Confirmacao: Dokploy > servico Darkhub > aba General > "Compose Type" deve estar em "Stack". Se estiver em "Docker Compose", a troca start-first NAO ocorre e o bloco injetado pelo render e inerte. O Stack exige a VPS como no Swarm (`docker swarm init`), rotulos Traefik sob `deploy.labels` e rede overlay.

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

2. Sem Swarm: dois servicos `darkhub-blue` / `darkhub-green` com rotulos Traefik distintos; o redeploy sobe o inativo, espera `healthy` e so entao troca o roteador (pode ser feito em `scripts/dokploy_redeploy.py`).

Criterio de aceite do ticket: sonda de 1 s durante um deploy real sem nenhuma resposta diferente de 200 em `/health`.

### 2.2b Roteiro de prova (owner)

Terminal 1 (PowerShell): sonda de 1 s por 10 min.

```powershell
python C:\Users\guigc\darkfac-ops\tools\health_probe.py C:\Users\guigc\hub_probe.jsonl 600
```

Terminal 2, durante a sonda (`--force` ignora o "ja esta no ar"):

```powershell
$env:DARKHUB_IMAGE_SOURCE = "ghcr"
python C:\dev\DarkFac\scripts\dokploy_redeploy.py --only Darkhub --wait --force
```

Verificacao ao fim da sonda (esperado: `non200 = 0` e dois shas, antigo e novo):

```powershell
$r = Get-Content C:\Users\guigc\hub_probe.jsonl | ForEach-Object { $_ | ConvertFrom-Json }
"total = $($r.Count)"; "non200 = $(($r | Where-Object { $_.status -ne 200 }).Count)"; "shas = $((($r | Where-Object sha).sha | Select-Object -Unique) -join ', ')"
```

Se `non200 > 0`: o compose esta em modo `docker-compose` (veredito acima); passa so com Stack/Swarm ou com a rota 2.

### 2.3 Acoes do owner (o orquestrador registra as OA)

- Desligar o autoDeploy do compose Darkhub no Dokploy (o deploy passa a ser so o do script).
- Criar credencial de pull do GHCR no Dokploy e escolher a rota 2.2.
- Agendar a sonda da secao 1.

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
