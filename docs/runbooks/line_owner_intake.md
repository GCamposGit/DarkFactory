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
