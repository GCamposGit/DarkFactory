<!-- covers: core/usage/ticket_cost.py, core/usage/adapters.py, scripts/claude_statusline.py, core/usage/sync_task.py, core/usage/models.py, core/usage/monitor.py, core/usage/refresh_requests.py, scripts/install_usage_sync_task.py, scripts/install_sync_task.ps1, hub/frontend/usage.js -->
# Custo por ticket (USR-378)

Cada tentativa de ticket (inclusive as que falham) grava uma linha em
`reports/ticket_quota_usage.jsonl` com ticket, tentativa, harness, modelo, estagio, variacao
da janela semanal e de 5h, custo de API e sucesso. Linha sem `ticket_id` e rejeitada.

- Custo de API: soma dos eventos de uso (OpenRouter etc.) com o mesmo `ticket_id`.
- Custo implicito da assinatura: variacao % da janela semanal / 100 x (mensalidade / 4,345).
- Mensalidades: `.factory/config/subscription_prices.json`, confirmadas pelo owner em 25/10/2026
  (20 USD/mes cada; Grok anual e Gemini promocional convertidos para o equivalente mensal).
- Hub: `GET /api/usage/ticket-costs` lista o custo total por ticket (tentativas falhas incluidas).

## Cota do Claude Code

Durante uma sessão, o Claude Code executa `scripts/claude_statusline.py` quando o statusline é
atualizado e, por causa de `refreshInterval: 30` em `.claude/settings.json`, também a cada 30
segundos. O comando recebe o JSON do statusline pela entrada padrão. A documentação oficial
confirma que `refreshInterval` repete o comando a cada N segundos, além das atualizações por
eventos ([Claude Code — Customize your status line](https://code.claude.com/docs/en/statusline#manually-configure-a-status-line)).
O script extrai somente `rate_limits.five_hour` e `rate_limits.seven_day` e grava `anthropic.json`
atomicamente em `.factory/usage/providers/` (ou no `DARKFAC_STATE_ROOT` configurado).
O adaptador considera snapshots de ate 15 minutos; assim, a leitura do statusline pode alimentar
o roteador mesmo quando a CLI local nao esta autenticada ou nao fornece `rate_limits`.

O arquivo persistido contem apenas percentuais usados/restantes, horario de reset, horario da
captura e metadados fixos do provedor. Campos de sessao, workspace, prompt, caminho e credenciais
do payload do Claude nao sao copiados. Payload invalido ou sem janelas com percentual valido nao
substitui a ultima leitura boa. A CLI continua sendo consultada como fonte alternativa quando o
snapshot nao esta fresco; nenhuma chamada HTTP e feita para coletar a cota.

## Probe headless do Claude (USR-416)

`ClaudeCodeAccountAdapter` tenta, apos `auth status`, `claude -p` minimo com `--output-format stream-json --verbose` e grava snapshot fresco a partir dos eventos `rate_limit_event` (sem chave nova).

## Medidor Antigravity RPC (USR-433)

`GeminiAccountAdapter` coleta quotas locais via Language Server local (`antigravity_rpc`). Quando a coleta remota ou as credenciais falham, o adaptador persiste um `ProbeFailure` estruturado com categoria (`unavailable`, `invalid_response`, `protocol`, etc.) e código do erro, mantendo o estado rastreável no relatório sem expor segredos.



## Publicacao de cotas e frescor

`core.usage.sync_task.publish_once()` le somente os JSONs locais de providers e credits:
nao chama `inspect`, nao forca sonda e nao invoca inferencia. Retorna `PublishResult`
com `ok`, `http_status`, `oldest_snapshot_age_seconds` (nulo se desconhecido) e `error`
sanitizado; nao propaga excecoes. O POST tem timeout HTTP de 6 segundos. Falha ao persistir no Hub devolve 503.
`scripts/sync_usage_to_cloud.py --once` e o invólucro CLI; `--loop --interval 300`
permanece disponivel. A exportacao diaria de OAs usa `scripts/snapshot_queue.py`
separadamente. O despachante ainda precisa chamar `publish_once()` pelo coordenador.

No publicador, configure `DARKFAC_TELEMETRY_KEY`, `DARKHUB_CLOUD_URL` e
`DARKFAC_NODE_ID`. A chave pode vir do `.env` do checkout principal. No Hub,
`DARKFAC_TELEMETRY_KEY` deve ser igual e `DARKHUB_USAGE_SNAPSHOT_DIR` deve apontar
para o volume persistente (ex.: `/app/hub/data/usage-snapshots`), para usar leitores sem sondas.
O servidor sobrepoe `received_at` com a hora UTC de chegada. A API calcula `age_seconds`
por esse campo, inclusive ao servir cache, e `stale=true` acima de 1200 segundos.
O alerta de publicacao parada usa o mesmo limiar de 20 minutos, evitando oscilar
na cadencia de cinco minutos da tarefa. Snapshots legados sem chegada conhecida ficam desatualizados. `checked_at` continua
indicando a medicao na origem: republicar um snapshot antigo renova a chegada, nao a medicao.
`measurement_age_seconds` informa essa segunda idade; medicao velha ou desconhecida
tambem marca `stale`, mesmo com chegada recente. O destaque usa a maior idade conhecida.
Publicacoes substituem o snapshot inteiro, sem reter percentuais anteriores ausentes no novo payload.
Pedidos manuais de refresh sao encerrados na chegada da publicacao: leitura anterior ao pedido
devolve falha explicita, nunca sucesso de medicao nova; nao ha polling separado no host.
O painel mostra idade por cartao, destaca DESATUALIZADO em vermelho e esmaece percentuais.

O instalador Python agora registra `--once` no logon e a cada cinco minutos,
com S4U (sem sessao interativa), tarefa habilitada e tres reinicios em falha.
O invólucro PowerShell usa o mesmo instalador. Tarefas antigas/interativas/desabilitadas
sao reconhecidas como atualizacao necessaria. O instalador nao foi executado nesta entrega.
S4U precisa ter acesso local aos snapshots e ao `.env`; validar HTTPS no contexto da tarefa.

A sonda Claude aproveita o snapshot de statusLine por 15 minutos, inclusive com force.
A chamada paga so e permitida depois dessa idade e com todas as janelas conhecidas
acima de 15%; no piso, retorna a ultima leitura, sem inferencia. A publicacao nunca
aciona essa sonda. Apos deploy, conferir `received_at`, aguardar/simular mais de 20 minutos
sem publicacao e verificar `stale` e destaque visual; observar recibos do publicador.
