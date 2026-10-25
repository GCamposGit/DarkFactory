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

## Ainda por vir (S4b)

Hub, escritores de `owner_actions`, `scripts/snapshot_queue.py` (snapshot diário no git) e
`node_sync`.

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
