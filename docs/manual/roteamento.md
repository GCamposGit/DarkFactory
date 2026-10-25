<!-- covers: core/line/routing.py, core/line/operating_harness.py, core/router/token_budget.py -->
# Roteamento do estagio de desenvolvimento (USR-378)

`core/line/routing.pick` e o unico roteador de producao. Para o estagio `development`:

- **Cascata**: `claude/sonnet`, `codex` e `grok` (`development.grok` no registro de modelos).
  `.factory/config/line_routing.json` e o default embutido `_default_routing_config` ficam alinhados.
- **Harness do chat so em ticket de complexidade alta**: a preferencia pelo harness de operacao
  (`DARKFAC_OPERATING_HARNESS` ou autodeteccao, `core/line/operating_harness.py`) so vale com
  `complexity == "high"`. Tickets `low` e `medium` ignoram o chat e vao para quem tiver mais folga.
- **Folga semanal**: para `development`, o ranking usa `remaining% da janela semanal / horas ate o reset`
  (maior primeiro), calculado por `_default_weekly_slack` (ou injetado por `slack_lookup`). Se algum
  candidato nao tiver janela semanal com reset conhecido, vale o headroom atual (maior primeiro).
- **Limiar critico unico**: `QUOTA_FLOOR_MIN` (15%) de `core/models/registry.py`. O roteador
  (`pressure_thresholds.critical`), a pressao de tokens e o gatilho de API paga em
  `core/router/token_budget.py` usam esse mesmo valor; cota desconhecida ou `<=` o piso nunca e eleita.
