# USR-207 - Hub sem queda a cada merge

Estado: parte entregue no repositorio, parte proposta (exige acao do owner no Dokploy/GitHub).

## 1. Entregue neste ticket

| Item | Onde |
| --- | --- |
| Todo `fetch` do frontend passa por um unico helper com retry/backoff e banner "Hub atualizando, reconectando..." | `hub/frontend/hub_net.js` (`hubNetFetch`), carregado primeiro em `index.html`; teste estatico `tests/test_hub_deploy_usr207.py::test_no_raw_fetch_outside_the_helper` |
| Classificador canonico dos caminhos que publicam imagem (`core/`, `hub/`, `scripts/`, `deploy/`, `requirements.txt`, `harness.config.json`) | `core/infra/deploy_classifier.py::image_trigger_paths`; `run_hub.py` nao e copiado por `deploy/dokploy/Dockerfile.hub` |
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
$action = New-ScheduledTaskAction -Execute $py -Argument "-m core.infra.availability_probe" -WorkingDirectory "C:\dev\DarkFac"
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "DarkFac-Hub-Availability" -Action $action -Trigger $trigger -Force
```

(`pythonw.exe` nao abre janela. Nao executar a partir de agentes; o owner cola no PowerShell ja feito na OA-044.)

## 2. Proposto: troca sem downtime (fase 2)

Causa raiz: `docker compose up` recria o container parando o antigo antes de subir o novo, e o build roda na VPS (2 vCPU, 3,7 GB).

### 2.1 Imagem construida fora da VPS

O workflow `.github/workflows/hub-image.yml` ja foi publicado pelo owner (OA-052). O arquivo protegido ainda aceita `docs/**` e `.factory/**`, embora esses caminhos nao alterem a imagem. O patch ao lado agora atualiza o workflow existente para usar a mesma lista canonica de `image_trigger_paths()`; merges so de ledger/docs deixam de construir imagem:

```powershell
git -C C:\dev\DarkFac apply docs/proposals/USR-207-ghcr-build.patch
```

O compose ja usa a imagem GHCR (OA-053 confirmou `DARKHUB_IMAGE_SOURCE=ghcr` ativo e `/health` no SHA novo), sem `build:`; diff exato do servico `darkhub`:

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

O redeploy usa a tag do SHA publicado no GHCR. A credencial de pull do registry GHCR no Dokploy e a sonda agendada no Notebook foram verificadas na OA-044; a credencial do runner `GHCR_TOKEN` foi configurada na OA-053.

### 2.1b Chave de ativacao (USR-339)

O codigo ja suporta as duas fontes de imagem, controladas por `DARKHUB_IMAGE_SOURCE` (ambiente de quem roda `scripts/dokploy_redeploy.py` e do `DokployDeploymentAdapter`):

- `build` (padrao): comportamento de hoje, build do contexto Git na VPS.
- `ghcr`: `render_darkhub_compose` troca `build:`/`pull_policy: build` por `image: ghcr.io/gcamposgit/darkhub:<sha>` + `pull_policy: always`, remove `container_name` e injeta `deploy.update_config.order: start-first` (+ `rollback_config`). O redeploy espera a tag do sha no GHCR (HEAD no manifesto; token em `GHCR_TOKEN` ou `GITHUB_PAT` com `read:packages`; timeout `--timeout`, padrao 900 s) e, se nao existir, recusa com erro explicito, sem voltar para build. A espera compartilhada pelo adapter da linha ainda e lacuna de codigo acompanhada no ticket de implementacao.
- Valor desconhecido falha fechado. `start-first` so vale em modo Stack/Swarm; no Compose simples o Dokploy ignora `update_config` (rota 2 da secao 2.2 cobre isso).
- Limite conhecido: a espera da tag esta no CLI `dokploy_redeploy.py`; o adapter da linha apenas renderiza.

### 2.1c Publicacao do filtro estreito (owner, OA-057 no DarkHub)

1. Abra o PowerShell e crie uma worktree limpa a partir de `origin/main`:
   `git -C C:\dev\DarkFac fetch origin` e `git -C C:\dev\DarkFac worktree add C:\dev\DarkFac\.worktrees\hub-image-filter -b chore/hub-image-filter origin/main`.
2. Aplique o patch de atualizacao do workflow existente: `git -C C:\dev\DarkFac\.worktrees\hub-image-filter apply C:\dev\DarkFac\docs\proposals\USR-207-ghcr-build.patch`.
3. Rode o portao na worktree candidata: `python C:\dev\DarkFac\.worktrees\hub-image-filter\core\harness\runner.py --quick`.
4. Faca commit e push da branch: `git -C C:\dev\DarkFac\.worktrees\hub-image-filter add .github/workflows/hub-image.yml`, `git -C C:\dev\DarkFac\.worktrees\hub-image-filter commit -m "ci: restringe gatilho da imagem Hub [USR-207]"` e `git -C C:\dev\DarkFac\.worktrees\hub-image-filter push -u origin chore/hub-image-filter`; abra e mescle o PR.
5. Verifique no GitHub Actions que o workflow "Hub image (GHCR)" ainda publica em merges que alteram runtime e que um merge somente de `docs/` ou `.factory/` nao dispara nova imagem GHCR. O modo GHCR ja esta ativo; nao e necessario reconfigura-lo.

Estado owner verificado em 22/10/2026: OA-044 concluiu autoDeploy desligado e sonda agendada com `pythonw.exe` absoluto; OA-052 publicou o workflow protegido; OA-053 gravou `GHCR_TOKEN` e confirmou modo GHCR ativo. OA-057 registra somente a publicacao deste filtro estreito.

### 2.2 Start-first gated por healthcheck

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
