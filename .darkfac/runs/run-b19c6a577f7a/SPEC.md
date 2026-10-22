# SPEC

## Objetivo
Fechar a parte de codigo do USR-207 fase 2 (USR-339): a espera da tag GHCR tambem no DokployDeploymentAdapter da linha, verificacao fail-closed do modo Stack/Swarm antes de qualquer deploy em modo ghcr, sonda de 1 s com evidencia gravada durante a troca real, correcao do patch do workflow hub-image.yml (uma imagem por merge de runtime) e sincronia da proposta, incluindo o comando de agendamento colavel no PowerShell.

## Fora de escopo
- Aumentar a VPS ou o limite de 512 MB sem a medicao da secao 3 da proposta
- Commitar .github/workflows/hub-image.yml (caminho protegido em core/orchestrator/guard.py: publicacao e da OA-052, do owner)
- Trocar o compose fonte para image: fixo; a fonte continua com build: e o render alterna por DARKHUB_IMAGE_SOURCE (rollback imediato)
- Implementar a rota blue/green com dois servicos darkhub-blue/green em Compose simples (so verificar o modo e recusar)
- Repetir a configuracao da OA-044 (autoDeploy off, registry GHCR, agendamento da sonda)

## Desenho
O estado atual (PR #558) ja tem: resolve_hub_image_source/render_darkhub_compose com modo ghcr (image ghcr.io/gcamposgit/darkhub:<sha>, pull_policy: always, container_name removido, deploy.update_config.order=start-first + rollback_config) em core/orchestrator/compose_render.py, e wait_for_ghcr_tag no CLI scripts/dokploy_redeploy.py. Faltam quatro costuras. (1) Extrair a espera da tag para um modulo compartilhado (core/infra/ghcr.py) e consumi-la em core/orchestrator/deployment_adapter.py::_render_compose_at_sha, que hoje chama render_darkhub_compose(source, sha) e herdaria o modo ghcr do ambiente sem nunca esperar a imagem; sem tag publicada o adapter deve devolver codigo de erro (release_identity_ghcr_image_missing) antes de compose.update, sem fallback para build. scripts/dokploy_redeploy.py passa a reexportar o helper para nao quebrar os testes atuais. (2) Decisao tecnica do grill (start-first so vale em Stack): ler o tipo do compose no payload de compose.one (fetch_service_status/baseline_payload no CLI e compose.one no adapter) e, quando image_source==ghcr e o tipo nao for stack/swarm (inclusive ausente ou desconhecido), recusar antes de qualquer POST, com a remediacao explicita; manter a OA do owner com esse passo. Em modo build nada muda. (3) Evidencia do aceite: core/infra/availability_probe.py ganha um modo watch (amostra /health a cada 1 s por uma duracao/ate convergir, conta respostas != 200, maior janela consecutiva fora do ar e convergencia do git_sha) com CLI -m core.infra.availability_probe --watch e gravacao de JSON em .factory/metrics/hub_deploy_windows/<sha>.json; scripts/dokploy_redeploy.py liga o amostrador (injetavel, best-effort, nunca derruba o deploy) na janela Darkhub e imprime o resumo. (4) docs/proposals/USR-207-ghcr-build.patch tem paths de trigger com docs/** e .factory/**, o que contraria a decisao do owner (so merges que alteram o runtime do Hub) e o aceite de uma unica imagem por merge: alinhar os paths com core/infra/deploy_classifier.py (core/, hub/, scripts/, deploy/, requirements.txt, harness.config.json, run_hub.py), adicionar concurrency por sha e workflow_dispatch (primeira imagem da OA-052), com teste estatico lendo o patch (e o workflow publicado, se existir). A proposta e atualizada para o estado entregue e o snippet de agendamento fica colavel sem caminho de usuario fixo.

## Arquivos a tocar
- core/infra/ghcr.py (novo)
- core/orchestrator/deployment_adapter.py
- core/orchestrator/compose_render.py
- scripts/dokploy_redeploy.py
- core/infra/availability_probe.py
- docs/proposals/USR-207-ghcr-build.patch
- docs/proposals/USR-207-zero-downtime.md
- .factory/owner_actions/owner_actions.json
- tests/test_dokploy_redeploy.py
- tests/test_real_deployment_adapter.py
- tests/test_hub_deploy_usr207.py
- tests/test_compose_render.py

## Riscos
- O aceite 'sonda de 1 s sem resposta != 200 num deploy real' depende da OA-052 (owner publica hub-image.yml e existe a primeira imagem) e do compose em modo Stack; em modo build o recreate ainda derruba o container, logo a evidencia real fica registrada na OA e nao pode ser aceite de ticket
- Nome do campo de tipo do compose na API do Dokploy pode diferir de composeType: ler de forma defensiva e falhar fechado em valor ausente/desconhecido quando o modo for ghcr
- Recusar deploy quando ghcr esta ligado sem Stack pode bloquear o deploy do Hub; mitigacao: mensagem com as duas saidas (trocar para Stack no Dokploy ou remover DARKHUB_IMAGE_SOURCE) e modo build intocado
- Alterar paths do trigger pode deixar um merge de runtime sem imagem; o teste estatico amarra a lista ao contrato de deploy_classifier para evitar divergencia silenciosa
- O amostrador de 1 s roda no no que executa o redeploy (Notebook/Desktop, externo a VPS); se rodasse na VPS a medicao seria invalida - manter best-effort e sem thread na VPS
- Imagem GHCR privada: a sonda HEAD do manifesto precisa de GHCR_TOKEN/GITHUB_PAT com read:packages; sem token a espera deve falhar com mensagem clara e sem vazar segredo em log
