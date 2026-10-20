# Sessoes interativas no registro de execucoes (USR-209)

Toda sessao aberta pelo owner (Claude Code, Codex, Grok Build, Antigravity) e os subagentes do Claude Code
aparecem em `/live` do DarkHub. O registro usa o CLI do USR-208 (`scripts/execution.py start|heartbeat|finish`).
Nenhum dos scripts abaixo bloqueia a sessao: erros sao engolidos, as chamadas tem prazo curto e a fila local e o
reenvio vem do registro (USR-208). Nada fora do repositorio e alterado (nenhum `~/.claude`).

## Claude Code (hooks versionados)

`.claude/settings.json` (chave `hooks`) chama `scripts/hooks/claude_execution_hook.py` nos eventos
`SessionStart`, `SessionEnd`, `SubagentStart` e `SubagentStop`. O hook so dispara um processo destacado e sai com
codigo 0 na hora; esse processo registra o inicio (source `claude_session` ou `subagent`, subagente com
`--parent` da sessao), envia heartbeat a cada 30 s e encerra a execucao quando o evento de fim chega
(`SessionEnd` com motivo `logout`/`other` vira `cancelled`). O ticket vem de `DARKFAC_TICKET_ID` ou da branch
`ticket/usr-NNN`.

## Codex, Grok Build e Antigravity (wrapper)

Lance pelo wrapper; o codigo de saida do comando e preservado.

```powershell
python C:\dev\DarkFac\scripts\harness_session.py codex -- codex
python C:\dev\DarkFac\scripts\harness_session.py grok -- grok
python C:\dev\DarkFac\scripts\harness_session.py antigravity -- agy
```

Use `--ticket USR-NNN` antes do `--` para forcar o ticket. O wrapper registra o inicio, envia heartbeat em
thread e encerra com `succeeded`, `failed` (saida diferente de 0) ou `cancelled` (Ctrl+C). Se o executavel
tiver outro nome na sua maquina, troque apenas o que vem depois de `--`. Hooks nativos: o Codex e o Grok Build
nao tem hook de ciclo de sessao versionado neste repositorio; o wrapper e o caminho canonico. O Antigravity e
somente leitura para desenvolvimento, mas tambem entra no Hub pelo wrapper.

## Variaveis de teste e ajuste

- `DARKFAC_EXECUTION_CLI`: caminho de um CLI alternativo (usado nos testes com um CLI falso).
- `DARKFAC_EXEC_HOOK_HEARTBEAT_S`: intervalo do heartbeat (padrao 30).
- `DARKFAC_EXEC_HOOK_STATE_DIR`: pasta de marcadores do hook (padrao: temporario do sistema).

## Verificacao manual (em ate 10 s)

1. Abra `https://darkhub.ggcampos.com/live` em um navegador.
2. Em um terminal PowerShell, abra uma sessao por harness: `claude` dentro de `C:\dev\DarkFac`; e os tres
   comandos do wrapper acima.
3. Em ate 10 s cada sessao aparece como em andamento, com o harness e o ticket (quando a branch for
   `ticket/usr-NNN`). Peca ao Claude Code para lancar um subagente: ele aparece ligado a sessao.
4. Encerre a sessao (`/exit` ou Ctrl+C): a execucao sai de "em andamento" e fica concluida/cancelada.
5. Falha de rede: com o Hub inacessivel a sessao abre normalmente; o evento fica na fila local do registro e e
   reenviado depois.
