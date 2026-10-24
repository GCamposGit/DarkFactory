# SPEC

## Objetivo
Fechar as lacunas que ainda derrubam o Hub a cada merge, reaproveitando o que ja existe no repositorio (workflow GHCR privado por sha, render `ghcr` default, blue-green no Traefik com rollback, classificador/dedup de deploy, helper `hubNetFetch`, sonda externa de /health). Seis frentes: (1) tornar o blue-green o caminho DEFAULT do deploy do Hub, porque hoje `core/orchestrator/bluegreen.py:50-54` so o ativa com `DARKHUB_DEPLOY_MODE=bluegreen` ou com o marcador ja gravado e o caminho default usa o render de servico unico, cujo `start-first` o Compose simples ignora (stop-then-start = a queda); (2) medir a indisponibilidade dentro do proprio deploy com uma sonda de 1 s que grava evidencia (hoje a sonda de 1 s vive fora do repositorio, em `C:\Users\guigc\darkfac-ops\tools\health_probe.py`); (3) manter os dados que o Hub mostra frescos quando o deploy da imagem e dispensado (USR-348 resolveu somente `owner_actions.json`; `demands.json` e os demais `.factory/**`/`docs/**` embutidos pelo `Dockerfile.hub:40,43,44` continuam a copia da imagem); (4) medir memoria sob carga, monitorar `OOMKilled` e travar o orcamento do overlap (o blue-green dobra o Hub para 2 x 512 MB numa VPS cujos limites de compose ja somam ~4,2 GB em 3,7 GB); (5) dar prova de vida a sonda externa, hoje uma tarefa agendada no Windows cuja morte e indistinguivel de um Hub saudavel; (6) endurecer a camada de rede do frontend, onde o helper hoje replica POST/PUT/DELETE em 502/503/504, nao tem timeout proprio, convive com 7 fallbacks `fetch` nus em `audio.js`/`tasks.js`/`interventions.js` que o portao estatico nao detecta, e `live.js` segue fora do helper.

## Fora de escopo
- Trocar o Dokploy por outra plataforma de deploy ou migrar o compose para Stack/Swarm (OA-057 escolheu blue-green no Traefik).
- Aumentar a VPS ou subir o limite de 512 MB do container sem evidencia medida (o T4 entrega o coletor e o criterio, nao a mudanca do limite).
- Editar `.github/workflows/*` (caminho protegido por `core/orchestrator/guard.py`): divergencia de gatilho vira acao do owner.
- Tornar o pacote `ghcr.io/gcamposgit/darkhub` publico: o owner decidiu no grill manter a imagem privada e corrigir a autenticacao de pull (ja feita na OA-059).
- Executar deploy real, `scripts/dokploy_bluegreen.py --restore-legacy` ou remover o container legado `darkhub-cloud-247` sem autorizacao explicita do owner (OA-058 aberta, OA-059 exige frase de autorizacao).
- Redesenhar paineis do Hub ou a pagina /live alem da camada de rede e do aviso de indisponibilidade.

## Desenho
Caminho de deploy: `scripts/dokploy_redeploy.py:1115-1131` e `core/orchestrator/deployment_adapter.py:442-444` ja ramificam para `run_bluegreen_via_dokploy`; basta inverter o default de `is_bluegreen` (opt-out explicito `DARKHUB_DEPLOY_MODE=single`, valor desconhecido falha fechado) e recusar blue-green quando `DARKHUB_IMAGE_SOURCE=build`, em vez de cair silenciosamente no caminho com queda. O portao de manifesto GHCR (`compose_render.wait_for_ghcr_image`) continua antes de qualquer escrita no Dokploy. Evidencia: um novo `core/infra/deploy_probe.py` com um amostrador de 1 s (clock/sleep/http injetaveis) que reaproveita `core.infra.availability_probe.http_probe`, roda em thread do inicio do overlap ao fim do cutover dentro de `run_bluegreen_deploy`, grava JSONL + resumo em `state_root()/reports/deploy/` (diretorio adicionado ao `.gitignore`, na linha do USR-352) e devolve veredito `non200 == 0` e os dois shas observados; timeout e erro de conexao contam como falha. O veredito nao inventa um rollback novo: a restauracao do compose anterior segue sendo a do health gate; non200 > 0 faz o CLI sair diferente de zero e alertar o Telegram ops por `availability_probe.default_sender`. Frescor de dados: generalizar `core/owner_actions/remote_ledger.py` em `core/infra/remote_data.py` (leitor cacheado da API de conteudos do GitHub, TTL <= 120 s, nunca levanta, fallback para a copia embutida) e ligar o caminho de leitura de `.factory/demands/demands.json` em `hub/backend/service.py:386,450`, preservando escrita local; um teste de inventario lista os arquivos de dados que o Hub serve e exige que cada um esteja em `REBUILD_PREFIXES`/`REBUILD_FILES` ou tenha leitor remoto registrado. Memoria: `core/infra/hub_memory_guard.py` amostra os containers do Hub pela API Dokploy (mesmo transporte de `scripts/dokploy_redeploy.py:482`), grava evidencia, alerta ops em `OOMKilled`/RSS >= 80% do limite e expoe `recommend_limit()` com o criterio do `docs/proposals/USR-207-zero-downtime.md` secao 3; roda como thread do daemon `core.infra.backup_cron`, ao lado do `disk_guard`, seguindo o mesmo padrao de isolamento de falha. Sonda externa: `availability_probe.tick` passa a registrar `last_tick_ts`/`last_ok_ts` e a postar heartbeat para o Hub com a chave de telemetria existente; o Hub alerta ops quando o heartbeat envelhece (default 10 min) e `health_ops.js` mostra 'sonda sem contato' - cada lado vigia o outro. Frontend: `hubNetFetch` ganha deadline por tentativa (AbortController composto com o `signal` do chamador), orcamento de retentativa por chamada, e deixa de repetir metodos nao idempotentes em 502/503/504; `live.html` passa a carregar `hub_net.js` e `live.js:1322` usa o helper com orcamento curto, mantendo o proprio timeout e a UI de conexao, o que permite encolher `RAW_FETCH_ALLOWED` para `hub_net.js`.

## Arquivos a tocar
- core/orchestrator/bluegreen.py
- core/orchestrator/compose_render.py
- scripts/dokploy_redeploy.py
- scripts/dokploy_bluegreen.py
- core/orchestrator/deployment_adapter.py
- core/infra/deploy_probe.py
- core/infra/availability_probe.py
- core/infra/hub_memory_guard.py
- core/infra/backup_cron.py
- core/infra/remote_data.py
- core/owner_actions/remote_ledger.py
- core/demands/store.py
- hub/backend/service.py
- hub/backend/api.py
- hub/frontend/hub_net.js
- hub/frontend/live.js
- hub/frontend/live.html
- hub/frontend/infra.js
- hub/frontend/interventions.js
- hub/frontend/tasks.js
- hub/frontend/audio.js
- hub/frontend/health_ops.js
- deploy/dokploy/Dockerfile.hub
- deploy/dokploy/docker-compose.hub.yml
- scripts/hub_load_probe.py
- .gitignore
- docs/proposals/USR-207-zero-downtime.md
- tests/test_hub_deploy_usr207.py
- tests/test_bluegreen_usr339.py
- tests/test_dokploy_redeploy.py
- tests/js/hub_net.test.cjs

## Riscos
- `render_darkhub_compose` esta acoplado por regex literal a `deploy/dokploy/docker-compose.hub.yml` (`compose_render.py:59-61` exige `image: darkhub:v2.1` + `pull_policy: build`, e `:196` exige exatamente 3 ocorrencias de `${DARKFAC_GIT_SHA}`): qualquer edicao cosmetica no compose recusa TODO deploy do Hub. Mexer no compose (T4) obriga rodar o render nos testes.
- A primeira execucao blue-green na VPS faz a migracao do servico legado; OA-058 esta ABERTA e o container legado `darkhub-cloud-247` compartilha os mesmos rotulos Traefik, o que dividiria o trafego. Ligar o default (T1) sem OA-058 resolvida pode piorar a disponibilidade; o ticket deve registrar/atualizar a acao do owner no mesmo turno.
- O overlap do blue-green dobra a memoria do Hub (2 x 512 MB) numa VPS de 3,7 GB cujos limites de compose ja somam ~4,2 GB; um OOM durante o overlap mataria a cor ativa e causaria exatamente a queda que se quer eliminar. Por isso o orcamento do overlap e teste, nao comentario.
- `.github/workflows/hub-image.yml` dispara em `docs/**` e `.factory/**`, que o classificador trata como data-only: a imagem e publicada e nunca implantada, e o conteudo embutido de `docs/`/`.factory/` divergente do main. O arquivo e protegido, entao a correcao do gatilho e acao do owner; o T3 resolve o lado que importa (dados frescos) sem editar o workflow.
- O pull privado do GHCR na VPS depende de um `docker login` manual com PAT classico de validade de 1 ano (OA-059), nao configurado nem rotacionado por codigo: um token expirado falha tarde, como erro de `compose up`. Registrar acao do owner para a credencial de registry no Dokploy e para o vencimento.
- Fazer o deploy sair diferente de zero por non200 > 0 pode travar a esteira; por isso a evidencia e sempre gravada, o alerta vai para o ops e o rollback continua restrito a falha do health gate, sem inventar reversao nova.
- Ler dados de `origin/main` no caminho de requisicao do Hub introduz dependencia da API do GitHub; manter cache, nunca levantar e sempre cair para a copia embutida (padrao ja provado em `remote_ledger.py`).
- Os testes do amostrador e dos guardas sao sensiveis a tempo; `pytest.ini` impoe timeout de 120 s e marcadores estritos. Usar clock/sleep/http injetados, zero rede, e marcar `serial` apenas se houver estado compartilhado.
- A prova operacional de zero downtime nao pode ser produzida de dentro do worktree: ela sai do artefato de evidencia gravado pelo deploy pos-merge do T2. Nenhum ticket deve ter como aceite 'rodar um deploy real'.
