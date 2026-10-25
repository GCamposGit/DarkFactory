# Fila de demandas

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
