# SPEC

## Objetivo
Fechar a parte de codigo das secoes 2.1-2.2 de docs/proposals/USR-207-zero-downtime.md sobre o que USR-339 ja entregou: (a) a espera pela tag GHCR do SHA deixa de ser exclusiva do CLI e passa a proteger tambem o DokployDeploymentAdapter da linha, sempre fail-closed e sem voltar para build na VPS; (b) o gatilho da imagem e a classificacao de deploy passam a ter uma unica definicao de 'runtime do Hub', para que um merge gere no maximo uma imagem e um deploy; (c) o modo real do compose no Dokploy (Stack/Swarm vs docker-compose) passa a ser lido e reportado antes de prometer troca sem queda, porque update_config/start-first e inerte em compose simples; (d) existe ferramenta que amostra /health a cada 1 s durante a troca real e produz a evidencia exigida pelo aceite; (e) a proposta e conferida/atualizada (agendamento da sonda em Register-ScheduledTask com pythonw.exe absoluto, estado das OA-044/052/053 e do modo ghcr ja ativo).

## Fora de escopo
- Aumentar a VPS, o limite de 512M ou qualquer reserva de recurso sem a medicao da secao 3 da proposta
- Implementar a rota 2 (blue/green darkhub-blue/darkhub-green com troca de rotulo Traefik) antes da decisao do owner sobre Stack vs blue/green; o ticket apenas registra a OA de decisao e enfileira o ticket de implementacao da rota escolhida
- Editar .github/workflows/hub-image.yml (caminho protegido pelo guard, core/orchestrator/guard.py); correcoes no workflow vao no patch em docs/proposals + OA para o owner publicar
- Mudar o Dockerfile.hub, o que a imagem copia, ou mexer no helper de fetch do frontend (hub/frontend/hub_net.js)
- Repetir configuracao de registry GHCR, autoDeploy ou agendamento da sonda (OA-044/052/053 ja concluidas)

## Desenho
Estado verificado no repositorio: .github/workflows/hub-image.yml ja esta no main (13f5d81f); core/orchestrator/compose_render.py ja tem resolve_hub_image_source/DARKHUB_IMAGE_SOURCE, troca build:->image: ghcr.io/gcamposgit/darkhub:<sha>, pull_policy: always, remocao de container_name e injecao de deploy.update_config.order=start-first; scripts/dokploy_redeploy.py ja tem wait_for_ghcr_tag + _default_ghcr_probe e testes (test_darkhub_ghcr_mode_waits_for_tag_then_installs_image_start_first). Faltam quatro lacunas concretas. 1) Espera compartilhada: core/orchestrator/deployment_adapter.py::_render_compose_at_sha chama render_darkhub_compose(source, sha) sem image_source e sem nenhuma espera; com DARKHUB_IMAGE_SOURCE=ghcr no ambiente da linha ele instala um compose apontando para uma tag possivelmente inexistente e dispara compose.deploy. A espera sai de scripts/dokploy_redeploy.py para core/orchestrator/compose_render.py (wait_for_ghcr_tag + GhcrManifestProbe + resolucao de token), o CLI reexporta para nao quebrar os testes atuais, e o adapter passa a esperar antes de qualquer compose.update, devolvendo um codigo de erro curto (release_identity_image_missing) sem fallback para build; 401/403 no manifesto deixa de consumir os 900 s e falha na hora com mensagem de credencial (incidente da OA-053). 2) Uma imagem por merge: hoje o gatilho do workflow (core, hub, scripts, deploy, docs, .factory, requirements.txt, harness.config.json) e core/infra/deploy_classifier.py (core/, hub/, scripts/, deploy/ + requirements.txt, harness.config.json, run_hub.py) divergem nas duas direcoes - merges so de docs/ledger constroem imagem (contra a decisao do owner 'somente merges que alteram o runtime do Hub') e run_hub.py, que o Dockerfile.hub nem copia, pede rebuild sem nunca gerar imagem. O classificador passa a ser a fonte unica (nova image_trigger_paths()), run_hub.py sai de REBUILD_FILES com comentario apontando o Dockerfile, o patch da proposta e regerado com o gatilho estreito e um teste estatico trava a consistencia entre classificador, patch e o workflow vivo. 3) Modo do compose: o payload de compose.one (ja lido pelo CLI e pelo adapter) traz composeType; em modo ghcr o CLI imprime o modo e marca a troca como zero-downtime capaz apenas em stack/swarm, registrando no resultado quando start-first e inerte - sem recusar o deploy (recusar deixaria o Hub sem via de atualizacao). A escolha entre rota 1 (Stack) e rota 2 (blue/green) vira OA de decisao + ticket enfileirado. 4) Evidencia: novo core/infra/deploy_downtime_watch.py amostra /health a cada 1 s (status HTTP + git_sha), e acionado pelo proprio dokploy_redeploy.py em volta do compose.deploy/espera do sha, grava JSON em core.paths.state_root()/hub_downtime/<sha>.json, imprime o resumo (amostras, nao-200, maior sequencia ruim, sha final) e marca a saida como evidencia falha se houver qualquer resposta diferente de 200. Toda logica nova e pura/injetavel (probe, clock, sleep, sampler) para rodar no portao sem rede e sem thread real.

## Arquivos a tocar
- core/orchestrator/compose_render.py
- core/orchestrator/deployment_adapter.py
- scripts/dokploy_redeploy.py
- core/infra/deploy_classifier.py
- core/infra/deploy_downtime_watch.py
- docs/proposals/USR-207-ghcr-build.patch
- docs/proposals/USR-207-zero-downtime.md
- tests/test_hub_deploy_usr207.py
- tests/test_compose_render.py
- tests/test_dokploy_redeploy.py
- tests/test_dokploy_release_identity.py
- .factory/owner_actions/owner_actions.json

## Riscos
- Mover wait_for_ghcr_tag para compose_render.py pode quebrar imports de scripts/dokploy_redeploy.py e dos testes existentes (tests/test_dokploy_redeploy.py usa mod.wait_for_ghcr_tag e o parametro ghcr_probe); manter reexport e assinatura iguais.
- Fazer o adapter esperar a tag introduz espera longa na linha; sem timeout e sleep injetaveis o portao fica lento ou pendurado.
- Remover run_hub.py de REBUILD_FILES muda comportamento de deploy: um merge que toca apenas run_hub.py deixa de redeployar (correto, nao esta na imagem) mas os testes parametrizados de test_needs_rebuild precisam ser atualizados junto.
- O workflow .github/workflows/hub-image.yml e protegido pelo guard; qualquer teste que exija igualdade exata com o gatilho estreito ficaria vermelho ate o owner publicar. O teste do workflow vivo deve verificar apenas cobertura (todo caminho de runtime esta no gatilho), e a igualdade exata so contra o patch em docs/proposals.
- composeType pode vir ausente ou com outro rotulo na API do Dokploy; tratar desconhecido como 'nao comprovadamente sem queda' sem recusar o deploy, para nao deixar o Hub sem via de atualizacao.
- A amostragem de 1 s roda em thread no CLI do redeploy: se a thread vazar ou lancar, pode travar o fim do deploy; usar daemon thread, join com timeout e nunca deixar a falha da medicao derrubar o deploy em si (apenas marcar a evidencia).
- O aceite 'sonda de 1 s nao ve resposta diferente de 200' so se comprova no deploy real pos-merge; em compose simples start-first e inerte e a medicao pode registrar queda - isso e evidencia valida, nao falha do portao, e alimenta a OA de decisao da rota.
