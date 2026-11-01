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

## Cota do Claude entre sessoes (USR-416)

O snapshot `anthropic` so e gravado pelo statusline enquanto ha sessao do Claude Code aberta. Sem leitura nova, `accounts --refresh` tenta uma chamada minima `claude -p` (stream-json, modelo haiku, diretorio vazio) e le o `rate_limit_event`. Se nao houver leitura, o roteador mantem o ultimo snapshot valido ate o `resets_at` da janela de 5h, somando 10 pontos ao uso (so anthropic; piso de 15 0nalterado). Depois do reset a cota volta a ser desconhecida.

## Operador Interativo Antigravity (USR-433 / USR-436)

O Antigravity é elegível para o estágio `development` **exclusivamente** quando atua como harness de operação interativo (`DARKFAC_OPERATING_HARNESS=antigravity` ou autodetectado). Em modo headless ou na frota (`--fleet`, `fleet_dispatcher`), Antigravity é estritamente somente-leitura e nunca é selecionado para escrita. Se sua quota for desconhecida ou `STALE`, permanece elegível na interface de chat interativa em modo degradado com aviso explícito e causa estruturada, respeitando o piso de 15% para cotas conhecidas. A capacidade de escrita (`write`) é declarada dinamicamente via `agent_cli.supports()` quando `operating_harness == 'antigravity'` em contexto interativo (fora do modo headless), viabilizando sua eleição direta antes do ranking de folga.

## Fallback e Otimizacao OpenRouter (USR-440 / USR-444)

O OpenRouter atua na fabrica com duplo papel: **otimizacao continua de custo-beneficio (Fronteira de Pareto)** e **garantia de operacao continua (fallback zero-touch)** quando as cotas de assinatura estiverem no piso critico (<= 15%) ou zeradas:

1. **Planejamento com Alta Inteligencia**: O estagio `planning` usa o papel `planning.openrouter` apontando para modelos de fronteira de alta capacidade (`tier: high`), garantindo que o fatiamento e especificacao mantenham qualidade Staff+ mesmo sem Claude Opus ou Codex Sol.
2. **Desenvolvimento com Escrita Autonoma**: OpenRouter declara suporte a `write` (`agent_cli.supports("openrouter", "write") is True`) atraves do motor de aplicacao cirurgica de patches e criacao de arquivos (`_apply_openrouter_write_blocks`). No estagio `development`, quando todas as assinaturas locais estao esgotadas ou em cooldown, o OpenRouter e eleito com o papel `development.openrouter`, permitindo que a esteira continue implementando e validando sem depender de recargas manuais.
3. **Avaliacao por Fronteira de Pareto**: Modelos de micro-custo (papel `distill.openrouter` a $0.002/tarefa) sao avaliados para estagios de menor complexidade (`distill`, `review`, fatias mecanicas), poupando as janelas de assinatura para tarefas arquiteturais densas.
4. **Enforcement de Orcamento e Alertas no Telegram**: O roteador e a telemetria de creditos (`ApiCreditsMonitor`) respeitam o teto por execucao (`run_caps.openrouter_usd`, default US$ 2.00) e o teto mensal (`run_caps.openrouter_monthly_cap_usd`, default US$ 10.00). Ao atingir o limiar de alerta (`openrouter_alert_threshold`, default 80% ou US$ 8.00), uma notificacao critica com categoria `AlertCategory.EXECUTION_BUDGET` e enviada via Telegram ao Owner.
5. **Consulta Dinamica a Fronteira de Pareto sem Modelos Hard-coded (USR-444)**: Nenhum modelo de inferencia fica hard-coded em codigo ou instrucoes estaticas. O roteador invoca `resolve_pareto_openrouter_model`, consultando diariamente o ledger de benchmarks (`core.benchmarks.fetcher.ensure_daily_benchmark()`) gerado a partir de Artificial Analysis e OpenRouter. Para planejamento e arquitetura, a esteira exige alta inteligencia (score >= 85.0, classe Claude Opus `anthropic/claude-opus-5` e GPT Sol `openai/gpt-5-6-sol` / `gpt-sol`). Modelos proibidos para autonomia (`fable`, `astra`) sao excluidos por contrato. <!-- census: wire-id -->


