# Runbook: portao oficial em candidato imutavel (USR-175)

## Problema

Na verificacao de USR-134 (2026-10-08) o portao foi executado a mao no checkout compartilhado
(`C:\dev\DarkFac`). Durante a suite o `main` avancou de `0d71e59` para `72c40a4` (PR #214) e o harness
recusou, corretamente, emitir evidencia: `Candidate HEAD changed during harness execution`.

A recusa esta certa e **nao deve ser relaxada**: evidencia so vale se ligada ao SHA exato testado. A
causa era operacional: o candidato era "o HEAD do checkout compartilhado", que outras sessoes movem.

## Solucao

```powershell
python C:\dev\DarkFac\scripts\gate_isolated.py                      # candidato = HEAD atual
python C:\dev\DarkFac\scripts\gate_isolated.py --ref <sha|branch>   # candidato explicito
```

O script (`core/git/gate_isolation.py`):

1. resolve `--ref` para um SHA;
2. cria uma worktree **destacada** `.worktrees/gate-<sha12>-<timestamp>` (`git worktree add --detach`):
   nao existe branch que outra sessao possa mover;
3. roda `core/harness/runner.py --quick` **dessa worktree** (o harness usa a raiz da propria worktree;
   `DARKFAC_PROJECT_ROOT`, `DARKFAC_STATE_ROOT` e `DARKFAC_HARNESS_STATE_DIR` herdados sao removidos do
   ambiente para nao apontar de volta ao checkout compartilhado);
4. confere que o candidato continua intacto (HEAD == SHA fixado e arvore limpa);
5. remove a worktree (com a mesma limpeza robusta do `run_ticket`).

O checkout compartilhado nunca e alterado (nem HEAD, nem indice, nem arquivos), entao a atualizacao
concorrente de `main` nao muda o candidato nem invalida o resultado.

Apenas o que esta **commitado** no SHA e testado; mudancas nao commitadas do checkout atual ficam de fora
de proposito (e o que torna a evidencia reproduzivel). Para validar uma branch de ticket, commite antes.
`run_ticket.py` continua rodando o portao na worktree propria do ticket (USR-69); este script cobre o caso
manual/de sessao (verificacao pos-merge, revisao).

## Fail-closed preservado

| Situacao | Saida |
|---|---|
| Portao passa e candidato intacto | `[ISOLATED_GATE_PASS]`, exit 0 |
| Portao falha | `[ISOLATED_GATE_FAIL]`, exit = codigo do portao |
| Candidato mudou (HEAD != SHA) ou ficou sujo durante a execucao | `[ISOLATED_GATE_FAIL]`, exit 3, mesmo que o portao tenha retornado 0 |
| `--ref` invalido, `git worktree add` falhou | exit 2 |

A recusa interna do harness (`Candidate HEAD changed ...`) continua intacta em `core/harness/runner.py`.

## Observacoes

- Ao rodar na worktree isolada o cache de verdicts do harness (`.factory/`) e o da worktree e se perde ao
  remove-la; o custo e uma execucao completa por candidato.
- Nao rode o portao isolado e `pytest` em paralelo no mesmo host (SUITE_LOCK).
- Se a remocao falhar no Windows, a worktree vai para `.factory/backups/trash/` e e limpa no proximo
  `purge_trash` (ver `core/git/ticket_workspace.py`).
