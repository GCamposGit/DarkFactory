# SPEC

## Objetivo
Fechar o USR-207 no que resta sob controle do repositorio: uma unica fonte de verdade para os caminhos que a imagem do Hub embute (classificador x Dockerfile x gatilho do workflow GHCR), uma unica espera da tag GHCR com recusa acionavel quando a imagem privada nao pode ser verificada, telemetria de memoria e OOMKilled do container darkhub para ajustar o limite com evidencia, e a documentacao/sonda externa descritas no estado realmente entregue. O ciclo start-first/blue-green (USR-339/OA-057), o helper hubNetFetch (hub/frontend/hub_net.js) e a sonda com alerta no Telegram ops (core/infra/availability_probe.py) ja existem e sao reutilizados, nao reescritos.

## Fora de escopo
- Trocar o Dokploy por outra plataforma de deploy ou migrar o compose para Swarm (OA-057 decidiu blue-green no Traefik).
- Aumentar a VPS ou subir o limite de 512 MB sem medicao registrada.
- Tornar a imagem GHCR publica: o owner decidiu manter o pacote privado com pull autenticado.
- Rollback de falha do compose up e a correcao do pull autenticado na VPS: escopo do USR-363 (planned) e da OA-059.
- Reescrever o helper hubNetFetch, o blue-green ou a sonda externa, que ja estao em main.
- Remover o container legado darkhub-cloud-247 (OA-058, acao do owner na VPS).

## Desenho
1) Caminhos embutidos: core/infra/deploy_classifier.py passa a declarar os caminhos de runtime como espelho explicito dos COPY de deploy/dokploy/Dockerfile.hub, e testes estaticos leem o Dockerfile e .github/workflows/hub-image.yml (somente leitura, caminho protegido pelo guard) para provar que todo caminho que pede rebuild esta coberto pelo gatilho do workflow e que merges so de ledger/docs nao pedem deploy. Quando o gatilho divergir, a mensagem do teste manda abrir acao do owner em .factory/owner_actions/owner_actions.json em vez de editar o workflow. 2) Espera da imagem: scripts/dokploy_redeploy.wait_for_ghcr_tag deixa de duplicar a logica e delega para core/orchestrator/compose_render.wait_for_ghcr_image, mantendo timeout, injecao de probe/sleep/clock e os codigos de saida do CLI; sem GHCR_TOKEN/GITHUB_PAT e com 401/403 a recusa e imediata, citando read:packages e a OA-053, sem nenhum POST ao Dokploy e sem fallback para build. 3) Memoria: core/infra/metrics.py ganha memoria usada/limite e contagem de OOMKilled por container a partir do que Dokploy/Docker ja retornam, o Hub expoe isso no painel de infraestrutura (hub/frontend/infra.js, que ja usa hubNetFetch) e um script de medicao sob carga registra evidencia em docs; o limite de 512 MB em deploy/dokploy/docker-compose.hub.yml so muda com a evidencia anexada. 4) Documentacao e sonda: docs/proposals/USR-207-zero-downtime.md descreve o estado entregue e o que ficou para USR-363/OA-058, e o bloco de agendamento da sonda continua colavel no PowerShell (Register-ScheduledTask, caminhos absolutos), travado por teste.

## Arquivos a tocar
- core/infra/deploy_classifier.py
- scripts/dokploy_redeploy.py
- core/orchestrator/compose_render.py
- core/infra/metrics.py
- hub/frontend/infra.js
- deploy/dokploy/docker-compose.hub.yml
- docs/proposals/USR-207-zero-downtime.md
- tests/test_hub_deploy_usr207.py
- tests/test_compose_render.py
- tests/test_dokploy_redeploy.py

## Riscos
- Mexer no classificador pode fazer um merge de codigo ser lido como 'somente dados' e deixar a producao em sha antigo: o fail-closed (diff vazio ou desconhecido => rebuild) tem de continuar coberto por teste.
- O gatilho do workflow vive em .github/workflows, caminho protegido: o alinhamento precisa sair por acao do owner, nunca por edicao silenciosa do agente.
- Unificar a espera da tag GHCR pode alterar codigos de saida do CLI usados pelo run_ticket/deploy; os testes existentes de dokploy_redeploy precisam continuar verdes.
- Telemetria de memoria depende de campos opcionais do Dokploy/Docker: ausencia de dado deve degradar para 'desconhecido' sem quebrar o painel nem a coleta.
- Reduzir ou subir o limite de 512 MB sem medicao real pode causar OOMKilled na troca blue-green (dois containers no ar): sem evidencia, o limite nao muda.
- Mensagens de erro de registry podem vazar token se forem impressas sem filtro.
