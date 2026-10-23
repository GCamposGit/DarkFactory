# SPEC

## Objetivo
Fechar a parte de codigo das secoes 2.1-2.2 do USR-207: tornar a imagem GHCR do SHA a fonte padrao da imagem do Hub, garantir que CLI e DokployDeploymentAdapter esperem a tag antes de qualquer mutacao no Dokploy (sem fallback para build na VPS), alinhar o gatilho do workflow protegido ao classificador de runtime, verificar o modo Stack exigido pelo start-first e entregar a sonda de 1 s que produz a evidencia de zero queda.

## Fora de escopo
- Aumentar a VPS ou os limites de deploy.resources sem a medicao descrita na secao 3 da proposta
- Refazer configuracao de owner ja concluida na OA-044/OA-052 (autoDeploy off, registry ghcr no Dokploy, sonda agendada, workflow publicado em main)
- Editar .github/workflows/hub-image.yml diretamente (caminho protegido por core/orchestrator/guard.py: a mudanca sai como patch em docs/proposals + OA para o owner)
- Rota 2.2.2 (blue/green com dois servicos darkhub-blue/green e troca de rotulos Traefik) enquanto a rota Stack/Swarm nao for descartada por medicao
- Trazer live.js para hubNetFetch (USR-208)

## Desenho
O USR-339 ja deixou render_darkhub_compose (core/orchestrator/compose_render.py) capaz de emitir 'image: ghcr.io/gcamposgit/darkhub:<sha>' + pull_policy: always, remover container_name e injetar deploy.update_config.order=start-first, e deixou wait_for_ghcr_tag em scripts/dokploy_redeploy.py. Faltam quatro costuras. (1) A espera da tag vive so no CLI: extrair probe+espera para um modulo compartilhado (core/orchestrator/ghcr_image.py) e chamar tambem em DokployDeploymentAdapter._bind_release_identity/_render_compose_at_sha, antes do compose.update, falhando fechado com codigo de erro proprio e nunca voltando para build. (2) resolve_hub_image_source ainda devolve 'build' por padrao, o que deixa a esteira (que nao herda a variavel de usuario do Windows) construindo na VPS: inverter o padrao para 'ghcr' e manter DARKHUB_IMAGE_SOURCE=build como escape hatch explicito. (3) O filtro de paths do workflow publicado (core/**, hub/**, scripts/**, deploy/**, docs/**, .factory/**, requirements.txt, harness.config.json) nao bate com core/infra/deploy_classifier.py (REBUILD_PREFIXES + REBUILD_FILES, que inclui run_hub.py e exclui docs/ e .factory/): um merge so de docs publica imagem sem deploy, e um merge so de run_hub.py precisa de deploy mas nunca gera imagem, levando o CLI a esperar 900 s e recusar. Como o arquivo e protegido, o codigo ganha uma checagem de cobertura (deploy_classifier.hub_image_workflow_globs + workflow_covers) que recusa em segundos com mensagem apontando o patch/OA, e a correcao do filtro sai como patch + owner action. (4) start-first so vale em Stack/Swarm: antes do deploy em modo ghcr, ler composeType via compose.one e avisar/recusar quando for compose simples, registrando OA para o owner trocar o compose Darkhub para Stack. A evidencia do aceite vem de um modo watch na sonda existente (core/infra/availability_probe.py), amostrando /health a cada 1 s durante a janela do deploy, persistindo relatorio (amostras, nao-200, maior lacuna, convergencia para o SHA esperado) e sendo acionado pelo proprio scripts/dokploy_redeploy.py em --only Darkhub.

## Arquivos a tocar
- core/orchestrator/compose_render.py
- core/orchestrator/ghcr_image.py
- core/orchestrator/deployment_adapter.py
- scripts/dokploy_redeploy.py
- core/infra/deploy_classifier.py
- core/infra/availability_probe.py
- docs/proposals/USR-207-zero-downtime.md
- docs/proposals/USR-207-hub-image-paths.patch
- tests/test_compose_render.py
- tests/test_dokploy_redeploy.py
- tests/test_hub_deploy_usr207.py
- tests/test_deployment_adapter_release_identity.py
- .factory/owner_actions/owner_actions.json

## Riscos
- Virar o padrao para ghcr sem a espera no adapter faz a esteira instalar um compose com pull_policy: always antes da imagem existir: o Hub fica sem container; por isso T1 (espera compartilhada) vem antes de T2 (troca do padrao)
- O filtro do workflow esta num caminho protegido: enquanto o owner nao publicar o patch, um merge so de run_hub.py nao gera imagem; a checagem de cobertura precisa recusar antes de mutar o Dokploy, nao depois do timeout
- Se o compose Darkhub nao estiver em modo Stack, update_config e ignorado e o criterio de 1 s sem resposta != 200 nao e alcancavel por codigo: a verificacao precisa ser explicita e a OA registrada, nao silenciosa
- Remover container_name muda a identidade do container na VPS; core.infra.metrics.resolve_host_node_id depende de hostname (USR-212, ja preservado no compose) e docker inspect por nome usado em runbooks/medicao da secao 3 passa a precisar do nome gerado
- A sonda de 1 s roda de fora da VPS (Notebook/Desktop) e nao deve virar um segundo disparo de deploy nem bloquear o fluxo quando a rede do operador oscilar
- O patch docs/proposals/USR-207-ghcr-build.patch ja foi aplicado em main (13f5d81f): mante-lo no repositorio convida reaplicacao; a retirada precisa seguir o precedente de USR-158
