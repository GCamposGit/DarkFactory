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
schtasks /Create /TN "DarkFac-Hub-Availability" /SC MINUTE /MO 1 /F /TR "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command \"Set-Location C:\dev\DarkFac; python -m core.infra.availability_probe\""
```

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
