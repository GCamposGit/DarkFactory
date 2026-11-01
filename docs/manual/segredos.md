<!-- covers: scripts/bootstrap_secrets.py, core/line/pilot_secrets.py -->
# Segredos: cofre SOPS + age (passo a passo, Windows)

1. Instale as ferramentas no PowerShell: `winget install FiloSottile.age` e `winget install Mozilla.SOPS`. Feche e reabra o PowerShell.
2. Gere sua chave: `New-Item -ItemType Directory -Force $env:APPDATA\sops\age; age-keygen -o $env:APPDATA\sops\age\keys.txt`. O comando imprime `Public key: age1...`; copie esse valor.
3. Aponte o SOPS para a chave (uma vez por maquina): `setx SOPS_AGE_KEY_FILE "$env:APPDATA\sops\age\keys.txt"` e reabra o PowerShell.
4. Abra `C:\dev\DarkFac\.sops.yaml` e coloque a chave publica em `age: "age1..."` (varias maquinas: separe por virgula).
5. Cadastre os segredos: `python C:\dev\DarkFac\scripts\bootstrap_secrets.py`. Ele pergunta, sem eco, so as chaves que faltam (lista em `.factory/config/secrets_required.json`) e grava `secrets\factory.enc.yaml` cifrado.
6. Confira: `python C:\dev\DarkFac\scripts\bootstrap_secrets.py --check` (codigo 0 = tudo cadastrado).
7. Em outra maquina: instale as ferramentas, gere a chave dela, adicione a chave publica ao `.sops.yaml` e rode `sops updatekeys C:\dev\DarkFac\secrets\factory.enc.yaml` numa maquina que ja le o cofre.

Nunca copie `keys.txt` para o repositorio. Agentes de codigo nao recebem `SOPS_*` nem `AGE_*`; so os processos da fabrica (broker, Hub, despachante) leem o cofre via `core.infra.secrets_vault.load_into_env()`.

## Ambiente dos agentes (allowlist, USR-343 S6)

O processo de um agente de desenvolvimento (Claude, Codex, Grok) e o de verificacao do ticket NAO herdam o ambiente do host. `core.line.agent_env.build_agent_env` monta o ambiente por **allowlist**, definida em `C:\dev\DarkFac\.factory\config\agent_env_policy.json`:

1. `keep_names`: autenticacao dos harnesses (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, OPENAI_API_KEY, OPENROUTER_API_KEY, XAI_API_KEY, GROK_API_KEY, GEMINI_API_KEY, GOOGLE_API_KEY) e diretorios de config (GROK_HOME, CODEX_HOME, CLAUDE_CONFIG_DIR).
2. `allow_names` e `allow_patterns`: conjunto minimo do sistema (PATH, PATHEXT, SYSTEMROOT, TEMP, TMP, HOME, USERPROFILE, APPDATA, LOCALAPPDATA, PROGRAMDATA, PROGRAMFILES*, PROCESSOR_*, LANG, LC_*, PYTHON* exceto PYTHONSTARTUP, VIRTUAL_ENV, NODE_OPTIONS, npm_config_*, proxies/CA) e flags `DARKFAC_*` nao secretas.
3. Capacidades do ticket (`capabilities`): nomes explicitos liberados para aquele ticket.

Existe tambem `FAKE_CLI_RESPONSE_*` (CLIs falsos dos testes). Qualquer outra variavel (GITHUB_TOKEN, DOKPLOY_*, TELEGRAM_*, qualquer `*_SECRET`, variaveis desconhecidas) e descartada. Padroes de negacao (`deny_names`, `deny_patterns`, incluindo `DARKFAC_*TOKEN/KEY/SECRET/PASSWORD`) vencem as allowlists; `SOPS_*`, `AGE_*` e `GH_APP_*` nunca passam, nem por capacidade. Um `env` explicito passado a `run_shell_commands` (ambiente do produto) continua sem filtro. Os nomes descartados so aparecem em log nivel debug (nunca valores).

Se um CLI de harness quebrar por falta de variavel, acrescente o NOME em `allow_names` (ou um padrao em `allow_patterns`) no JSON acima, via PR.

## Segredos de piloto: mansaodolago.com (USR-279 T4)

O piloto do gestor de SEO e redes sociais da mansaodolago.com usa tres chaves no mesmo cofre, com
prefixo `MANSAODOLAGO_` em `.factory/config/secrets_required.json`:

| Chave | Para que serve | Fase que usa |
| --- | --- | --- |
| `MANSAODOLAGO_SEARCH_CONSOLE_CREDENTIALS_JSON` | Credencial (JSON) de leitura do Search Console de www.mansaodolago.com, para a auditoria de SEO | M1 |
| `MANSAODOLAGO_INSTAGRAM_GRAPH_ACCESS_TOKEN` | Token de acesso da Graph API para publicar posts aprovados pelo owner | M2 (adiado: a empresa ainda nao tem Instagram/Facebook, ver `docs/FASE_ATUAL.md`) |
| `MANSAODOLAGO_GOOGLE_ADS_REFRESH_TOKEN` | Refresh token OAuth para leitura/recomendacoes do Google Ads (sem developer token, Google dispensou a exigencia em 27/10) | M3 |

Cadastro pelo mesmo fluxo acima (`bootstrap_secrets.py` pergunta so as que faltam). Prontidao do
piloto, sem nunca expor valor: `core.line.pilot_secrets.mansaodolago_readiness()` filtra
`core.infra.secrets_vault.required_keys()` pelo prefixo e devolve presente/ausente por chave.

```powershell
python -m core.line.pilot_secrets
```

Imprime `presente`/`ausente` por chave e sai `0` quando as tres estao cadastradas, `1` caso
contrario -- nunca imprime o valor. O codigo do piloto (quando implementado) so pode obter um
segredo via `core.line.pilot_secrets.require_pilot_secret(chave)`, que levanta
`PilotSecretMissingError` em vez de assumir um valor ou seguir com mock quando a chave falta.
