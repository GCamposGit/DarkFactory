# HF-03-08 — resultado do preflight de ativação

## Continuação após autorização de escopo (20/09/2026)

O `main` remoto foi reconfirmado em `e3b93cc90fc6368b3f23792ad4b6d3db2f1a543a` e a branch isolada foi atualizada sobre ele. O candidato local removeu o fallback `deterministic_mock` do worker e a execução padrão sintética do registry. Sem executor e verificador de evidência vinculados, o worker não toma jobs; o `--status` devolve código não-pronto enquanto os 13 estágios não estiverem vinculados. Resultados `success` sem verificação são rebaixados para `failed`; o lease recebe heartbeat durante a execução e a finalização usa o horário real. O coordenador exige Bearer token de 32+ caracteres nas rotas de intake, status detalhado e runs; sem configuração retorna 503 e sem credencial válida retorna 401. O intake fica desligado por padrão, limita o projeto a `darkfac` e não expõe runs de outros projetos. `/healthz` é apenas liveness; `/readyz` autenticado verifica banco e chave de intake. O Compose exige digest de imagem e restringe a porta 8001 ao loopback do host. As contraprovas focais passaram; a suíte completa e o harness devem ser repetidos sobre o SHA final.

Essas mudanças são contenção e preparação, não a fatia vertical aceita. Ainda não há executores produtivos vinculados aos 13 estágios nem recibos externos de PR/merge, build/deploy, jornada persistida, PID/heartbeat, digest instalado, restart e rollback. Não foi feito deploy ou novo intake; o alvo consultado permanece na versão anterior. O valor do digest imutável, o segredo da API e o acesso operacional ao Dokploy não foram obtidos. `DARKFAC_INTAKE_ENABLED` deve continuar `false` até a cadeia real e seus oráculos estarem comprovados. `HF-15-02` permanece bloqueado.

A revisão adversarial independente retornou `changes_required`. Os achados locais sobre recibo inexistente, expiração do lease, healthcheck do worker, isolamento de projeto e exposição de erro foram corrigidos e cobertos por contraprovas. Permanecem abertos os achados que exigem build aprovado, digest instalado e observação externa da jornada; portanto não há aprovação de integração operacional.

Observado em 2026-09-20 16:13:44 UTC. Estado: `needs_replan`. A ativação operacional não foi executada e `HF-15-02` permanece bloqueado. O recibo estruturado está em `.factory/planning/continuous-autonomy/activation-receipts.json`.

## Baseline e observação do alvo

- Branch isolada: `codex/hf-03-08-activation`; SHA-base: `87d1424a9bbfe6a2cce92369a200838c8977b3e4` (`origin/main` observado nesta sessão).
- `GET http://178.105.73.168:8001/healthz` retornou HTTP 200, `database_status=ready`, `dbos_engine=active`, versão `v1`. Isso comprova a disponibilidade consultada do coordenador, não a execução funcional de cada etapa.
- `GET /api/v1/tasks/run-dde02a573f38` retornou `project_id=darkfac`, `mode=autonomous`, `status=completed`, `config_version=1.0` e `plan_digest=350c7667a90ac93dfc9f58bd6ea657ff67815339c368a595a5964dfa56e7624b`. O run informado pelo owner foi consultado somente para leitura; nenhum job novo foi enviado.
- Os recibos do próprio run indicam `provider:remote_codex` para `planning` e `development`, mas `provider:deterministic_mock` para `integration`, `build_deploy` e `target_journey`. Portanto, `status=completed` não satisfaz o oráculo de jornada real.

## Contraprova e causa técnica

`core/orchestrator/cloud_worker.py::dispatch_claimed_job` percorre as etapas pelo mesmo caminho genérico: grava `<stage>_deliverable.json`, verifica o hash desse arquivo e finaliza com `StageResult(outcome="success")`. Apenas `planning` e `development` tentam um provedor remoto; as demais etapas recebem `deterministic_mock`. O caminho ativo não chama os handlers específicos já existentes para integração, release ou jornada. Além disso, `core/workflow/handlers.py::DefaultStageHandler._default_execute` também pode produzir `success` com referências sintéticas se um serviço não estiver vinculado. É necessário fechar ambos os caminhos de sucesso sem efeito real.

O manifesto `deploy/dokploy/docker-compose.cloud.yml` usa o contexto Git `#main` e a tag `:latest` para coordinator e worker. Esses identificadores são mutáveis e não atendem à exigência de imagem fixada do handoff. O endpoint `/health` citado no binding retornou 404 nesta sessão; o coordenador implementa `/healthz`. A resposta 404, isoladamente, não indica que o serviço está fora do ar.

A consulta ao status do run funcionou pela porta pública 8001 sem credencial. No candidato local, `CloudCoordinator.create_app` declara `POST /api/v1/tasks` e `GET /api/v1/tasks/{run_id}` sem dependência de autenticação nessa camada. O envio de tarefas não foi testado, pois criaria um job. A fronteira de acesso deve ser verificada e corrigida antes de permitir novos intakes autônomos no alvo.

O comando focal `python .factory/planning/continuous-autonomy/verify.py --binding HF-03-08` passou apenas na checagem de existência dos dois outputs. O verificador do pacote completo falhou com `integrity mismatch`: ele inclui todo `docs/handoffs/continuous-autonomy/*.md`, portanto este novo resultado altera o conjunto assinado em `integrity.json`. O manifesto é somente leitura no escopo atual. Esse conflito também precisa de replanejamento; não cabe editar o verificador ou o manifesto por este ticket.

Não foram observados externamente PID, heartbeat do worker, operação remota de deploy, digest da imagem instalada, escrita/leitura persistida da jornada ou rollback controlado. A tentativa SSH somente leitura falhou por autenticação (`Permission denied`). Nenhum restart, deploy, rollback, novo intake ou alteração em outro projeto foi realizado.

## Replanejamento técnico necessário

O handoff atual permite escrever apenas este resultado e `activation-receipts.json`; `core/`, `deploy/`, testes e o verificador são somente leitura. A lacuna exige retorno ao planejador high, preservando os oráculos atuais. Proposta de fatiamento para validação do planejador:

1. Vincular o worker cloud ao registry de handlers reais por estágio, versão e política; ausência de binding deve falhar sem materializar sucessor. Incluir contraprovas de `integration`, `build_deploy` e `target_journey` com serviço ausente.
2. Fixar build e imagem por SHA/digest, conferir no alvo o digest instalado e obter recibos da operação externa. Alinhar o probe de saúde ao endpoint efetivo sem usar HTTP 200 como único critério.
3. Executar canário em namespace autorizado com observador independente: um único intake público até PR/merge, build, deploy, jornada persistida e memória; em seguida restart e falha controlada para observar recuperação e rollback. Registrar PID, heartbeat, job, SHA, digest, operação e dados lidos do alvo.
4. Atualizar o contrato de integridade do pacote para admitir os dois outputs obrigatórios, preservando a detecção de drift dos artefatos de planejamento.
5. Vincular as rotas de intake/status do coordenador a autenticação e rede autorizadas, com teste externo de negação sem credencial e aceite somente da identidade operacional prevista.

Cada unidade nova precisa de `allowed_paths`, testes focais, ambiente, ownership e aprovação vinculados à baseline. O resultado deste preflight não libera `HF-15-02` nem declara `HF-03-08` entregue.
