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

A aprovação por toque no Telegram (S3) automatizará esses passos.
