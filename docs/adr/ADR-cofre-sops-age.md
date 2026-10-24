# ADR: cofre de segredos com SOPS + age (USR-343 S5)

Status: aceito.

## Contexto
Segredos de deploy, GitHub App, Telegram e worker estavam espalhados em variaveis de ambiente por maquina.

## Decisao
- Um arquivo cifrado `secrets/factory.enc.yaml` no repositorio, cifrado com SOPS e chaves age (`.sops.yaml` lista os destinatarios publicos).
- A chave privada age fica so em cada maquina da fabrica (`SOPS_AGE_KEY_FILE`), fora do repositorio.
- `scripts/bootstrap_secrets.py` carrega, confere (`--check`) e cadastra segredos faltantes. A lista obrigatoria vive em `.factory/config/secrets_required.json`.
- `core/infra/secrets_vault.py` exporta o cofre para o ambiente dos processos da fabrica (broker, Hub, despachante). E no-op sem `sops` ou sem arquivo.
- Agentes de codigo NAO leem o cofre: `agent_env_policy.json` nega `^SOPS_` e `^AGE_` no ambiente deles, e a decifragem ocorre so nos processos da fabrica.

## Consequencias
Um cadastro por segredo, valido em todas as maquinas. Perder a chave age exige recadastrar os segredos.
