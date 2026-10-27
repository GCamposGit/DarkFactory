<!-- covers: core/line/routing.py, core/line/operating_harness.py, core/router/token_budget.py -->
# Roteamento do estagio de desenvolvimento (USR-378)

`core/line/routing.pick` e o unico roteador de producao. Para o estagio `development`:

- **Cascata**: `claude/sonnet`, `codex` e `grok` (`development.grok` no registro de modelos).
  `.factory/config/line_routing.json` e o default embutido `_default_routing_config` ficam alinhados.
- **Harness do chat so em ticket de complexidade alta**: a preferencia pelo harness de operacao
  (`DARKFAC_OPERATING_HARNESS` ou autodeteccao, `core/line/operating_harness.py`) so vale com
  `complexity == "high"`. Tickets `low` e `medium` ignoram o chat e vao para quem tiver mais folga. O `run_ticket.py` repassa o `estimated_complexity` do ticket (`low|medium|high`; ausente ou invalido nao e repassado) ao `pick` (USR-399).
- **Folga semanal**: para `development`, o ranking usa `remaining% da janela semanal / horas ate o reset`
  (maior primeiro), calculado por `_default_weekly_slack` (ou injetado por `slack_lookup`). Se algum
  candidato nao tiver janela semanal com reset conhecido, vale o headroom atual (maior primeiro).
- **Limiar critico unico**: `QUOTA_FLOOR_MIN` (15%) de `core/models/registry.py`. O roteador
  (`pressure_thresholds.critical`), a pressao de tokens e o gatilho de API paga em
  `core/router/token_budget.py` usam esse mesmo valor; cota desconhecida ou `<=` o piso nunca e eleita.

O probe de cota tem cache negativo de 10 min (`probe_negative.json` ao lado dos snapshots): probe que expira ou volta sem leitura utilizavel nao e repetido por 10 min. O probe do Google tenta no maximo 2 portas por fase, com timeout de 1 s (USR-404).

## Cota do Claude Code

A fonte de quota do Claude e o payload `rate_limits` fornecido pelo statusLine de uma sessão
autenticada do Claude Code Desktop. O comando versionado `scripts/claude_statusline.py` recebe o
payload por stdin, conserva apenas `five_hour` e `seven_day` (`used_percentage`, `resets_at`) e
grava atomicamente `anthropic.json` em `state_root()/usage/providers/` (por padrão
`.factory/usage/providers/`; a variável `DARKFAC_STATE_ROOT` pode mudar a raiz). Prompt, sessão,
workspace, email, tokens e o payload bruto não são persistidos.

O adaptador considera o snapshot fresco por 120 segundos, medidos desde `checked_at`. Payload
inválido ou sem janelas válidas não substitui a última captura boa. Sem leitura fresca, a CLI
`claude auth status --json` e as credenciais locais podem indicar conexão/plano, mas não quota;
quota desconhecida deixa Claude inelegível no roteador. Nenhuma chamada HTTP ou User-Agent
falsificado é usado para ler quota.

Para conferir no Notebook, abra uma sessão autenticada do Claude Code Desktop neste projeto,
aguarde o statusLine informar a sincronização e execute:

```powershell
python -m core.usage.cli accounts --refresh
```

Confirme que Anthropic apresenta `adapter=claude_statusline`, `quota_supported=true` e as janelas
`claude:5h` e `claude:weekly`, com percentuais e horários de reset. Se a captura não aparecer,
confira se o statusLine está habilitado para o projeto, se Python consegue executar
`scripts/claude_statusline.py` e se o arquivo sanitizado foi gravado sob `state_root()/usage/`.
Uma captura acima de 120 segundos deixa de ser elegível; uma nova chamada à CLI, sem `rate_limits`,
não estende a validade da quota antiga.
