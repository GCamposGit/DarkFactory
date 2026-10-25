# Governança: guard enxuto e autorização assinada (USR-343 S1)

## O que o guard protege

Só o que a fábrica não pode alterar sozinha:

- Documentos de governança: `MISSION.md`, `FACTORY_RULES.md`, `AGENTS.md`.
- Âncora de autoproteção: `core/orchestrator/guard.py`, `core/governance/*` e `.github/workflows/*`.

`core/harness/*`, `harness.config.json`, `.agents/rules/*`, holdout e testes de contrato deixaram de ser protegidos.

## Como o owner autoriza uma mudança protegida

Um registro JSON em `.factory/governance/authorizations/<id>.json`, assinado com Ed25519 e presente na base confiável (`origin/main`), vincula: `id`, `ticket_id`, `approver` (`owner`), `paths` exatos, `candidate_kind` (`commit` ou `tree`), `candidate` e `expires_at`. Registro sem assinatura válida é rejeitado. A chave pública vem de `.factory/governance/owner_pubkey`, também lida da base confiável. O guard roda só com stdlib e `cryptography` e falha fechado em qualquer dúvida.

## Primeira vez: gerar o par de chaves (uma vez)

1. Abra o PowerShell na máquina do owner.
2. Rode `cd C:\dev\DarkFac` e depois `python -m core.governance.keys init`.
3. A chave privada fica em `%USERPROFILE%\.darkfac\owner_ed25519.pem` (nunca no repositório; faça backup num cofre). O comando recusa sobrescrever uma privada existente.
4. A chave pública é gravada em `C:\dev\DarkFac\.factory\governance\owner_pubkey`. Faça o commit dela e leve a `main` (hoje o arquivo contém um placeholder, e enquanto ele existir nenhuma autorização é aceita).

## Assinar um registro

1. Crie o JSON sem o campo `signature` (modelo em `core/governance/authorization.py`).
2. `python -m core.governance.keys sign C:\caminho\registro.json`
3. Coloque o arquivo em `.factory/governance/authorizations/` e leve a `main`.

A aprovação por toque no Telegram (abaixo) automatiza esses passos.

## Aprovacao por toque

Quando um PR abre ou recebe novo commit tocando arquivos protegidos, o Hub avisa o owner no Telegram com numero do PR, caminhos protegidos, sha da cabeca e resumo do diff, mais os botoes **Aprovar** e **Rejeitar**.

1. O toque chega ao webhook do Hub (segredo do webhook verificado) e so vale se `from.id` for igual a `TELEGRAM_OWNER_CHAT_ID`; qualquer outro remetente e recusado e auditado.
2. O Hub (processo que nunca e agente) monta o registro de autorizacao (ticket, caminhos exatos, `candidate_kind=commit`, sha, validade de 24 h, aprovador `owner`), assina com a chave Ed25519 do owner e grava o JSON em `.factory/governance/authorizations/` na `main` pela API de contents, com o token do GitHub App.
3. O PR fica pendente e e mergeado (squash, fixado no sha aprovado) assim que os checks obrigatorios ficarem verdes. Novo commit no PR muda o sha: o registro deixa de valer e o merge pendente e descartado; um novo aviso e enviado.
4. Trilha de auditoria em `protected_publication/audit.jsonl` no diretorio de estado do Hub.

### Configuracao unica (Dokploy, servico do DarkHub)

1. Dokploy, projeto darkfac-cloud, servico darkhub, aba Environment.
2. `TELEGRAM_OWNER_CHAT_ID` = seu id numerico do Telegram (o do chat privado com o bot).
3. `OWNER_SIGNING_KEY` = conteudo PEM da chave privada gerada por `python -m core.governance.keys init` (rodado em `C:\dev\DarkFac`) (use `\n` no lugar das quebras de linha), ou coloque a chave no cofre SOPS com este nome. Nunca em ambiente de agente: a politica de ambiente de agentes bloqueia qualquer variavel com `SIGNING_KEY`.
4. `GH_APP_ID`, `GH_APP_INSTALLATION_ID`, `GH_APP_PRIVATE_KEY` (GitHub App com contents e pull requests em escrita) e, se o repositorio nao for `GCamposGit/DarkFactory`, `DARKFAC_GH_REPO`.
5. No GitHub, o webhook do repositorio deve enviar `pull_request`, `check_suite`, `check_run` e `status` para `/api/webhooks/github`.
6. Salve e faca redeploy: `python C:\dev\DarkFac\scripts\dokploy_redeploy.py`.
