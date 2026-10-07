# Runbook — Sessao Codex desktop com chamada de ferramenta sem resposta (USR-79)

Limite conhecido de falha externa: o problema e do aplicativo Codex desktop, nao da fabrica.

## 1. Sintoma

- Ao retomar uma tarefa interrompida (reinicio do app, queda, fechamento), o Codex registra
  `Custom tool call output is missing` (`codex_core::util`) e a tarefa interrompe de novo.
- No JSONL da sessao (`~/.codex/sessions/AAAA/MM/DD/rollout-*.jsonl`) existe um item
  `custom_tool_call` (ou `function_call`) com um `call_id` sem nenhum
  `custom_tool_call_output` (ou `function_call_output`) com o mesmo `call_id`.
- Caso de origem: tarefa `01a0f2bd-3bae-76e1-908e-53a84dab0bf9`, 30/09/2026, `call_id`
  `call_yv1A78hyGK206kEsHXbAohqh`.

## 2. Por que a fabrica nao consegue corrigir

- Os arquivos de sessao sao estado interno do aplicativo desktop; editar o JSONL e fragil, nao
  suportado e pode corromper a tarefa. A fabrica nunca os escreve.
- O caminho headless da fabrica (`codex exec`) cria sessoes proprias e nao le as sessoes do
  aplicativo desktop; portanto nao ha "retomada" a interceptar nesse caminho.
- A correcao definitiva depende de uma versao futura do Codex tratar a saida ausente.

## 3. Deteccao (somente leitura)

```powershell
python -m core.line.codex_session_probe C:\Users\<usuario>\.codex\sessions\<arquivo>.jsonl
```

Codigos de saida: `0` sem chamadas orfas, `2` com chamadas orfas, `1` arquivo ilegivel. O relatorio
lista cada `call_id` sem resposta e o ultimo comando confirmado.

No `run_ticket.py --resume-delivery`, se a variavel `DARKFAC_CODEX_SESSION_PATH` apontar para um
JSONL, a retomada apenas registra um aviso em log quando houver chamada orfa; nada e bloqueado.

## 4. Recuperacao segura

1. Nao edite nem apague o JSONL original.
2. Preserve a evidencia: copie o arquivo para fora de `~/.codex/sessions`
   (por exemplo `Copy-Item <jsonl> C:\dev\DarkFac\.factory\evidence\codex_orphan\`).
3. Rode o probe acima e anote o `call_id` orfao e o "ultimo comando confirmado".
4. Abra uma tarefa NOVA no Codex (ou `python C:\dev\DarkFac\run_ticket.py <TICKET_ID>`) informando:
   o objetivo, o estado atual da worktree (`git status`, `git log -3`, branch), o ultimo comando
   confirmado e que a chamada orfa pode ou nao ter sido executada (verifique o efeito colateral
   antes de repetir).
5. Nao tente retomar a tarefa quebrada: ela volta a interromper.
