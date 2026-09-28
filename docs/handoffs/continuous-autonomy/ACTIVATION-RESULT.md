# HF-03-08 — fechamento de contenção e preparação (2026-09-28)

## Decisão e escopo atual

O plano HF-27 determina que HF-03-08 termine como contenção + preparação. A antiga tentativa de ativação vertical e o pedido de replanejamento de cinco unidades abaixo ficam substituídos por [HF-27-08](../production-line/HF-27-08.md) e [HF-27-10](../production-line/HF-27-10.md) (ambos já `implemented` em `main`). HF-15-02 fica arquivado como auditoria; seu ensaio de 24h não é dependência de entrega (ver [HF-15-02.md](HF-15-02.md)).

Neste fechamento (worktree `claude/hf-03-08-closeout`, branch nunca mesclado anterior `codex/hf-03-08-finalize` não reaproveitado — main avançou 71 commits desde a base observada por aquele branch): coordenador autenticado (Bearer token opcional, código falha fechado com 503 sem ele, nunca "sucesso" silencioso), intake opt-in e escopo de projeto limitado, porta 8001 do Compose em loopback, handler legado `DefaultStageHandler._default_execute` fail-closed. O worker de produção já usa o registry real de handlers e heartbeat de lease desde HF-27-08 (integrado antes desta sessão, PR #33).

**Diferença deliberada em relação ao pedido original de pin por digest de imagem:** este fechamento **não** adiciona `DARKFAC_CLOUD_IMAGE_DIGEST` ao Compose. Não existe pipeline de CI publicando imagens em GHCR neste repositório; o Dokploy builda a imagem direto do SHA do `main` a cada push (`build: context: .../DarkFactory.git#main`, `image: ...:latest`). Fixar por digest exigiria uma etapa de publicação inexistente e quebraria o deploy autônomo atual — fica como item residual deferido (ver `required_before_live_activation` em `activation-receipts.json`).

Não foi feito deploy, alteração remota de firewall, intake, restart, rollback nem acesso a segredos nesta sessão. A observação remota de 21/09 descrita abaixo é histórica, não confirma o estado do alvo em 28/09. O target segue operacionalmente não verificado; o bind de porta em loopback só entra em vigor quando o Compose for implantado pelo Dokploy.

## Evidência local desta revisão

Ver `.factory/planning/continuous-autonomy/activation-receipts.json`, bloco `closeout`, para os comandos exatos e o resultado literal do portão único (`core/harness/runner.py --quick`) e dos testes focais desta sessão, incluindo SHA dos commits e hash por arquivo alterado.

---

# HF-03-08 — resultado do preflight de ativação (arquivo histórico, 2026-09-20/24)

> O texto abaixo é o registro original do preflight de ativação, preservado como evidência histórica. Não descreve o estado do fechamento de 2026-09-28 acima; onde os dois divergem (em especial sobre pin de imagem por digest), a seção "Decisão e escopo atual" acima é a que vale.

## Continuação após autorização de escopo (20/09/2026)

O `main` remoto foi reconfirmado em `e3b93cc90fc6368b3f23792ad4b6d3db2f1a543a` e a branch isolada foi atualizada sobre ele. O candidato local removeu o fallback `deterministic_mock` do worker e a execução padrão sintética do registry. Sem executor e verificador de evidência vinculados, o worker não toma jobs; o `--status` devolve código não-pronto enquanto os 13 estágios não estiverem vinculados. Resultados `success` sem verificação são rebaixados para `failed`; o lease recebe heartbeat durante a execução e a finalização usa o horário real. O coordenador exige Bearer token de 32+ caracteres nas rotas de intake, status detalhado e runs; sem configuração retorna 503 e sem credencial válida retorna 401. O intake fica desligado por padrão, limita o projeto a `darkfac` e não expõe runs de outros projetos. `/healthz` é apenas liveness; `/readyz` autenticado verifica banco e chave de intake. O Compose exige digest de imagem e restringe a porta 8001 ao loopback do host. As contraprovas focais passaram; a suíte completa e o harness devem ser repetidos sobre o SHA final.

Essas mudanças são contenção e preparação, não a fatia vertical aceita. Ainda não há executores produtivos vinculados aos 13 estágios nem recibos externos de PR/merge, build/deploy, jornada persistida, PID/heartbeat, digest instalado, restart e rollback. Não foi feito deploy ou novo intake; o alvo consultado permanece na versão anterior. O valor do digest imutável, o segredo da API e o acesso operacional ao Dokploy não foram obtidos. `DARKFAC_INTAKE_ENABLED` deve continuar `false` até a cadeia real e seus oráculos estarem comprovados. `HF-15-02` permanece bloqueado.

## Contenção temporária do ingresso público (21/09/2026)

Após autorização explícita do Owner, o host foi observado com `iptables v1.8.10 (nf_tables)`, política `FORWARD DROP` e salto inicial para `DOCKER-USER`. Foram inseridas regras limitadas à interface pública `eth0` para descartar TCP destino 8001 em `DOCKER-USER` e `INPUT`, tanto em IPv4 quanto em IPv6. Depois das quatro regras, a consulta externa a `http://178.105.73.168:8001/healthz` expirou (`curl` status `000`), enquanto `https://dokploy.ggcampos.com/` permaneceu em HTTP 200. No próprio host, `http://127.0.0.1:8001/healthz` continuou retornando `database_status=ready` e `dbos_engine=active`.

Essa contenção reduz a exposição imediata, mas não é persistente após reboot e não aprova o candidato. A correção permanente permanece o bind de porta do Compose em `127.0.0.1`, junto da autenticação do coordenador, e só pode substituir as regras temporárias depois de deploy e contraprova externa.

A revisão adversarial independente retornou `changes_required`. Os achados locais sobre recibo inexistente, expiração do lease, healthcheck do worker, isolamento de projeto e exposição de erro foram corrigidos e cobertos por contraprovas. Permanecem abertos os achados que exigem build aprovado, digest instalado e observação externa da jornada; portanto não há aprovação de integração operacional.

Observado em 2026-09-20 16:13:44 UTC. Estado naquele momento: `needs_replan`. A ativação operacional não foi executada e `HF-15-02` permanece bloqueado. O recibo estruturado está em `.factory/planning/continuous-autonomy/activation-receipts.json`.

## Baseline e observação do alvo

- Branch isolada: `codex/hf-03-08-activation`; SHA-base: `87d1424a9bbfe6a2cce92369a200838c8977b3e4` (`origin/main` observado nesta sessão).
- `GET http://178.105.73.168:8001/healthz` retornou HTTP 200, `database_status=ready`, `dbos_engine=active`, versão `v1`. Isso comprova a disponibilidade consultada do coordenador, não a execução funcional de cada etapa.
- `GET /api/v1/tasks/run-dde02a573f38` retornou `project_id=darkfac`, `mode=autonomous`, `status=completed`, `config_version=1.0` e `plan_digest=350c7667a90ac93dfc9f58bd6ea657ff67815339c368a595a5964dfa56e7624b`. O run informado pelo owner foi consultado somente para leitura; nenhum job novo foi enviado.
- Os recibos do próprio run indicam `provider:remote_codex` para `planning` e `development`, mas `provider:deterministic_mock` para `integration`, `build_deploy` e `target_journey`. Portanto, `status=completed` não satisfaz o oráculo de jornada real.

## Contraprova e causa técnica

`core/orchestrator/cloud_worker.py::dispatch_claimed_job` percorria as etapas pelo mesmo caminho genérico: gravava `<stage>_deliverable.json`, verificava o hash desse arquivo e finalizava com `StageResult(outcome="success")`. Apenas `planning` e `development` tentavam um provedor remoto; as demais etapas recebiam `deterministic_mock`. Esse caminho já foi substituído pelo registry real de handlers em HF-27-08 (antes desta sessão). Além disso, `core/workflow/handlers.py::DefaultStageHandler._default_execute` também podia produzir `success` com referências sintéticas se um serviço não estivesse vinculado — esse segundo caminho de sucesso sem efeito real é o que este fechamento corrige (`outcome="failed"`, `cause_code="missing_stage_service"`).

O manifesto `deploy/dokploy/docker-compose.cloud.yml` usa o contexto Git `#main` e a tag `:latest` para coordinator e worker. Esses identificadores são mutáveis; o pedido original deste preflight era fixá-los por digest. Esse fechamento decidiu **não** seguir esse pedido (ver "Decisão e escopo atual" acima) porque não há pipeline de publicação de imagem para gerar um digest confiável a cada mudança. O endpoint `/health` citado no binding original retornou 404 nesta sessão; o coordenador implementa `/healthz`. A resposta 404, isoladamente, não indica que o serviço está fora do ar.

A consulta ao status do run funcionou pela porta pública 8001 sem credencial. No candidato local daquela sessão, `CloudCoordinator.create_app` declarava `POST /api/v1/tasks` e `GET /api/v1/tasks/{run_id}` sem dependência de autenticação nessa camada. Esse é exatamente o gap que este fechamento corrige (Bearer token obrigatório em todas as rotas exceto `/healthz`).

O comando focal `python .factory/planning/continuous-autonomy/verify.py --binding HF-03-08` passava apenas na checagem de existência dos dois outputs originais. O verificador do pacote completo (`verify.py` sem `--binding`) já falhava antes desta sessão por razões não relacionadas a HF-03-08 (ver nota em `HF-03-08.md`, seção "Validação e continuidade").

Não foram observados externamente PID, heartbeat do worker, operação remota de deploy, digest da imagem instalada, escrita/leitura persistida da jornada ou rollback controlado nesta sessão nem nas anteriores. A tentativa SSH somente leitura de 20/09 falhou por autenticação (`Permission denied`). Nenhum restart, deploy, rollback, novo intake ou alteração em outro projeto foi realizado em nenhuma das sessões, incluindo esta.

## Replanejamento técnico proposto originalmente (não reaberto)

O texto abaixo é o replanejamento de cinco unidades proposto no preflight original. O Owner decidiu explicitamente **não abrir** esse replan (ver "Decisão e escopo atual" acima); HF-27-08 e HF-27-10 cobrem funcionalmente os itens 1–3.

1. Vincular o worker cloud ao registry de handlers reais por estágio, versão e política; ausência de binding deve falhar sem materializar sucessor. Incluir contraprovas de `integration`, `build_deploy` e `target_journey` com serviço ausente. — **Feito por HF-27-08.**
2. Fixar build e imagem por SHA/digest, conferir no alvo o digest instalado e obter recibos da operação externa. — **Não feito; item residual deferido (sem pipeline de publicação de imagem).**
3. Executar canário em namespace autorizado com observador independente. — **Feito por HF-27-10** (canário contínuo, `core.line.canary`, loop em `darkfac-canary` no Compose).
4. Atualizar o contrato de integridade do pacote para admitir os dois outputs obrigatórios. — Feito neste fechamento (`integrity.json` regenerado).
5. Vincular as rotas de intake/status do coordenador a autenticação e rede autorizadas. — **Feito neste fechamento** (Bearer token + bind loopback).

O resultado deste preflight não liberou `HF-15-02` nem declarou `HF-03-08` entregue como fatia vertical operacional — isso continua verdade; HF-03-08 encerra como contenção + preparação, não como ativação produtiva.
