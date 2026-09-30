# Runbook - Demanda do Owner entrando na linha autonoma (V1)

Criterio V1: uma demanda enviada pelo Owner via Telegram (ou DarkHub) vira
branch, PR, merge, deploy, smoke e relatorio no Telegram, sem passo manual.
Este runbook diz como disparar, o que precisa estar configurado e como o
projeto `darkfac` valida e implanta.

## 1. Como disparar

O codigo que faz a ponte e `core/line/owner_intake.py`: empurra um ticket de
`demands.json` pelo mesmo intake publico do canario
(`AutonomousIntakeService`), no Postgres que os workers leem. Cria um run cujo
primeiro job e o Grill (o bot pergunta no Telegram; voce responde; a linha segue).
E idempotente por ticket (`channel=owner`, `external_id=ticket:<id>`): enviar duas
vezes devolve o mesmo run.

| Acao | Como |
|---|---|
| Demanda nova por texto ou voz | No Telegram, mande `/demand <descricao>` (ou uma nota de voz). Cria o ticket `USR-NN` e, com a linha habilitada (secao 2), ja enfileira o run e responde "Linha autonoma: Ticket USR-NN enviado a linha (run run-...)". |
| Ticket que ja existe (ex.: USR-62) | No Telegram, mande `/linha USR-62`. Resposta: "Ticket USR-62 enviado a linha (run run-...); o Grill comeca em seguida." |
| Mesmo, pelo DarkHub | PowerShell: `Invoke-RestMethod -Method Post -Uri https://darkhub.ggcampos.com/api/demands/tickets/USR-62/line` (retorna `ok`, `run_id`). Se o Cloudflare Zero Trust exigir login, use o mesmo comando a partir de uma sessao ja autenticada ou o Telegram. |

Respostas de erro: ticket inexistente (`nao encontrado`, HTTP 404), projeto fora
de `DARKFAC_LINE_INTAKE_PROJECTS` (padrao so `darkfac`, HTTP 422), ticket
editado depois de enviado (HTTP 409: crie um ticket novo), banco fora do ar
(HTTP 503; nada e aceito em memoria).

## 2. O que precisa estar configurado (uma vez)

No Dokploy, compose do DarkHub (`wuH-sjZBig74xFGdk4IsL`), aba **Environment**:

1. `DARKHUB_LINE_DATABASE_URL` = `postgresql://USUARIO:SENHA@darkfaccore-postgresprimary-aebh67:5432/BANCO`
   (mesmo usuario/senha/banco do worker; precisa de permissao de INSERT, a role
   somente-leitura do painel nao serve). Se vazio, o Hub usa
   `DARKFAC_HF02_DATABASE_URL` e depois `DARKHUB_CONTROL_DATABASE_URL`.
2. `DARKFAC_LINE_AUTOSUBMIT` = vazio (habilitado quando ha URL), `true` ou
   `false` (desliga o envio automatico do `/demand`; `/linha` continua valendo).
3. `TELEGRAM_BOT_TOKEN`, `TELEGRAM_AUTHORIZED_USERS`, `TELEGRAM_AUTHORIZED_CHATS`
   e `TELEGRAM_WEBHOOK_SECRET` ja existentes (o webhook do Hub processa
   `/demand` e `/linha`).

Depois de mergear este PR, o Hub e o compose da linha precisam ser
reimplantados para carregar o codigo novo: o Hub (`wuH-...`) e o proprio alvo
de deploy da linha; o compose `darkfac-cloud` (`QJK0YXPQvCgjrpdgWH0uo`, worker,
canario e o novo `darkfac-pg-tailnet`) e reimplantado por fora da linha, pois
a imagem carrega `.factory/projects.json`.

## 3. Alvo de deploy do darkfac

`.factory/projects.json`, projeto `darkfac`: `deploy.type=dokploy`,
`service_name=wuH-sjZBig74xFGdk4IsL`, `service_type=compose` (o DarkHub,
`darkhub.ggcampos.com`); smoke `https://darkhub.ggcampos.com/` (HTTP 200).

Trava de seguranca: o estagio de release (`core/line/stage_release.py`) e o
adaptador Dokploy (deploy e rollback) recusam o id `QJK0YXPQvCgjrpdgWH0uo`
(compose `darkfac-cloud`, que roda o proprio worker: reimplanta-lo no meio de
um run reiniciaria o worker) com `cause_code=deploy_target_forbidden_self_restart`
(falha terminal, sem retry). Ids extras podem ser bloqueados com
`DARKFAC_LINE_FORBIDDEN_DEPLOY_IDS` (lista separada por virgula).

## 4. Validate do darkfac dentro do worker

O autodetector escolheria `python core/harness/runner.py --quick`, mas o
estagio de desenvolvimento valida a arvore suja, antes de commitar o ticket, e o
runner recusa arvore suja ("Candidate worktree is dirty"): toda iteracao
falharia e o run terminaria em `validate_exhausted`. Por isso o projeto declara
`commands` explicitos:

- `setup`: `python -c "import pytest, xdist, pydantic, fastapi, psutil" || python -m pip install -r requirements.txt`.
  As dependencias ja estao na imagem (`requirements.txt` + `dbos`, `psycopg`,
  `psutil`); o `pip install` so roda se faltar algo. Executa uma vez por worktree.
- `validate`: `python scripts/line_validate.py`. Em arvore limpa (o
  ValidationStage clona a branch enviada) roda o gate oficial
  (`runner.py --quick`, com cache de verdicts e despacho para o worker de testes
  do Desktop via `DARKFAC_TEST_WORKERS`, que o compose agora injeta, com fallback
  local). Em arvore suja (loop de desenvolvimento) roda os mesmos passos `quick`
  de `harness.config.json` diretamente, na VPS, sem duplicar comandos.

Orcamento de tempo: cada comando de validate tem teto de 1800 s
(`command_timeout_s`); os passos do harness somam 900 s (paralelo) + 300 s
(serial) + compilacao. A suite tem ~2000 testes; em arvore suja ela roda no CX23
(2 vCPU, `-n auto`), portanto e o trecho mais lento da linha. Se estourar o
teto, o run falha em `validate_exhausted` com o log do teste na branch; a saida
e reduzir o escopo do ticket ou subir o teto em
`core/line/stage_build.py` (`command_timeout_s`).

## 5. Dois bots, um papel cada (owner decision)

| Bot | Papel |
|---|---|
| `@darkfac_bot` (Owner, `TELEGRAM_OWNER_BOT_TOKEN`) | Somente ALERTAS (falhas do canario, resumo semanal, pedidos de acao humana). Nao trata comandos, demandas nem botoes: responde com um ponteiro para o bot de operacoes ou ignora texto solto. |
| `@darkfac_ops_bot` (Orchestrator, `TELEGRAM_OPS_BOT_TOKEN`) | Perguntas e respostas do Grill (botoes inline), `/demand`, voz, `/linha`, `/grill`, `/approve` e todos os callbacks. |

Rotas do Hub: `POST /api/webhooks/telegram/ops` e `POST /api/webhooks/telegram/owner`
(a rota antiga `/api/webhooks/telegram` continua valendo como ops). No startup, em
producao (`DARKHUB_ENV=production`), o Hub registra o webhook de cada bot na propria rota
via `setWebhook` (idempotente, com o `TELEGRAM_WEBHOOK_SECRET` existente; nenhum token e
logado). Desligar: `DARKHUB_TELEGRAM_AUTO_WEBHOOK=false`. URL base: `DARKHUB_PUBLIC_URL`
(padrao `https://darkhub.ggcampos.com`).

O worker envia as perguntas do Grill com o token do bot de operacoes quando
`TELEGRAM_OPS_BOT_TOKEN` esta definido (senao, com o do Owner). Variavel a adicionar no
Dokploy, ambiente do compose `darkfac-cloud`: `TELEGRAM_OPS_BOT_TOKEN` (mesmo valor que o
DarkHub ja recebe). O compose ja a repassa ao worker e ao canario.

Resposta de Grill pelo botao: o Hub grava a resposta no store da linha (Postgres,
tabela `grill_answers`; nao precisa de git) e acorda so o job de grill; o worker aplica a
resposta no proximo claim. O canario acorda sozinho o proprio grill em `waiting_human`.

## 6. Reenvio de ticket e diagnostico de falha de agente

- `/linha <ticket>` (e `POST /api/demands/tickets/<id>/line`): run em andamento e reapresentado
  (nunca duplicado); run entregue responde "ja foi entregue"; run terminado sem entregar abre
  uma nova tentativa `ticket:<id>:a<n>` (teto `DARKFAC_LINE_MAX_TICKET_ATTEMPTS`, padrao 5). A
  resposta informa a tentativa. A tentativa 1 mantem o id historico `ticket:<id>`.
- Falhas de agente ficam visiveis na branch `df/<run>`: `.darkfac/runs/<run>/validate-*.log.md`
  (desenvolvimento) e `agent-attempts-<grill|planning>.json`, cada um com harness, modelo, tipo de
  erro, duracao e os primeiros ~2000 caracteres (sem segredos). O worker registra um WARNING por
  estagio sem sucesso (cause_code + trecho de ~300 caracteres).
- Um harness que falha (crash) e excluido pelo resto das iteracoes do ticket e fica ~10 min em
  cooldown; JSON invalido no grill/planning e reexecutado em outro harness antes de ser terminal.
- `DARKFAC_CODEX_SANDBOX_MODE` (`auto` padrao, `danger-full-access`, `bypass`): o sandbox Linux do
  Codex nao sobe dentro do container, entao o compose do worker usa `bypass`
  (`--dangerously-bypass-approvals-and-sandbox`, valido no codex-cli 0.48.0); Desktop e Notebook
  nao definem a variavel e mantem o sandbox. O Claude Code roda como usuario nao-root (uid 1000),
  requisito do `--permission-mode bypassPermissions`.
