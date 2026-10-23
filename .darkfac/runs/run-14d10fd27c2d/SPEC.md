# SPEC

## Objetivo
Fechar os residuos da parte 2 do USR-207 (USR-339) que ainda vivem no repositorio: garantir que todo merge que dispara deploy do Hub tenha imagem GHCR publicada para aquele sha (invariante travado por teste), unificar a espera da tag GHCR num unico caminho que recusa rapido e com mensagem acionavel quando nao da para verificar a tag, e colocar docs/proposals/USR-207-zero-downtime.md no estado real entregue com o comando de agendamento da sonda protegido por teste. O codigo de GHCR (DARKHUB_IMAGE_SOURCE), remocao de container_name, start-first e blue-green ja foi entregue em #605 e so e completado/validado aqui.

## Fora de escopo
- Editar .github/workflows/hub-image.yml ou qualquer arquivo sob .github/workflows (caminho protegido por core/orchestrator/guard.py; mudanca exige acao do owner)
- Rollback do blue-green em falha de compose up e na migracao legada, e pull da imagem privada do GHCR na VPS (registryId/docker login) - escopo do USR-363 ja na fila
- Frescor do ledger .factory no Hub apos merge so de dados - escopo do USR-348 ja na fila
- Prova ao vivo com sonda de 1 s durante deploy real (producao, depende do USR-363 e de acao do owner; OA-058 segue aberta)
- Migrar o compose Darkhub para Stack/Swarm (owner escolheu blue-green no Traefik em OA-057)
- Aumentar VPS ou o limite de 512 MB do container sem medicao

## Desenho
Tres frentes pequenas, todas em arquivos nao protegidos.

1. Coerencia imagem<->deploy. core/infra/deploy_classifier.py decide se um merge redeploya o Hub; .github/workflows/hub-image.yml decide se o merge publica ghcr.io/gcamposgit/darkhub:<sha>. Hoje os conjuntos divergem: run_hub.py esta em REBUILD_FILES mas nao e COPY do deploy/dokploy/Dockerfile.hub nem path do workflow, logo um merge so de run_hub.py pede deploy, a imagem nunca e publicada e o redeploy em modo ghcr fica 900 s em wait_for_ghcr_tag e termina recusando. Correcao: tirar run_hub.py de REBUILD_FILES (a verdade do que a imagem embute e o COPY do Dockerfile.hub) e travar a invariante com teste estatico que le o workflow e o Dockerfile: todo prefixo/arquivo de rebuild do classificador precisa ser COPY da imagem e estar coberto pelo gatilho do workflow. A direcao inversa (workflow dispara tambem para docs/** e .factory/**, que o classificador trata como dados) fica deliberadamente como superconjunto e documentada: garante imagem existente para o sha sem gerar segundo deploy.

2. Uma unica espera de tag GHCR. Hoje existem duas implementacoes quase iguais (core/orchestrator/compose_render.wait_for_ghcr_image, usada pelo DokployDeploymentAdapter e pelo blue-green, e scripts/dokploy_redeploy.wait_for_ghcr_tag/_default_ghcr_probe). O CLI passa a ser casca fina sobre a funcao compartilhada, preservando timeout de 900 s, injecao de probe e traducao para DokployUsageError (codigo de saida inalterado). A sonda ganha semantica de erro: sem GHCR_TOKEN/GITHUB_PAT configurado e resposta 401/403, recusa imediata citando GHCR_TOKEN com read:packages (OA-053) em vez de 10-15 min de poll cego; com token configurado, 401/403/404 seguem em poll e o ultimo status HTTP entra na mensagem de timeout. Nenhuma chamada de escrita ao Dokploy antes da imagem existir, sem fallback para build.

3. Documento no estado real. Secoes 2.1/2.2/2.3 da proposta ainda estao escritas como 'Proposto' apesar de OA-044/052/053/057 cumpridas e do codigo em main; reescrever para o que esta em producao, registrar o resultado da prova de 23/10 (compose up em error por pull da imagem privada) e apontar o que resta para USR-363 e OA-058. A secao 1 (agendamento da sonda) ja usa Register-ScheduledTask com pythonw.exe absoluto, mas nada guarda isso: adicionar teste estatico de colagem segura no PowerShell.

## Arquivos a tocar
- core/infra/deploy_classifier.py
- core/orchestrator/compose_render.py
- scripts/dokploy_redeploy.py
- docs/proposals/USR-207-zero-downtime.md
- tests/test_hub_deploy_usr207.py
- tests/test_compose_render.py
- tests/test_dokploy_redeploy.py

## Riscos
- Tirar run_hub.py de REBUILD_FILES muda a decisao de deploy: conferir que run_hub.py realmente nao e COPY do Dockerfile.hub (hoje nao e) para nao deixar de implantar codigo embutido.
- O teste que le .github/workflows/hub-image.yml nao pode editar o arquivo protegido; se a invariante quebrar no futuro, a mensagem do teste deve mandar abrir acao do owner, nao sugerir editar o workflow.
- Recusar rapido em 401/403 sem token pode atrapalhar a primeira publicacao de um pacote inexistente; por isso a recusa rapida vale somente quando nenhum token esta configurado.
- Unificar as duas esperas pode mudar timeout/tipo de excecao visto pelo CLI; manter 900 s, a injecao de probe e o mapeamento para DokployUsageError, cobertos por teste.
- O critério de aceite da sonda de 1 s em deploy real nao fecha neste ticket (producao + USR-363); o relatorio final deve declarar isso explicitamente em vez de simular a prova.
