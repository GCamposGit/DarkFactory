# Harness de operacao e Grok Build como desenvolvedor (USR-109)

Regra do owner (2026-10-01): se o usuario opera a fabrica por um harness especifico, esse harness e o desenvolvedor principal do estagio `development` sempre que a cota dele estiver acima do piso critico de 15%. Subagentes mais simples e testes no desktop continuam livres.

Implementacao: `core/line/operating_harness.py` (deteccao) e `core/line/routing.py::pick` (preferencia). Especificacao completa na skill `19-run-ticket`, secao 2b.

## Como o harness de operacao e descoberto

Precedencia: parametro `pick(operating_harness=...)` > variavel `DARKFAC_OPERATING_HARNESS` (`claude|codex|grok|antigravity`) > autodeteccao pelo ambiente do harness pai > nenhum. Sinais de mais de um harness ao mesmo tempo sao ambiguos e valem como nenhum (o roteador nunca chuta). Pump, worker e nuvem nao tem sinal e seguem o Dynamic Headroom de sempre.

| Harness | Variaveis de ambiente | Situacao |
|---|---|---|
| Claude Code | `CLAUDECODE=1`, `CLAUDE_CODE_ENTRYPOINT` (ex.: `claude-desktop`) | Verificado em sessao real em 2026-10-01 |
| Grok Build 1.0.46 | `GROK_AGENT`, `GROK_SESSION_ID` | Verificado em sessao real em 2026-10-01. `GROK_CLI`, que `core/harness/suite_lock.py` assume, NAO e exportado |
| Codex | `CODEX_SANDBOX`, `CODEX_THREAD_ID` | NAO confirmado (nenhuma sessao real do Codex foi inspecionada) |
| Antigravity | `ANTIGRAVITY_SESSION` | NAO confirmado |

Para Codex e Antigravity, declare o harness explicitamente (confiavel): `$env:DARKFAC_OPERATING_HARNESS = "codex"` (PowerShell) antes de rodar `python C:\dev\DarkFac\run_ticket.py <TICKET_ID>`. Quando uma sessao real de cada um mostrar o que exporta, corrija `AUTODETECT_ENV_SIGNALS` em `core/line/operating_harness.py`.

## Regra de eleicao (estagio `development`)

1. Filtros de sempre: `host_caps`, modo `write` declarado, modelo proibido, `exclude`, cooldown, cota desconhecida, cota <= 15%.
2. Se o harness de operacao sobreviveu, e eleito, antes do ranking por headroom e da preferencia por complexidade. Se nao esta na cascata de `development`, entra como candidato extra: so este passo pode elege-lo, ele nunca disputa o ranking por headroom.
3. Se nao sobreviveu (critico, cooldown, cota desconhecida, sem `write`), vale a regra de sempre. O piso de 15% nunca e relaxado (so `--force` ou pedido explicito do usuario).
4. E prioridade, nao trava: rate limit no meio do ticket exclui o harness no retry e o roteador volta ao maior headroom.
5. Planning, grill, review e integration nao mudam. A revisao fica em outra familia que o implementador (`other_family_than_development`, inclui Grok e Antigravity).

## Grok Build como agente de escrita

`core/line/agent_cli.py` declara `grok` com `read` e `write`; o modo de escrita roda `grok --prompt-file <arquivo> --cwd <worktree> --output-format json --permission-mode <modo>`. O Antigravity continua somente leitura e nunca e eleito para `development`.

Modo de permissao: padrao `auto`, medido em Grok Build 1.0.46 neste Windows em repositorios git temporarios: `acceptEdits` e `dontAsk` falham (a primeira edicao e cancelada, `stopReason: "cancelled"`, exit 0), `auto` e `bypassPermissions` funcionam. Override: `DARKFAC_GROK_PERMISSION_MODE` (`default|acceptEdits|auto|dontAsk|bypassPermissions|plan`, sem diferenciar maiusculas; invalido gera WARNING e usa `auto`). Um run que termina com `stopReason: "cancelled"` e tratado como falha, nunca como sucesso silencioso. Cancelamento de uma ferramenta comum continua `crash` (a politica de retry de sempre). Cancelamento de um editor em caminho de `PROTECTED_PATTERNS` (`core/orchestrator/guard.py`, por exemplo `core/harness/*`) e `protected_path` (USR-205): uma unica tentativa, a worktree fica preservada, nenhum outro harness recebe o mesmo escopo, e o `run_ticket` registra uma acao do owner pedindo a proposta em `docs/proposals`.

Teste de fumaca real (opt-in, nao roda no gate): `DARKFAC_LIVE_GROK_TEST=1 python -m pytest tests/line/test_grok_runner.py -k live -q`.

## Desvio do ticket USR-109

O ticket pedia `("grok", None)` na cascata de `development`. Isso foi substituido pelo candidato extra do harness de operacao: com Grok na cascata, o pump headless passaria a escolher Grok por maior headroom sem ele ter provado qualidade como desenvolvedor autonomo, o que nao foi pedido pelo owner.
