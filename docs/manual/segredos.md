<!-- covers: scripts/bootstrap_secrets.py, core/line/pilot_secrets.py -->
# Segredos: cofre SOPS + age (passo a passo, Windows)

1. Instale os pacotes atuais no PowerShell, informando os IDs exatos:
   ```powershell
   winget install --exact --id SecretsOPerationS.SOPS
   winget install --exact --id FiloSottile.age
   ```
   A instalação pelo WinGet e a resolução dos comandos pelo `PATH` são etapas separadas. Feche e reabra o PowerShell e confira a instalação:
   ```powershell
   winget list --exact --id SecretsOPerationS.SOPS
   winget list --exact --id FiloSottile.age
   Get-Command sops, age, age-keygen -ErrorAction SilentlyContinue | Select-Object Name, Source
   sops --version
   age --version
   age-keygen --version
   ```
   Os três comandos devem aparecer no `Get-Command` e cada comando `--version` deve imprimir a versão instalada. Essas verificações nao leem nem gravam credenciais ou arquivos de chave.
2. Se o WinGet listar os pacotes, mas `Get-Command` nao encontrar algum executavel ou os comandos de versao falharem, procure as instalacoes no diretorio de pacotes e acrescente os diretorios encontrados ao `PATH` desta sessao do PowerShell:
   ```powershell
   $wingetPackages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
   $executables = foreach ($package in @('SecretsOPerationS.SOPS_*', 'FiloSottile.age_*')) {
       Get-ChildItem $wingetPackages -Directory -Filter $package -ErrorAction SilentlyContinue |
           Get-ChildItem -Recurse -File -Include sops.exe, age.exe, age-keygen.exe -ErrorAction SilentlyContinue
   }
   $executables | Select-Object FullName
   foreach ($exe in $executables) {
       if ($env:Path -notlike "*$($exe.DirectoryName)*") {
           $env:Path = "$($exe.DirectoryName);$env:Path"
       }
   }
   Get-Command sops, age, age-keygen -ErrorAction SilentlyContinue | Select-Object Name, Source
   sops --version
   age --version
   age-keygen --version
   ```
   Esse ajuste vale somente para a sessao atual. Se nenhum executavel aparecer em `$executables`, confira `winget list` e reinstale o pacote ausente; se os arquivos aparecerem mas os comandos continuarem sem versao, use o caminho completo mostrado por `FullName` para confirmar qual instalacao esta sendo executada.
3. Gere sua chave somente depois de confirmar os comandos: `New-Item -ItemType Directory -Force "$env:APPDATA\sops\age"; age-keygen -o "$env:APPDATA\sops\age\keys.txt"`. O comando imprime `Public key: age1...`; copie esse valor.
4. Aponte o SOPS para a chave (uma vez por maquina): `setx SOPS_AGE_KEY_FILE "$env:APPDATA\sops\age\keys.txt"` e reabra o PowerShell.
5. Abra `C:\dev\DarkFac\.sops.yaml` e coloque a chave publica em `age: "age1..."` (varias maquinas: separe por virgula).
6. Cadastre os segredos: `python C:\dev\DarkFac\scripts\bootstrap_secrets.py`. Ele pergunta, sem eco, so as chaves que faltam (lista em `.factory/config/secrets_required.json`) e grava `secrets\factory.enc.yaml` cifrado.
7. Confira: `python C:\dev\DarkFac\scripts\bootstrap_secrets.py --check` (codigo 0 = tudo cadastrado).
8. Em outra maquina: instale as ferramentas, confirme que `sops`, `age` e `age-keygen` resolvem pelo `PATH`, gere a chave dela, adicione a chave publica ao `.sops.yaml` e rode `sops updatekeys C:\dev\DarkFac\secrets\factory.enc.yaml` numa maquina que ja le o cofre.

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
