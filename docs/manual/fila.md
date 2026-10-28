<!-- covers: core/git/app_token.py, core/infra/node_sync.py, scripts/sync_usage_to_cloud.py, scripts/snapshot_queue.py -->
# Fila de tickets (USR-343)

A fila de desenvolvimento vive no control store: Postgres quando `DARKFAC_HF02_DATABASE_URL`
está definida, senão o SQLite local `.factory/control.db`. Tabelas (criadas com
`CREATE TABLE IF NOT EXISTS`):

- `tickets (project_id, ticket_id, status, payload, updated_at)`
- `owner_actions (action_id, status, payload, updated_at)`

## Registrar um ticket

```powershell
python C:\dev\DarkFac\run_ticket.py --create --queue-only --title "Titulo" --problem "Problema" --criteria "Criterio"
```

O comando grava o ticket (status `planned`) no control store e termina. Não cria worktree
`ticket/queue-*`, PR nem merge. O Telegram usa o mesmo caminho.

## Leitura

`core.demands.store.DemandsStore` (usado pelo despachante, Hub e `/tickets`) lê o control store
primeiro e complementa com `.factory/demands/demands.json` (fallback e histórico). Em conflito
de ID vale o control store, exceto quando o ledger já marca o ticket como `completed` ou
`cancelled`. Se o control store estiver inacessível, a leitura cai para o `demands.json`.
Com `DARKFAC_DEMANDS_PATH` definida, só o arquivo indicado é lido.

## Acoes do owner e status (S4b)

- Escritores de `owner_actions` (respostas do Hub via `answer_sync`, espelhos, alertas de CI, CLI)
  gravam no control store (`OwnerActionStore` no ledger padrao). Nao existem mais PRs `chore/oa-*`
  nem `ticket/queue-*`; a leitura mescla o control store sobre o `owner_actions.json`.
- `DemandsStore.save_ticket` e `update_status` gravam tambem a linha em `tickets` do control store.
- Snapshot diario: `python C:\dev\DarkFac\scripts\snapshot_queue.py` (a flag `--force` ignora o
  marcador do dia) escreve `demands.json` e `owner_actions.json` a partir do control store em UM
  commit, publicado em `main` com o token do GitHub App (`core/git/app_token.py`). E idempotente
  (marcador no diretorio de estado; ledger igual nao gera commit) e roda uma vez por dia dentro do
  laco de `scripts/sync_usage_to_cloud.py`. Le e escreve so numa worktree
  descartavel de `origin/main`: o checkout de quem chama nunca fica sujo (USR-414).
- `core/infra/node_sync.py` trata os dois ledgers como arquivos gerados: antes do pull do Desktop
  (`/system/update`) e na remediacao do Notebook, edicoes locais sao copiadas para
  `<estado>/ledger_backups/` e revertidas (`git checkout`), entao o update nao aborta mais (USR-374).

# Fila de demandas (entrada)

## Entrada unica

Toda demanda do owner (Telegram `/demand`, DarkHub, chat) entra por UMA funcao:
`core.line.owner_intake.submit_owner_demand(texto, source=..., demands_service=...)`.

1. Cria o ticket no armazenamento duravel (`DemandsStore`, `demands.json` no diretorio de estado
   compartilhado), nunca em arquivo efemero de container.
2. Registra o ticket na fila canonica. Quando `core.demands.queue_store.register_ticket`
   (USR-343 S4a) estiver em `main`, o gancho `_register_in_canonical_queue` passa a usa-lo sem
   outra mudanca; ate la o `DemandsStore` e a fila canonica.
3. Se `autosubmit_enabled()`, empurra o ticket para a linha (`submit_ticket_to_line`).

## Um alimentador so

O `CloudCoordinator` inicia um unico alimentador: `FeederCycle` (agenda, a cada 15 min) que
envolve `BacklogFeeder` (avalia G1, dependencias e vagas). Um redeploy nao perde nada: o
alimentador le o armazenamento duravel e o `submit_ticket_to_line` e idempotente
(`ticket:<id>`).

## Grill de um toque

A mensagem de grill no Telegram traz, por pergunta com recomendacao, o botao
"Aceitar recomendada": payload `cb:grill:<run_id>#<question_id>:<indice da opcao>`, o mesmo
caminho dos botoes de opcao (`build_telegram_line_grill_handler`), que grava a resposta e
acorda o job de grill.
