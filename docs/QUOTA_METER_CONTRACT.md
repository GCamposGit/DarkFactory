# Contrato do medidor de cotas

O roteador usa a menor porcentagem **restante** entre as janelas conhecidas (`headroom`). Quota
desconhecida, leitura suspeita ou snapshot vencido bloqueia a rota; `limited` vale
0%. A idade vem de `checked_at`, não da hora de modificação do arquivo. O piso
operacional permanece 15%.

| Provedor | Fonte e campo bruto | Semântica confirmada | Janelas e reset | Conferência manual |
| --- | --- | --- | --- | --- |
| xAI/Grok | Grok Bot `GetSandUsageStatus` em `api2.cursor.sh`, `usagePercent` | **usado** em pontos percentuais; restante = `100 - usagePercent` | Pool semanal (`grok:weekly_pool`); `currentPeriodStart` e `nextResetTimestampUtc`. `hasAvailableUsage=false` limita a conta a 0% de headroom. Não há medição numérica independente da janela 5h. | [Grok usage](https://grok.com/?_s=usage): abrir a conta SuperGrok, conferir uso disponível e data de renovação. Endpoint é interno e pode mudar. |
| OpenAI/Codex | `codex app-server` `account/rateLimits/read`, `rateLimitsByLimitId.*.{primary,secondary}.usedPercent`; alternativamente `token_count.rate_limits` da sessão local | **usado** em pontos percentuais; restante = `100 - usedPercent` | Janela móvel 5h (`primary`, 300 min) e limite semanal (`secondary`, 10080 min); `resetsAt` em epoch seconds por janela. Se qualquer janela atingir >= 100%, status vira `limited` (0% headroom). | [Codex usage](https://chatgpt.com/codex/settings/usage): comparar os limites de 5h e semanais da mesma conta no painel oficial do ChatGPT. |
| Anthropic/Claude Code Desktop | StatusLine autenticado do Claude Code; `rate_limits.five_hour/seven_day.{used_percentage,resets_at}`; coletor `scripts/claude_statusline.py` | **usado**, pontos percentuais 0..100; restante% = `100 - used_percentage` | Janela móvel 5h (`claude:5h`, 300 min) e semanal 7 dias (`claude:weekly`, 10080 min); `resets_at` em epoch seconds ou RFC 3339 conforme o payload. Snapshot sanitizado em `state_root()/usage/providers/anthropic.json`, aceito por 120 segundos desde `checked_at`. | `python -m core.usage.cli accounts --refresh` deve mostrar `adapter=claude_statusline`, `quota_supported=true` e ambas as janelas; comparar com [Claude usage](https://claude.ai/settings/usage). Headers HTTP não são fonte ativa de quota. |
| Google/Antigravity | Language Server local `RetrieveUserQuotaSummary` (ou `GetAvailableModels`), `remainingFraction` | **restante**, fração 0..1; restante% = `remainingFraction * 100`, usado% = `100 - (remainingFraction * 100)` | Janela móvel 5h (`antigravity:gemini-5h`, 300 min) e limite semanal (`antigravity:gemini-weekly`, 10080 min); `resetTime` em RFC 3339 UTC por bucket. | Abrir painel interno do Antigravity / Language Server; conferir modelo Gemini Flash/Pro. [Google One](https://one.google.com/) indica plano, enquanto os buckets reais de tokens residem no LS. |

## Evidência e fixtures reais

Todas as quatro fontes possuem fixtures REAIS sanitizadas em `tests/fixtures/usage/`,
com data de captura, identificação de fonte e sem qualquer credencial, token ou e-mail:

1. `tests/fixtures/usage/xai_2026-10-01.json`: capturado via `GetSandUsageStatus`. Campo `usagePercent: 1.40324` -> `used=1.4%`, `remaining=98.6%`.
2. `tests/fixtures/usage/openai_2026-10-01.json`: capturado via `codex app-server` (`account/rateLimits/read`). Campos: `primary.usedPercent: 47` (5h -> `used=47%`, `remaining=53%`) e `secondary.usedPercent: 53` (semanal -> `used=53%`, `remaining=47%`). Headroom governante = 47.0% (semanal).
3. `tests/fixtures/usage/anthropic_2026-10-01.json`: fixture histórica de headers da API unificada. Ela valida apenas a interpretação antiga e não comprova nem alimenta a fonte ativa do Claude. A fonte ativa é o payload statusLine documentado acima; a captura real no Notebook ainda precisa ser anexada à evidência do ticket.
4. `tests/fixtures/usage/google_2026-10-01.json`: capturado via `RetrieveUserQuotaSummary` do Language Server local. Campos: `gemini-weekly.remainingFraction: 0.15` (semanal -> `used=85%`, `remaining=15%`) e `gemini-5h.remainingFraction: 1.0` (5h -> `used=0%`, `remaining=100%`). Headroom governante = 15.0%.

O teste parametrizado `tests/test_quota_meter_contract.py::test_real_fixtures_payload_semantics_fails_if_inverted`
assegura que qualquer inversão semântica (interpretar campo usado como restante ou vice-versa) falha deterministamente.

## Auditoria e conferência contra painel oficial

O script de auditoria `scripts/quota_audit.py` compara a leitura calculada do medidor contra
as evidências manuais observadas nos dashboards oficiais:

```powershell
python scripts/quota_audit.py --expect xai=VALOR --expect openai=VALOR --expect google=VALOR --expect anthropic=VALOR
```

- Se a divergência for <= 5 pontos percentuais, a auditoria é aprovada (código de saída 0).
- Se a divergência for > 5 pontos percentuais, um alerta `MISMATCH` é emitido e o comando sai com código 1.

Cada sonda real grava campos permitidos, interpretação e flags em
`.factory/usage/history/<provedor>.jsonl`. A rotação move o arquivo completo
para um arquivo com data após 30 dias; o arquivo ativo só recebe append. O
histórico é local e ignorado pelo Git.
Para confirmar uma leitura Grok suspeita após comparar o painel, executar
`scripts/quota_audit.py --provider xai --expect xai=VALOR --confirm`. O comando
só grava a confirmação local se a leitura bruta estiver a até 5 pontos do
valor informado; a confirmação vale apenas para o mesmo período e percentual.
