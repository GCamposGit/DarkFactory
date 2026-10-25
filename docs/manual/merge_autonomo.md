<!-- covers: core/git/app_token.py, core/git/autonomy.py -->
# Merge autonomo com GitHub App (USR-343)

A fabrica faz push e merge com um token de curta duracao de uma GitHub App, sem a conta admin do owner.

## Variaveis (somente no processo executor da entrega, nunca no ambiente dos agentes de codigo)

| Variavel | Valor |
| --- | --- |
| `GH_APP_ID` | ID numerico da App (pagina da App, "App ID") |
| `GH_APP_INSTALLATION_ID` | numero final da URL de instalacao em Settings > Applications > Installed GitHub Apps |
| `GH_APP_PRIVATE_KEY` | conteudo do `.pem` gerado em "Generate a private key" (aceita `\n` literal) |

## Passo a passo (owner, uma vez)

1. GitHub > Settings > Developer settings > GitHub Apps > New GitHub App.
2. Nome `darkfac-factory`; Homepage qualquer URL; desmarque Webhook > Active.
3. Repository permissions: Contents = Read and write; Pull requests = Read and write; Metadata = Read-only.
4. "Where can this app be installed" = Only on this account. Create GitHub App.
5. Em "Private keys", clique Generate a private key e guarde o `.pem`.
6. Install App > escolha apenas o repositorio DarkFac.
7. Cadastre as tres variaveis no cofre de segredos da fabrica (processo executor).

## Comportamento

- `core/git/app_token.py` assina um JWT RS256 (9 min), troca por um token de instalacao restrito a
  `contents:write` e `pull_requests:write` e o guarda em cache ate 5 min antes de expirar.
- `core/git/autonomy.py` injeta o token como `GH_TOKEN` somente nos subprocessos `git push` e
  `gh pr create|merge`. Sem as tres variaveis (ou se a troca falhar) vale a autenticacao atual do `gh`.
- O padrao `^GH_APP_` em `.factory/config/agent_env_policy.json` mantem as variaveis fora dos agentes.
- Para o merge sem `--admin`, o ruleset do `main` deve listar a App como bypass actor (ou exigir apenas os checks).
