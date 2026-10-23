# SPEC

## Objetivo
Fechar a parte de codigo de USR-207 secoes 2.1-2.2 (continuacao de USR-339, com OA-044 e OA-052 ja concluidas): a imagem do Hub passa a vir do GHCR fixada no SHA como padrao, a espera da tag protege tambem o DokployDeploymentAdapter (sem fallback para build na VPS), a troca usa start-first sem container_name fixo e passa a existir medicao externa de 1 s em /health durante o deploy real, com a proposta e as acoes do owner sincronizadas.

## Fora de escopo
- Aumentar a VPS ou o limite de 512 MB do container sem a medicao da secao 3 da proposta
- Editar .github/workflows/hub-image.yml ou qualquer caminho protegido pelo guard (workflow ja publicado via OA-052; mudanca exigiria autorizacao de governanca do owner)
- Executar o agendamento da sonda na maquina do owner (ja feito na OA-044) ou rodar deploy manual fora do ciclo autonomo
- Implementar a rota blue/green com dois servicos darkhub-blue/green em scripts/dokploy_redeploy.py (so sera necessaria se o Dokploy ficar em Compose simples; decisao do owner)
- Mover live.js (USR-208) para o helper hubNetFetch

## Desenho
Estado atual: core/orchestrator/compose_render.py já sabe renderizar as duas fontes (resolve_hub_image_source, DARKHUB_IMAGE_SOURCE), trocando build:/pull_policy: build/container_name por image: ghcr.io/gcamposgit/darkhub:<sha> + pull_policy: always + deploy.update_config.order: start-first; scripts/dokploy_redeploy.py já tem wait_for_ghcr_tag (HEAD no manifesto, GHCR_TOKEN/GITHUB_PAT, timeout 900 s, falha fechada). Faltam quatro costuras: (1) a espera da tag vive só no CLI — extrair para um modulo compartilhado (core/infra/ghcr.py) e usa-la no DokployDeploymentAdapter._bind_release_identity antes do compose.deploy, com codigo de erro proprio e sem voltar para build; o adapter também passa a resolver a fonte explicitamente em _render_compose_at_sha; (2) o padrao de DARKHUB_IMAGE_SOURCE vira 'ghcr' (a imagem do sha já existe no GHCR desde a OA-052), ficando 'build' como escape hatch explicito; o compose em disco segue sendo a base com build: (o renderer transforma) e os comentarios passam a apontar a fase 2 como entregue; (3) start-first só vale em Stack/Swarm: o CLI e o adapter passam a ler o composeType do serviço no Dokploy e, em modo ghcr com Compose simples, avisam/registram OA em vez de prometer troca sem queda; (4) a evidencia do aceite exige medicao externa de 1 s — novo core/infra/switchover_probe.py (amostra /health a cada 1 s, exige 100% de 200 e convergencia do git_sha esperado, grava JSON de evidencia) chamado pelo redeploy do Hub quando --wait, com http/clock injetaveis para teste. A proposta docs/proposals/USR-207-zero-downtime.md é atualizada (secoes 1, 2.1, 2.1b, 2.2) e ganha teste estatico do snippet PowerShell (Register-ScheduledTask + pythonw.exe absoluto, sem schtasks com aspas quebradas).

## Arquivos a tocar
- core/infra/ghcr.py
- core/infra/switchover_probe.py
- core/orchestrator/compose_render.py
- core/orchestrator/deployment_adapter.py
- scripts/dokploy_redeploy.py
- deploy/dokploy/docker-compose.hub.yml
- docs/proposals/USR-207-zero-downtime.md
- tests/test_dokploy_redeploy.py
- tests/test_compose_render.py
- tests/test_deployment_adapter.py
- tests/test_hub_deploy_usr207.py
- .factory/owner_actions/owner_actions.json

## Riscos
- Flipar o padrao para ghcr quebra qualquer deploy cujo sha nao tenha imagem publicada (merge em caminho fora do filtro do workflow ou workflow vermelho): mitigar com falha fechada explicita e mensagem que ensina DARKHUB_IMAGE_SOURCE=build como rollback manual
- O pacote GHCR e privado: sem GHCR_TOKEN/GITHUB_PAT com read:packages no ambiente do runner a espera da tag falha em 401/403 — tratar 401/403 como erro de credencial distinto de 404 e nunca logar o token
- Se o Dokploy estiver em Compose simples o update_config e ignorado e a sonda vera queda mesmo com o codigo correto; o aceite depende da rota 2.2 escolhida pelo owner
- Remover container_name muda o nome do container na VPS: scripts/consultas que usam darkhub-cloud-247 (ex. docker stats/inspect da secao 3) e resolve_host_node_id dependem do hostname, que permanece
- A medicao de 1 s precisa rodar de fora da VPS durante um deploy real; em ambiente sem rede o teste cobre apenas o amostrador com http injetado e a evidencia real fica registrada no ticket/OA
