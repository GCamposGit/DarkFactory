# Pedidos humanos do worker para o Hub (USR-194)

O worker mantém o espelho local e envia os pedidos ao Hub pela API autenticada
`POST /api/owner-actions/sync`. O endpoint recebe somente `SyncEvent`, calcula o
ID no servidor e não envia notificações Telegram. A chave é a credencial de
máquina existente `DARKFAC_TELEMETRY_KEY`; a URL vem de `DARKHUB_CLOUD_URL`.
O Compose do worker injeta ambas, sem valores reais versionados.

O outbox é o arquivo `artifacts/owner-action-outbox` sob `state_root()`, no volume
de artefatos da VPS. A retenção de diretórios de workflows não o remove. O envio
tem timeout de 1,5 segundo, até dois eventos por chamada e retry persistido em
30 segundos. Polling e heartbeat existentes tentam escoar a fila inclusive com
todos os slots ocupados; o heartbeat acorda em até 15 segundos. Indisponibilidade
mantém o evento pendente e emite diagnóstico sem credenciais.

Linhas inválidas e rejeições determinísticas ficam em quarentena durável, sem ACK
falso e sem bloquear outros pedidos. Erros de credencial mantêm os eventos e um
backoff global persistido; os diagnósticos contêm apenas classe ou código HTTP.

No Hub, `data_dir/owner_actions/dynamic.json` guarda os pedidos dinâmicos e o par
canônico run/etapa. Não altera definições versionadas. Colisão de ID com outra
identidade ou com uma ação curada é rejeitada antes de mutação. Conclusão é
terminal: mensagens antigas de abertura não reabrem a ação. O overlay de
resoluções humanas continua prevalecendo após reinício e deploy.

A prova controlada de produção publica um pedido sintético diretamente pelo
transporte, confirma chegada em menos de um minuto, publica conclusão e confirma
o estado final. Não chama o notificador nem resolve decisões reais do owner.
Falha nessa prova impede declarar o ticket entregue.
