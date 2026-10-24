# SPEC

## Objetivo
Fechar operacionalmente os criterios do USR-207/USR-339 (Hub sem queda a cada merge) sobre o que ja existe no repo: conciliar o que a imagem embute com o workflow GHCR e o classificador de deploy, garantir uma unica operacao logica de deploy por merge, provar zero indisponibilidade por HTTPS com sonda de 1 s atravessando a troca blue/green, eliminar o ultimo fetch cru do frontend, medir memoria sob carga com sobreposicao de containers e verificar a sonda externa com alerta no Telegram ops.

## Fora de escopo
- Trocar o Dokploy por outra plataforma de deploy ou migrar para Docker Swarm (OA-057 ja decidiu blue/green via Traefik).
- Aumentar a VPS ou o plano Hetzner sem medicao de memoria.
- Tornar o pacote GHCR publico (owner decidiu manter imagem privada com pull autenticado).
- Reescrever o frontend do Hub, mudar visual dos paineis ou alterar contratos de API nao relacionados ao deploy.
- Reconfigurar o destino Telegram ops ou o agendamento externo ja registrados (apenas verificar operacao).

## Desenho
A base ja existe: .github/workflows/hub-image.yml publica ghcr.io/gcamposgit/darkhub:<sha>, core/infra/deploy_classifier.py decide rebuild, core/orchestrator/bluegreen.py + scripts/dokploy_bluegreen.py fazem a troca gated por /health, hub/frontend/hub_net.js centraliza o fetch com retry/backoff e banner 'Hub atualizando, reconectando...', e core/infra/availability_probe.py alerta o Telegram ops apos 120 s de /health falhando. As lacunas sao de coerencia e de evidencia. (1) Coerencia de caminhos: Dockerfile.hub copia docs/ e .factory/ para dentro da imagem, mas REBUILD_PREFIXES do classificador cobre apenas core/, hub/, scripts/, deploy/, requirements.txt e harness.config.json, enquanto hub-image.yml dispara tambem em docs/** e .factory/**; o resultado e imagem construida sem deploy e dado embutido ficando velho quando o deploy e dispensado. A correcao e tornar .factory/ e docs/ dados montados (volume ja existente darkhub-factory-data, mais o padrao seed ja presente em /app/.factory_seed e /app/hub_seed) atualizados por um sync leve pos-merge que nao recria container, e travar Dockerfile x workflow x classificador com um teste de consistencia que le as linhas COPY. (2) Um deploy por merge: um unico ponto de entrada (dokploy_redeploy delegando ao blue/green), curto-circuito por sha ja implantado (REASON_SAME_SHA) e lock de concorrencia, com as fases de sobreposicao e retirada contando como uma operacao logica; autoDeploy do compose Darkhub verificado por API e divergencia virando OA. (3) Evidencia de zero downtime: sonda HTTPS de 1 s no dominio publico executada em paralelo a run_bluegreen_deploy, com timeout e erro de conexao contando como falha, confirmacao de que o novo sha passou a servir e artefato de evidencia; qualquer amostra diferente de 200 reprova o deploy. (4) Frontend: live.js ainda chama fetch cru; mover para hubNetFetch e adicionar guarda de repo proibindo fetch( fora de hub_net.js, preservando a regra de nao repetir metodos mutaveis. (5) Memoria: medir container sob carga (todas as abas + refresh) e durante a sobreposicao blue+green (2x512 MB numa VPS de 3,7 GB a 79%), registrar evidencia e ajustar o limite no compose com base nela, com monitor de OOMKilled. (6) Sonda externa: verificar execucao agendada real e o caminho de alerta/recuperacao no Telegram ops, e fechar OA-058 (container legado darkhub-cloud-247).

## Arquivos a tocar
- deploy/dokploy/Dockerfile.hub
- deploy/dokploy/docker-compose.hub.yml
- .github/workflows/hub-image.yml
- core/infra/deploy_classifier.py
- core/orchestrator/compose_render.py
- core/orchestrator/bluegreen.py
- scripts/dokploy_bluegreen.py
- scripts/dokploy_redeploy.py
- core/infra/availability_probe.py
- hub/frontend/live.js
- hub/frontend/hub_net.js
- tests/test_hub_deploy_usr207.py
- .factory/owner_actions/owner_actions.json
- docs/

## Riscos
- Tirar .factory/ e docs/ da imagem pode quebrar o Hub em producao se algum modulo ler esses caminhos em import time; mitigar mantendo o seed em /app/.factory_seed e /app/hub_seed com fallback e teste de boot.
- A sobreposicao blue/green dobra a memoria do Hub numa VPS ja em 79%; medir antes e, se faltar folga, reduzir o limite ou encurtar a janela de sobreposicao em vez de ampliar a VPS.
- A sonda de 1 s so tem valor em deploy real; em ambiente de teste ela precisa ser injetavel, o que pode mascarar falhas se o caminho real nao for exercitado ao menos uma vez com evidencia gravada.
- Pull autenticado da imagem privada no GHCR ja falhou antes (USR-363); credencial expirada derruba o deploy e cai no fallback de build na VPS, que e justamente o que a demanda quer eliminar.
- Desligar o autoDeploy 'NEW CHANGES' depende de estado no Dokploy fora do repo; se for religado manualmente, o deploy duplo volta sem aviso - por isso a verificacao por API precisa ser parte do fluxo.
- Guarda de 'nenhum fetch cru' pode gerar falso positivo em string ou comentario; ancorar a regra em chamada real e permitir excecao unica em hub_net.js.
