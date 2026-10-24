# SPEC

## Objetivo
Fechar os residuos do USR-207 que ainda vivem no repositorio, sem repetir o que ja foi entregue em #605/USR-339/USR-365 (imagem GHCR, blue-green em core/orchestrator/bluegreen.py, dedupe de deploy em plan_darkhub_deploy, classificador em core/infra/deploy_classifier.py, sonda externa em core/infra/availability_probe.py, helper hubNetFetch): (1) tirar a ultima excecao de fetch cru do frontend (live.js da pagina /live), (2) trazer para dentro do repositorio, testada, a sonda de 1 s que produz a evidencia de zero downtime (hoje o roteiro aponta para um script fora do repo em C:\Users\guigc\darkfac-ops\tools\health_probe.py), e (3) entregar a medicao de memoria sob carga e o monitoramento de OOMKilled, que e o unico critério de aceite sem nenhuma implementacao (nao existe uma linha lendo cgroup/OOM no repositorio).

## Fora de escopo
- Pull da imagem privada do GHCR na VPS, rollback do blue-green em falha de compose up e remocao/migracao do container legado darkhub-cloud-247 - escopo do USR-363 (planned) e das acoes OA-058/OA-059.
- Editar qualquer arquivo em .github/workflows/** (caminho protegido por core/orchestrator/guard.py); divergencia de gatilho vira acao do owner.
- Migrar o compose Darkhub para Swarm/Stack (o owner escolheu blue-green no Traefik em OA-057).
- Aumentar a VPS ou mudar o limite de 512 MB do container nesta demanda: aqui se entrega a medicao e o criterio, a mudanca do limite exige a evidencia coletada.
- Executar deploy real, prova ao vivo da troca ou reconfigurar Dokploy/autoDeploy (OA-044 e OA-059 ja concluidas; a prova de 1 s em producao segue na OA-058).
- Frescor do ledger .factory embutido na imagem (USR-348, ja completed).

## Desenho
Tres frentes pequenas, todas em arquivos nao protegidos e sem I/O de rede nos testes.

1. Frontend (criterio 3). hub/frontend/hub_net.js e o unico lugar com fetch cru; a lista RAW_FETCH_ALLOWED do teste estatico (tests/test_hub_deploy_usr207.py:329) ainda isenta live.js, que faz fetch direto em fetchSnapshot() (hub/frontend/live.js:1322) com AbortController de 8 s e polling proprio de 10 s. Como a janela de retry do helper (~85 s) nao cabe nessa cadencia, hubNetFetch ganha um orcamento de tentativas por chamada (options.hubNetMaxAttempts); live.js passa 1 tentativa e assim herda banner 'Hub atualizando, reconectando...' + vigia de /health sem empilhar requisicoes, e AbortError continua propagando intacto. live.html passa a carregar hub_net.js antes de live.js (hoje carrega so live.js) e a allowlist do teste fica apenas com hub_net.js.

2. Evidencia de zero downtime (criterio 1). Novo core/infra/downtime_probe.py: amostra GET /health no dominio publico uma vez por segundo (agenda sem drift), grava JSONL {ts,status,sha,latency_ms,error} e expoe summarize(samples) puro (total, non200, maior sequencia de falha, shas distintos); timeout e erro de conexao contam como falha (status 0). scripts/hub_downtime_probe.py e a casca fina com caminho absoluto, no padrao de scripts/dokploy_bluegreen.py; sai 0 somente com non200 == 0 e, com --expect-switch, tambem exige >= 2 shas distintos. docs/proposals/USR-207-zero-downtime.md secao 2.2b passa a citar esse comando do repo em vez do script fora dele, mantendo OA-058 como a prova operacional pendente.

3. Memoria e OOM (criterio 5). Novo core/infra/container_memory.py le o proprio cgroup de dentro do container (v2: memory.current/memory.peak/memory.max/memory.events oom_kill; fallback v1: memory.usage_in_bytes/limit_in_bytes/max_usage_in_bytes/memory.failcnt), modelo Pydantic v2, nunca levanta e devolve 'desconhecido' fora de container ou sem limite. Exposto em GET /health?memory=1 (hub/backend/main.py:185 ja tem o padrao do parametro opcional ?disk=); o /health sem flag fica inalterado para nao encarecer a sonda de 1 s nem o HEALTHCHECK do Dockerfile. Um amostrador em thread registrado no _lifespan (hub/backend/main.py:131, mesmo padrao do refresh de benchmark) guarda o pico e alerta o Telegram de ops em >= 80% do limite e em qualquer aumento de oom_kill, com a mesma convencao de dedupe por janela do core/infra/disk_guard.py; sem token ou sem limite legivel, degrada para log. scripts/hub_memory_load.py gera a carga documentada (concorrencia em /api/cloud/status, /api/usage/accounts, /api/telemetry/tickets) e escreve o relatorio de evidencia (pico %, p95, oom_kills lidos de /health?memory=1). A secao 3 do documento troca o roteiro manual de docker stats por esses dois comandos, mantendo o criterio de decisao (pico >= 80% de 512 MB ou OOMKilled/RestartCount crescendo => 768 MB e remedir).

## Arquivos a tocar
- hub/frontend/hub_net.js
- hub/frontend/live.js
- hub/frontend/live.html
- core/infra/downtime_probe.py
- scripts/hub_downtime_probe.py
- core/infra/container_memory.py
- scripts/hub_memory_load.py
- hub/backend/main.py
- docs/proposals/USR-207-zero-downtime.md
- tests/test_hub_deploy_usr207.py
- tests/js/hub_net.test.cjs
- tests/test_hub_downtime_probe.py
- tests/test_container_memory.py

## Riscos
- A prova ao vivo de zero downtime (sonda de 1 s por HTTPS atravessando um deploy real) nao fecha neste ticket: ele torna a sonda reproduzivel e testada, mas a execucao depende de janela operacional (OA-058). O relatorio final deve declarar isso em vez de simular a prova.
- Defeito observado na leitura do codigo e ja coberto por ticket: no caminho de migracao legada, render_bluegreen_compose emite apenas darkhub-blue/darkhub-green, ou seja a fase de overlap instala um compose sem o servico 'darkhub' - o compose up do Dokploy pode remover o container legado antes do portao de saude da cor nova, abrindo justamente a janela de queda nessa unica migracao. Escopo do USR-363 (planned) + OA-058; aqui so entra como risco documentado, nao se mexe em bluegreen.py.
- Acrescentar memoria ao /health precisa ficar fora do caminho padrao: a sonda externa de 1 s e o HEALTHCHECK do container batem em /health a cada poucos segundos e nao podem pagar leitura extra.
- O retry do helper dentro do polling de /live pode empilhar requisicoes se o orcamento de tentativas nao for limitado; a janela do helper nunca pode passar da cadencia de 10 s da pagina.
- O alerta de memoria sai de dentro do container do Hub e depende de TELEGRAM_OPS_BOT_TOKEN/TELEGRAM_AUTHORIZED_CHATS (ja presentes em deploy/dokploy/docker-compose.hub.yml); sem eles o amostrador deve registrar em log e seguir, nunca falhar o startup.
- Os testes de tests/js/* pulam quando Node nao esta no runner; a garantia real continua nos testes estaticos Python de tests/test_hub_deploy_usr207.py.
- core/infra/deploy_classifier.py trata core/ e scripts/ como caminhos embutidos na imagem: os arquivos novos destes tickets implicam rebuild/deploy do Hub no merge, o que e correto e esperado, mas o merge precisa ter imagem GHCR publicada para o sha.
