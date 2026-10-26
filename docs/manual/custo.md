<!-- covers: core/usage/ticket_cost.py, core/usage/adapters.py, scripts/claude_statusline.py -->
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

O Claude Code Desktop executa `scripts/claude_statusline.py` a cada 30 segundos. O script recebe
o JSON do statusline, extrai somente `rate_limits.five_hour` e `rate_limits.seven_day` e grava
`anthropic.json` atomicamente em `.factory/usage/providers/` (ou no `DARKFAC_STATE_ROOT` configurado).
O adaptador considera snapshots de ate 120 segundos; assim, a leitura do statusline pode alimentar
o roteador mesmo quando a CLI local nao esta autenticada ou nao fornece `rate_limits`.

O arquivo persistido contem apenas percentuais usados/restantes, horario de reset, horario da
captura e metadados fixos do provedor. Campos de sessao, workspace, prompt, caminho e credenciais
do payload do Claude nao sao copiados. Payload invalido ou sem janelas com percentual valido nao
substitui a ultima leitura boa. A CLI continua sendo consultada como fonte alternativa quando o
snapshot nao esta fresco; nenhuma chamada HTTP e feita para coletar a cota.
