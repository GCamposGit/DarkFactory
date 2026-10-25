<!-- covers: core/usage/ticket_cost.py -->
# Custo por ticket (USR-378)

Cada tentativa de ticket (inclusive as que falham) grava uma linha em
`reports/ticket_quota_usage.jsonl` com ticket, tentativa, harness, modelo, estagio, variacao
da janela semanal e de 5h, custo de API e sucesso. Linha sem `ticket_id` e rejeitada.

- Custo de API: soma dos eventos de uso (OpenRouter etc.) com o mesmo `ticket_id`.
- Custo implicito da assinatura: variacao % da janela semanal / 100 x (mensalidade / 4,345).
- Mensalidades: `.factory/config/subscription_prices.json`, confirmadas pelo owner em 25/10/2026
  (20 USD/mes cada; Grok anual e Gemini promocional convertidos para o equivalente mensal).
- Hub: `GET /api/usage/ticket-costs` lista o custo total por ticket (tentativas falhas incluidas).
